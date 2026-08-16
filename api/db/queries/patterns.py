"""DB queries for /patterns endpoints — cross-college pattern signals.

pattern_signals is a fully derived table (see pipeline/pattern_builder.py): it
is dropped and rebuilt on every run, so signal_id is NOT stable across
rebuilds.  Callers must treat a signal_id as valid only for the current
build — never persist one as a bookmark.

Unlike every other list endpoint here, these queries take no school/date
filter.  A signal is an aggregate ACROSS schools; filtering one out would
change what the aggregate means while leaving school_count untouched, which
is a subtly wrong number rather than a smaller list.  Date filtering is
similarly withheld: first/last_observed_date are the span of the underlying
rows, so a range filter would have to re-aggregate to stay honest.
"""
from __future__ import annotations
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text

# Signal types whose supporting rows can be expanded for a detail view.
# budget_trend and personnel_trend aggregate financial_items / personnel_actions
# and do not record their supporting ids, so they cannot be traced yet.
TRACEABLE_TYPES = {"recurring_initiative"}

_UNTRACEABLE_NOTE = (
    "This signal aggregates {table} directly and does not record which rows "
    "produced it, so it cannot be expanded. Only initiative signals are "
    "traceable today."
)

_NOTE_BY_TYPE = {
    "budget_trend":    _UNTRACEABLE_NOTE.format(table="financial_items"),
    "personnel_trend": _UNTRACEABLE_NOTE.format(table="personnel_actions"),
}


def _row_to_dict(r) -> dict:
    d = dict(r._mapping)
    ids = d.pop("supporting_initiative_ids", None) or []
    d["supporting_count"] = len(ids)
    d["traceable"] = d["signal_type"] in TRACEABLE_TYPES and len(ids) > 0
    return d


def list_patterns(
    db: Session,
    signal_type:  Optional[str],
    category:     Optional[str],
    needs_review: Optional[bool],
    min_schools:  Optional[int],
    limit:        int,
    offset:       int,
) -> tuple[list[dict], int]:
    filters: list[str] = []
    params: dict = {"limit": limit, "offset": offset}

    if signal_type:
        filters.append("p.signal_type = :signal_type")
        params["signal_type"] = signal_type
    if category:
        filters.append("p.category = :category")
        params["category"] = category
    if needs_review is not None:
        filters.append("p.needs_review = :needs_review")
        params["needs_review"] = needs_review
    if min_schools is not None:
        filters.append("p.school_count >= :min_schools")
        params["min_schools"] = min_schools

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    total = db.execute(
        text(f"SELECT COUNT(*) FROM pattern_signals p {where}"), params
    ).scalar()

    rows = db.execute(text(f"""
        SELECT
            p.signal_id, p.signal_type, p.category, p.description,
            p.school_count, p.meeting_count,
            p.first_observed_date, p.last_observed_date,
            p.confidence, p.needs_review, p.extractor_version,
            p.supporting_initiative_ids
        FROM pattern_signals p
        {where}
        ORDER BY p.needs_review, p.confidence DESC, p.school_count DESC, p.signal_id
        LIMIT :limit OFFSET :offset
    """), params).fetchall()

    return [_row_to_dict(r) for r in rows], total


def get_pattern_detail(db: Session, signal_id: int) -> Optional[dict]:
    """Expand one signal into the rows and verified quotations behind it."""
    row = db.execute(text("""
        SELECT
            p.signal_id, p.signal_type, p.category, p.description,
            p.school_count, p.meeting_count,
            p.first_observed_date, p.last_observed_date,
            p.confidence, p.needs_review, p.extractor_version,
            p.supporting_initiative_ids
        FROM pattern_signals p
        WHERE p.signal_id = :sid
    """), {"sid": signal_id}).fetchone()

    if row is None:
        return None

    init_ids = list(row.supporting_initiative_ids or [])
    detail = _row_to_dict(row)
    detail.update({
        "schools": [],
        "supporting_initiatives": [],
        "evidence": [],
        "trace_note": None,
    })

    if not init_ids:
        detail["trace_note"] = _NOTE_BY_TYPE.get(
            row.signal_type,
            "No supporting rows were recorded for this signal.",
        )
        return detail

    # Bind the id list explicitly rather than interpolating — same pattern the
    # insights queries use for meeting-id fan-out.
    placeholders = ", ".join(f":i{n}" for n in range(len(init_ids)))
    params = {f"i{n}": v for n, v in enumerate(init_ids)}

    init_rows = db.execute(text(f"""
        SELECT
            i.initiative_id, i.school_slug, s.name AS school_name,
            i.meeting_id, m.title AS meeting_title, m.published_date,
            i.initiative_name, i.category, i.observed_action,
            i.measured_outcome, i.confidence,
            (SELECT COUNT(*) FROM extraction_evidence e
              WHERE e.initiative_id = i.initiative_id) AS evidence_count
        FROM initiatives i
        JOIN meetings m ON m.meeting_id = i.meeting_id
        JOIN schools  s ON s.school_id  = i.school_id
        WHERE i.initiative_id IN ({placeholders})
        ORDER BY m.published_date DESC NULLS LAST, i.initiative_id
    """), params).fetchall()

    detail["supporting_initiatives"] = [dict(r._mapping) for r in init_rows]

    # Per-school rollup, derived from the supporting rows rather than stored —
    # pattern_signals keeps only school_count, and a count cannot tell you
    # whether measured outcomes came from one school or five.
    school_rows = db.execute(text(f"""
        SELECT
            i.school_slug, s.name AS school_name,
            COUNT(*) AS initiative_count,
            COUNT(i.measured_outcome) AS measured_count
        FROM initiatives i
        JOIN schools s ON s.school_id = i.school_id
        WHERE i.initiative_id IN ({placeholders})
        GROUP BY i.school_slug, s.name
        ORDER BY COUNT(*) DESC, i.school_slug
    """), params).fetchall()

    detail["schools"] = [dict(r._mapping) for r in school_rows]

    return detail


