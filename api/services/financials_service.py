"""Service layer for financials."""
from __future__ import annotations
from typing import Optional
from sqlalchemy.orm import Session

from api.db.queries.financials import list_financials, get_financials_summary
from api.schemas.financials import FinancialRow, FinancialListResponse
from api.schemas.common import Pagination


def get_financials(
    db: Session,
    school_slug: Optional[str] = None,
    meeting_id: Optional[int] = None,
    action_type: Optional[str] = None,
    category: Optional[str] = None,
    amount_min: Optional[float] = None,
    amount_max: Optional[float] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> FinancialListResponse:
    rows, total = list_financials(
        db, school_slug=school_slug, meeting_id=meeting_id,
        action_type=action_type, category=category,
        amount_min=amount_min, amount_max=amount_max,
        date_from=date_from, date_to=date_to,
        limit=limit, offset=offset,
    )
    return FinancialListResponse(
        items=[FinancialRow(**r) for r in rows],
        pagination=Pagination.build(total=total, limit=limit, offset=offset),
    )


def get_summary(
    db: Session,
    school_slug: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    return get_financials_summary(
        db, school_slug=school_slug, date_from=date_from, date_to=date_to,
    )
