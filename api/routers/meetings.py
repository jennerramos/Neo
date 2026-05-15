"""Meetings router."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from api.db.session import get_db
from api.services.meetings_service import get_meetings, get_meeting, get_transcript
from api.schemas.meetings import MeetingListResponse, MeetingOverview, MeetingTranscript

router = APIRouter(prefix="/meetings", tags=["meetings"])


@router.get("", response_model=MeetingListResponse)
def list_meetings(
    school: Optional[str] = Query(None, description="Filter by school slug"),
    date_from: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    date_to:   Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    status:    Optional[str] = Query(None, description="processing status"),
    limit:     int = Query(20, ge=1, le=200),
    offset:    int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    return get_meetings(
        db, school_slug=school, date_from=date_from,
        date_to=date_to, status=status, limit=limit, offset=offset,
    )


@router.get("/{meeting_id}", response_model=MeetingOverview)
def meeting_detail(meeting_id: int, db: Session = Depends(get_db)):
    result = get_meeting(db, meeting_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return result


@router.get("/{meeting_id}/transcript", response_model=MeetingTranscript)
def meeting_transcript(meeting_id: int, db: Session = Depends(get_db)):
    """Full ordered transcript for a meeting — every chunk, not just highlights."""
    result = get_transcript(db, meeting_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return result
