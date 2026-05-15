"""Votes router."""
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.db.session import get_db
from api.services.votes_service import get_votes, get_summary
from api.schemas.votes import VoteListResponse, VotesStats

router = APIRouter(prefix="/votes", tags=["votes"])


@router.get("", response_model=VoteListResponse)
def list_votes(
    school:     Optional[str]  = Query(None, description="Filter by school slug"),
    meeting_id: Optional[int]  = Query(None),
    passed:     Optional[bool] = Query(None),
    date_from:  Optional[str]  = Query(None, description="Start date YYYY-MM-DD"),
    date_to:    Optional[str]  = Query(None, description="End date YYYY-MM-DD"),
    limit:      int = Query(50, ge=1, le=500),
    offset:     int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return get_votes(
        db, school_slug=school, meeting_id=meeting_id,
        passed=passed, date_from=date_from, date_to=date_to,
        limit=limit, offset=offset,
    )


@router.get("/summary", response_model=VotesStats)
def votes_summary(
    school:    Optional[str] = Query(None, description="Filter by school slug"),
    date_from: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    date_to:   Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    db: Session = Depends(get_db),
):
    """Headline stats for the Votes page — total, pass rate, unanimity, top movers."""
    return get_summary(db, school_slug=school, date_from=date_from, date_to=date_to)