def get_pattern_evidence(
    db: Session, signal_id: int, limit: int = 20
) -> Optional[list[dict]]:
    """Verified quotations behind a signal, newest meeting first.

    Returns None when the signal does not exist, [] when it exists but has no
    traceable rows — the caller needs to tell those apart to choose between a
    404 and an empty panel.
    """
    row = db.execute(
        text("SELECT supporting_initiative_ids FROM pattern_signals WHERE signal_id = :sid"),
        {"sid": signal_id},
    ).fetchone()
    if row is None:
        return None

    init_ids = list(row.supporting_initiative_ids or [])
    if not init_ids:
        return []

    placeholders = ", ".join(f":i{n}" for n in range(len(init_ids)))
    params: dict = {f"i{n}": v for n, v in enumerate(init_ids)}
    params["limit"] = limit

    ev_rows = db.execute(text(f"""
        SELECT
            e.initiative_id, e.chunk_id, e.exact_quote, e.supports,
            e.start_time_sec, e.meeting_id,
            m.title AS meeting_title, i.school_slug
        FROM extraction_evidence e
        JOIN initiatives i ON i.initiative_id = e.initiative_id
        JOIN meetings    m ON m.meeting_id    = e.meeting_id
        WHERE e.initiative_id IN ({placeholders})
        ORDER BY m.published_date DESC NULLS LAST,
                 e.start_time_sec NULLS LAST, e.evidence_id
        LIMIT :limit
    """), params).fetchall()

    return [{
        "initiative_id": r.initiative_id,
        "chunk_id":      r.chunk_id,
        "text":          r.exact_quote,
        "supports":      list(r.supports) if r.supports else [],
        "timestamp_sec": r.start_time_sec,
        "meeting_id":    r.meeting_id,
        "meeting_title": r.meeting_title,
        "school_slug":   r.school_slug,
        "verified":      True,
    } for r in ev_rows]


def get_patterns_summary(db: Session) -> dict:
    """Headline counts for a patterns dashboard."""
    totals = db.execute(text("""
        SELECT
            COUNT(*)                                   AS total,
            COUNT(*) FILTER (WHERE NOT needs_review)   AS trustee_ready,
            COUNT(*) FILTER (WHERE needs_review)       AS needs_review,
            COUNT(DISTINCT category)                   AS categories,
            COALESCE(MAX(school_count), 0)             AS max_school_count
        FROM pattern_signals
    """)).fetchone()

    by_type = db.execute(text("""
        SELECT
            signal_type,
            COUNT(*)                                 AS total,
            COUNT(*) FILTER (WHERE NOT needs_review) AS trustee_ready
        FROM pattern_signals
        GROUP BY signal_type
        ORDER BY COUNT(*) DESC, signal_type
    """)).fetchall()

    versions = db.execute(text(
        "SELECT DISTINCT extractor_version FROM pattern_signals ORDER BY 1"
    )).fetchall()

    return {
        "total":            totals.total,
        "trustee_ready":    totals.trustee_ready,
        "needs_review":     totals.needs_review,
        "categories":       totals.categories,
        "max_school_count": totals.max_school_count,
        "by_type":          [dict(r._mapping) for r in by_type],
        "extractor_versions": [r[0] for r in versions if r[0]],
    }
