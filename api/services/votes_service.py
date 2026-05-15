"""Service layer for votes."""
from __future__ import annotations
from typing import Optional
from sqlalchemy.orm import Session

from api.db.queries.votes import list_votes, get_votes_summary
from api.schemas.votes import VoteRow, VoteListResponse
from api.schemas.common import Pagination


def get_votes(
    db: Session,
    school_slug: Optional[str] = None,
    meeting_id: Optional[int] = None,
    passed: Optional[bool] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> VoteListResponse:
    rows, total = list_votes(
        db, school_slug=school_slug, meeting_id=meeting_id,
        passed=passed, date_from=date_from, date_to=date_to,
        limit=limit, offset=offset,
    )
    return VoteListResponse(
        votes=[VoteRow(**r) for r in rows],
        pagination=Pagination.build(total=total, limit=limit, offset=offset),
    )


def get_summary(
    db: Session,
    school_slug: Optional[str] = None,
    date_from: Optional[str]   = None,
    date_to: Optional[str]     = None,
) -> dict:
    return get_votes_summary(db, school_slug=school_slug, date_from=date_from, date_to=date_to)
