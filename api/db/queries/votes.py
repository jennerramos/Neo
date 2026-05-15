"""DB queries for votes endpoints."""
from __future__ import annotations
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text

from ._filters import apply_school_date_filters


def list_votes(
    db: Session,
    school_slug: Optional[str],
    meeting_id: Optional[int],
    passed: Optional[bool],
    date_from: Optional[str],
    date_to: Optional[str],
    limit: int,
    offset: int,
) -> tuple[list[dict], int]:
    filters: list[str] = []
    params: dict = {"limit": limit, "offset": offset}

    apply_school_date_filters(
        filters, params,
        school_slug=school_slug, date_from=date_from, date_to=date_to,
    )
    if meeting_id is not None:
        filters.append("v.meeting_id = :meeting_id")
        params["meeting_id"] = meeting_id
    if passed is not None:
        filters.append("v.passed = :passed")
        params["passed"] = passed

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    count_sql = text(f"""
        SELECT COUNT(*) FROM votes v
        JOIN meetings m ON m.meeting_id = v.meeting_id
        JOIN schools s ON s.school_id = m.school_id
        {where}
    """)
    total = db.execute(count_sql, params).scalar()

    rows_sql = text(f"""
        SELECT
            v.vote_id, s.slug AS school_slug, s.name AS school_name,
            v.meeting_id, m.title AS meeting_title,
            m.published_date,
            v.motion_text, v.vote_result_text,
            v.yes_count, v.no_count, v.abstain_count,
            v.passed, v.unanimous, v.moved_by,
            v.confidence
        FROM votes v
        JOIN meetings m ON m.meeting_id = v.meeting_id
        JOIN schools s ON s.school_id = m.school_id
        {where}
        ORDER BY m.published_date DESC NULLS LAST, v.vote_id
        LIMIT :limit OFFSET :offset
    """)
    rows = db.execute(rows_sql, params).fetchall()
    return [dict(r._mapping) for r in rows], total


def get_votes_summary(
    db: Session,
    school_slug: Optional[str] = None,
    date_from: Optional[str]   = None,
    date_to: Optional[str]     = None,
) -> dict:
    """Headline stats for the Votes page: total, pass rate, unanimity, top movers.

    Respects the same school/date filters the list endpoint takes so the cards
    update as the user narrows the table.
    """
    filters: list[str] = []
    params: dict = {}
    apply_school_date_filters(
        filters, params,
        school_slug=school_slug, date_from=date_from, date_to=date_to,
    )
    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    totals = db.execute(text(f"""
        SELECT
            COUNT(*)                                        AS total,
            COUNT(*) FILTER (WHERE v.passed = TRUE)         AS passed,
            COUNT(*) FILTER (WHERE v.passed = FALSE)        AS failed,
            COUNT(*) FILTER (WHERE v.unanimous = TRUE)      AS unanimous
        FROM votes v
        JOIN meetings m ON m.meeting_id = v.meeting_id
        JOIN schools s  ON s.school_id  = m.school_id
        {where}
    """), params).fetchone()

    movers_where = "WHERE " + " AND ".join([*filters, "v.moved_by IS NOT NULL"])
    top_movers = db.execute(text(f"""
        SELECT v.moved_by AS name, COUNT(*) AS cnt
        FROM votes v
        JOIN meetings m ON m.meeting_id = v.meeting_id
        JOIN schools s  ON s.school_id  = m.school_id
        {movers_where}
        GROUP BY v.moved_by
        ORDER BY cnt DESC
        LIMIT 5
    """), params).fetchall()

    t = totals._mapping if totals else {"total": 0, "passed": 0, "failed": 0, "unanimous": 0}
    total = t["total"] or 0
    passed = t["passed"] or 0
    # pass_rate out of decided motions only (passed+failed) — an abstention-
    # dominated vote shouldn't artificially deflate the rate.
    decided = passed + (t["failed"] or 0)

    return {
        "total":          total,
        "passed":         passed,
        "failed":         t["failed"] or 0,
        "unanimous":      t["unanimous"] or 0,
        "pass_rate":      round(passed / decided, 3) if decided else None,
        "unanimous_rate": round((t["unanimous"] or 0) / total, 3) if total else None,
        "top_movers":     [dict(r._mapping) for r in top_movers],
    }
