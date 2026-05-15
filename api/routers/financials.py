"""Financials router."""
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.db.session import get_db
from api.services.financials_service import get_financials, get_summary
from api.schemas.financials import FinancialListResponse, FinancialsStats

router = APIRouter(prefix="/financials", tags=["financials"])


@router.get("", response_model=FinancialListResponse)
def list_financials(
    school:      Optional[str]   = Query(None, description="Filter by school slug"),
    meeting_id:  Optional[int]   = Query(None),
    action_type: Optional[str]   = Query(None, description="approved | proposed | discussed"),
    category:    Optional[str]   = Query(None),
    amount_min:  Optional[float] = Query(None),
    amount_max:  Optional[float] = Query(None),
    date_from:   Optional[str]   = Query(None, description="Start date YYYY-MM-DD"),
    date_to:     Optional[str]   = Query(None, description="End date YYYY-MM-DD"),
    limit:       int = Query(50, ge=1, le=500),
    offset:      int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return get_financials(
        db, school_slug=school, meeting_id=meeting_id,
        action_type=action_type, category=category,
        amount_min=amount_min, amount_max=amount_max,
        date_from=date_from, date_to=date_to,
        limit=limit, offset=offset,
    )


@router.get("/summary", response_model=FinancialsStats)
def financials_summary(
    school:    Optional[str] = Query(None, description="Filter by school slug"),
    date_from: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    date_to:   Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    db: Session = Depends(get_db),
):
    """Headline stats for the Financials page — scoped by school + date range."""
    return get_summary(db, school_slug=school, date_from=date_from, date_to=date_to)
