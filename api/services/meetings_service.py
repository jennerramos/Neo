"""Service layer for meetings — wraps DB queries."""
from __future__ import annotations
from typing import Optional
from sqlalchemy.orm import Session

from api.db.queries.meetings import list_meetings, get_meeting_overview, get_meeting_transcript
from api.schemas.meetings import (
    MeetingRow, MeetingListResponse, MeetingOverview,
    VoteSummary, FinancialSummary, PersonnelSummary, TranscriptChunk,
    MeetingTranscript, TranscriptSegment,
)
from api.schemas.common import Pagination


def get_meetings(
    db: Session,
    school_slug: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> MeetingListResponse:
    rows, total = list_meetings(
        db, school_slug=school_slug,
        date_from=date_from, date_to=date_to,
        status=status, limit=limit, offset=offset,
    )
    return MeetingListResponse(
        meetings=[MeetingRow(**r) for r in rows],
        pagination=Pagination.build(total=total, limit=limit, offset=offset),
    )


def get_meeting(db: Session, meeting_id: int) -> Optional[MeetingOverview]:
    data = get_meeting_overview(db, meeting_id)
    if data is None:
        return None
    return MeetingOverview(
        meeting=MeetingRow(**data["meeting"]),
        votes=[VoteSummary(**v) for v in data["votes"]],
        financials=[FinancialSummary(**f) for f in data["financials"]],
        personnel=[PersonnelSummary(**p) for p in data["personnel"]],
        key_chunks=[TranscriptChunk(**c) for c in data["key_chunks"]],
    )


def get_transcript(db: Session, meeting_id: int) -> Optional[MeetingTranscript]:
    data = get_meeting_transcript(db, meeting_id)
    if data is None:
        return None
    return MeetingTranscript(
        meeting=MeetingRow(**data["meeting"]),
        segments=[TranscriptSegment(**s) for s in data["segments"]],
    )
