from __future__ import annotations
from datetime import date
from typing import Optional, List
from pydantic import BaseModel
from .common import Pagination


class VoteRow(BaseModel):
    vote_id:          int
    school_slug:      str
    school_name:      Optional[str] = None
    meeting_id:       int
    meeting_title:    Optional[str] = None
    # DB returns a date; Pydantic serializes it as ISO "YYYY-MM-DD" in responses,
    # which matches the TypeScript `string | null` contract.
    published_date:   Optional[date] = None
    motion_text:      Optional[str] = None
    vote_result_text: Optional[str] = None
    yes_count:        Optional[int] = None
    no_count:         Optional[int] = None
    abstain_count:    Optional[int] = None
    passed:           Optional[bool] = None
    unanimous:        Optional[bool] = None
    moved_by:         Optional[str] = None
    confidence:       float = 0.0

    # Twin of database.models.Vote — kept ORM-validatable for consistency
    # with MeetingRow / FinancialRow. See refactor_candidates.md #4.
    model_config = {"from_attributes": True}


class VoteListResponse(BaseModel):
    votes:      List[VoteRow]
    pagination: Pagination


class TopMover(BaseModel):
    name: str
    cnt:  int


class VotesStats(BaseModel):
    """Page-level aggregate stats returned by /votes/summary.

    Mirrors `VotesStats` in frontend/src/types/index.ts. `pass_rate` is over
    decided motions only (passed+failed) so abstention-heavy votes don't
    artificially deflate the number.
    """
    total:          int
    passed:         int
    failed:         int
    unanimous:      int
    pass_rate:      Optional[float] = None
    unanimous_rate: Optional[float] = None
    top_movers:     List[TopMover]
