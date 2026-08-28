"""
Pattern signals over the API.

pattern_signals held the only cross-college claims Neo makes and had no
endpoint at all — it was reachable solely by counting rows in
scripts/status_check.py.  These tests pin the parts of the new /patterns
layer that can be checked without a live database: the traceability
derivation, the route ordering that decides whether /patterns/summary is a
path or a bad integer, and the response contracts that carry a signal's
caveats alongside its claim.

The unit tests below need no database. The corpus checks at the end do use
the configured one, because what a signal aggregates over is a property of the
built table, not of the code that reads it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.db.queries.patterns import TRACEABLE_TYPES, _row_to_dict
from api.routers.patterns import router
from api.schemas.patterns import (
    PatternDetail, PatternEvidence, PatternRow, PatternsStats,
)


def _row(**over):
    """Minimal stand-in for a pattern_signals row from SQLAlchemy."""
    base = {
        "signal_id": 1,
        "signal_type": "recurring_initiative",
        "category": "dual_enrollment",
        "description": "…",
        "school_count": 7,
        "meeting_count": 35,
        "first_observed_date": None,
        "last_observed_date": None,
        "confidence": 0.97,
        "needs_review": False,
        "extractor_version": "v2.6",
        "supporting_initiative_ids": [1, 2, 3],
    }
    base.update(over)
    return SimpleNamespace(_mapping=base, **base)


# ---------------------------------------------------------------------------
# Traceability
# ---------------------------------------------------------------------------

def test_initiative_signal_with_ids_is_traceable():
    d = _row_to_dict(_row())
    assert d["traceable"] is True
    assert d["supporting_count"] == 3


def test_supporting_ids_do_not_leak_into_the_row():
    """The id array is an implementation detail; the row exposes a count."""
    d = _row_to_dict(_row())
    assert "supporting_initiative_ids" not in d


@pytest.mark.parametrize("signal_type", ["budget_trend", "personnel_trend"])
def test_aggregate_signals_are_not_traceable(signal_type):
    """These aggregate other tables and record no supporting ids."""
    d = _row_to_dict(_row(signal_type=signal_type, supporting_initiative_ids=[]))
    assert d["traceable"] is False
    assert d["supporting_count"] == 0


def test_initiative_signal_without_ids_is_not_traceable():
    """A traceable TYPE with an empty array still cannot be expanded — the
    claim is about the rows on hand, not the category they belong to."""
    d = _row_to_dict(_row(supporting_initiative_ids=[]))
    assert d["traceable"] is False


def test_null_id_array_is_treated_as_empty():
    d = _row_to_dict(_row(supporting_initiative_ids=None))
    assert d["supporting_count"] == 0
    assert d["traceable"] is False


def test_only_initiative_signals_are_declared_traceable():
    assert TRACEABLE_TYPES == {"recurring_initiative"}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _paths():
    return [r.path for r in router.routes]


def test_summary_is_registered_before_the_id_route():
    """Reversed, FastAPI would try to parse "summary" as an int and 422."""
    paths = _paths()
    assert paths.index("/patterns/summary") < paths.index("/patterns/{signal_id}")


def test_expected_routes_exist():
    assert set(_paths()) == {
        "/patterns",
        "/patterns/summary",
        "/patterns/{signal_id}",
        "/patterns/{signal_id}/evidence",
    }


# ---------------------------------------------------------------------------
# Response contracts
# ---------------------------------------------------------------------------

def test_row_defaults_are_conservative():
    """An unknown signal defaults to needing review and being untraceable —
    the safe direction for anything shown to trustees."""
    row = PatternRow(
        signal_id=1, signal_type="recurring_initiative", category="x",
        description="y", school_count=2, meeting_count=2,
    )
    assert row.needs_review is True
    assert row.traceable is False
    assert row.supporting_count == 0


def test_detail_extends_row_so_list_and_detail_cannot_drift():
    assert issubclass(PatternDetail, PatternRow)


def test_detail_lists_default_empty_with_no_note():
    d = PatternDetail(
        signal_id=1, signal_type="budget_trend", category="bond",
        description="y", school_count=3, meeting_count=4,
    )
    assert d.schools == []
    assert d.supporting_initiatives == []
    assert d.evidence == []
    assert d.trace_note is None


def test_evidence_requires_a_chunk():
    """A PatternEvidence row without a chunk id is not evidence — every
    quotation in this table was located inside a named chunk."""
    with pytest.raises(Exception):
        PatternEvidence(initiative_id=1, text="a quote")


def test_evidence_defaults_to_verified():
    ev = PatternEvidence(initiative_id=1, chunk_id="vid_0001", text="a quote")
    assert ev.verified is True
    assert ev.supports == []


def test_stats_shape():
    s = PatternsStats(
        total=27, trustee_ready=26, needs_review=1, by_type=[],
        categories=25, max_school_count=8, extractor_versions=["v2.6"],
    )
    assert s.total == s.trustee_ready + s.needs_review


# ── P2-2: what a signal is allowed to aggregate over ────────────────────────
#
# Signals are the only cross-college claim Neo makes, and the Insights page now
# renders them above the matrix. Each check here corresponds to a defect the
# built table actually carried.

@pytest.fixture(scope="module")
def signal_db():
    from api.db.session import SessionLocal
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_no_signal_draws_on_an_archived_or_soft_deleted_meeting(signal_db):
    """All three builders joined `meetings` for the date range and none of them
    filtered it, so 39 `archived_old`, `is_active = FALSE` meetings fed the
    trustee-ready signals — the same defect the Insights page fixed in its own
    queries, but with a longer reach, because a signal claims something about
    every college at once."""
    from sqlalchemy import text

    rows = signal_db.execute(text("""
        SELECT DISTINCT m.meeting_id, m.status, m.is_active
        FROM pattern_signals p
        JOIN initiatives i ON i.initiative_id = ANY(p.supporting_initiative_ids)
        JOIN meetings    m ON m.meeting_id    = i.meeting_id
        WHERE m.status <> 'indexed' OR NOT m.is_active
    """)).fetchall()
    assert not rows, (
        f"{len(rows)} archived or soft-deleted meetings feed pattern signals: "
        f"{[tuple(r) for r in rows[:3]]}"
    )


def test_signal_date_ranges_stay_inside_the_pilot_corpus(signal_db):
    """A signal reporting "first observed 2020-08-21" is quoting a meeting the
    pipeline archived out of the pilot. The band states these dates on screen,
    so an out-of-corpus one is visible to a trustee."""
    import config
    from sqlalchemy import text

    rows = signal_db.execute(text("""
        SELECT signal_id, category, first_observed_date, last_observed_date
        FROM pattern_signals
        WHERE first_observed_date IS NOT NULL
          AND EXTRACT(YEAR FROM first_observed_date) < :cutoff
    """), {"cutoff": config.MEETING_YEAR_CUTOFF}).fetchall()
    assert not rows, (
        f"signals observed before the {config.MEETING_YEAR_CUTOFF} corpus cutoff: "
        f"{[tuple(r) for r in rows[:3]]}"
    )


def test_a_signal_never_starts_after_it_ends(signal_db):
    from sqlalchemy import text

    rows = signal_db.execute(text("""
        SELECT signal_id, first_observed_date, last_observed_date
        FROM pattern_signals
        WHERE first_observed_date IS NOT NULL
          AND last_observed_date IS NOT NULL
          AND first_observed_date > last_observed_date
    """)).fetchall()
    assert not rows, [tuple(r) for r in rows[:3]]


def test_trustee_ready_signals_span_at_least_two_institutions(signal_db):
    """The band asks for min_schools=2 and needs_review=false. One college's own
    record is not a pattern across colleges, and the gate has to hold in the
    table as well as in the query string."""
    from sqlalchemy import text

    rows = signal_db.execute(text("""
        SELECT signal_id, category, school_count
        FROM pattern_signals
        WHERE needs_review = FALSE AND school_count < 2
    """)).fetchall()
    assert not rows, (
        f"trustee-ready signals from a single institution: "
        f"{[tuple(r) for r in rows[:3]]}"
    )
