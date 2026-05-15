"""DB queries for financials endpoints."""
from __future__ import annotations
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text

from ._filters import apply_school_date_filters


def list_financials(
    db: Session,
    school_slug: Optional[str],
    meeting_id: Optional[int],
    action_type: Optional[str],
    category: Optional[str],
    amount_min: Optional[float],
    amount_max: Optional[float],
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
        filters.append("f.meeting_id = :meeting_id")
        params["meeting_id"] = meeting_id
    if action_type:
        filters.append("f.action_type = :action_type")
        params["action_type"] = action_type
    if category:
        filters.append("LOWER(f.category) LIKE :category")
        params["category"] = f"%{category.lower()}%"
    if amount_min is not None:
        filters.append("f.amount >= :amount_min")
        params["amount_min"] = amount_min
    if amount_max is not None:
        filters.append("f.amount <= :amount_max")
        params["amount_max"] = amount_max

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    count_sql = text(f"""
        SELECT COUNT(*) FROM financial_items f
        JOIN meetings m ON m.meeting_id = f.meeting_id
        JOIN schools s ON s.school_id = m.school_id
        {where}
    """)
    total = db.execute(count_sql, params).scalar()

    rows_sql = text(f"""
        SELECT
            f.item_id, s.slug AS school_slug, s.name AS school_name,
            f.meeting_id, m.title AS meeting_title,
            m.published_date,
            f.action_type, f.category, f.vendor,
            f.amount, f.description,
            f.confidence
        FROM financial_items f
        JOIN meetings m ON m.meeting_id = f.meeting_id
        JOIN schools s ON s.school_id = m.school_id
        {where}
        ORDER BY m.published_date DESC NULLS LAST, f.amount DESC NULLS LAST
        LIMIT :limit OFFSET :offset
    """)
    rows = db.execute(rows_sql, params).fetchall()
    return [dict(r._mapping) for r in rows], total


def get_financials_summary(
    db: Session,
    school_slug: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    """Return totals by action-type + top vendors, scoped by school/date."""
    params: dict = {}
    extra: list[str] = []
    apply_school_date_filters(
        extra, params,
        school_slug=school_slug, date_from=date_from, date_to=date_to,
    )

    extra_clause = (" AND " + " AND ".join(extra)) if extra else ""

    totals = db.execute(text(f"""
        SELECT action_type, COUNT(*) AS cnt, SUM(amount) AS total
        FROM financial_items f
        JOIN meetings m ON m.meeting_id = f.meeting_id
        JOIN schools s ON s.school_id = m.school_id
        WHERE f.needs_review = FALSE {extra_clause}
        GROUP BY action_type
        ORDER BY total DESC NULLS LAST
    """), params).fetchall()

    top_vendors = db.execute(text(f"""
        SELECT vendor, COUNT(*) AS cnt, SUM(amount) AS total
        FROM financial_items f
        JOIN meetings m ON m.meeting_id = f.meeting_id
        JOIN schools s ON s.school_id = m.school_id
        WHERE f.needs_review = FALSE AND vendor IS NOT NULL {extra_clause}
        GROUP BY vendor
        ORDER BY total DESC NULLS LAST
        LIMIT 10
    """), params).fetchall()

    # Single largest line item in the current scope. Trustees care about the
    # outlier: a $20M building purchase says more about priorities than a
    # rolled-up category total. We surface it as its own card and link it to
    # the originating meeting.
    largest = db.execute(text(f"""
        SELECT
            f.item_id,
            f.amount,
            f.action_type,
            f.vendor,
            f.description,
            f.meeting_id,
            m.published_date,
            m.title          AS meeting_title,
            s.slug           AS school_slug,
            s.name           AS school_name
        FROM financial_items f
        JOIN meetings m ON m.meeting_id = f.meeting_id
        JOIN schools s ON s.school_id = m.school_id
        WHERE f.needs_review = FALSE AND f.amount IS NOT NULL {extra_clause}
        ORDER BY f.amount DESC NULLS LAST
        LIMIT 1
    """), params).fetchone()

    return {
        "by_action_type": [dict(r._mapping) for r in totals],
        "top_vendors":    [dict(r._mapping) for r in top_vendors],
        "largest_item":   dict(largest._mapping) if largest else None,
    }
