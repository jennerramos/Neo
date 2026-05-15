"""Schemas for /meetings endpoints."""
from __future__ import annotations
from typing import Optional, List
from datetime import date
from pydantic import BaseModel
from .common import Pagination, pick
from .votes import VoteRow
from .financials import FinancialRow


class MeetingRow(BaseModel):
    # Convention: every Pydantic schema that twins a SQLAlchemy ORM model
    # (MeetingRow↔Meeting, VoteRow↔Vote, FinancialRow↔FinancialItem, plus
    # School and Pagination) sets `from_attributes=True`. Today every call
    # site builds these via `Model(**dict)` from raw-SQL dicts, so the flag
    # is dormant — but having it on uniformly means a future caller can
    # switch to ORM-mode (`MeetingRow.model_validate(orm_obj)`) without
    # touching the schema. See refactor_candidates.md #4.
    meeting_id:      int
    video_id:        str
    school_slug:     str
    school_name:     Optional[str] = None
    title:           Optional[str] = None
    published_date:  Optional[date] = None
    status:          str
    source_type:     Optional[str] = None
    duration_seconds: Optional[float] = None
    word_count:      Optional[int] = None
    quality_score:   Optional[float] = None

    model_config = {"from_attributes": True}


class MeetingListResponse(BaseModel):
    meetings:   List[MeetingRow]
    pagination: Pagination


# MeetingOverview nests projections of the per-row schemas.  Deriving them with
# `pick()` keeps a single source of truth: if VoteRow renames a field, this
# import will fail-fast rather than silently shipping a stale TS contract.
# See refactor_candidates.md #2.
VoteSummary = pick("VoteSummary", VoteRow, (
    "vote_id", "motion_text", "vote_result_text",
    "yes_count", "no_count", "passed", "unanimous",
))

FinancialSummary = pick("FinancialSummary", FinancialRow, (
    "item_id", "action_type", "category",
    "vendor",  "amount",      "description",
))


class PersonnelSummary(BaseModel):
    action_id:    int
    action_type:  Optional[str] = None
    person_name:  Optional[str] = None
    position:     Optional[str] = None
    department:   Optional[str] = None
    is_interim:   Optional[bool] = None


class TranscriptChunk(BaseModel):
    chunk_id:    str
    speaker:     Optional[str] = None
    start_time:  Optional[float] = None
    text:        str
    quality_score: Optional[float] = None


class MeetingOverview(BaseModel):
    meeting:     MeetingRow
    votes:       List[VoteSummary]
    financials:  List[FinancialSummary]
    personnel:   List[PersonnelSummary]
    key_chunks:  List[TranscriptChunk]   # top 5 highest-quality chunks


class TranscriptSegment(BaseModel):
    chunk_id:      str
    chunk_index:   Optional[int] = None
    speaker:       Optional[str] = None
    start_time:    Optional[float] = None
    end_time:      Optional[float] = None
    text:          str
    token_count:   Optional[int] = None
    quality_score: Optional[float] = None


class MeetingTranscript(BaseModel):
    meeting:  MeetingRow
    segments: List[TranscriptSegment]
