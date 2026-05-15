from __future__ import annotations
from datetime import date
from typing import Optional, List
from pydantic import BaseModel
from .common import Pagination


class FinancialRow(BaseModel):
    item_id:        int
    school_slug:    str
    school_name:    Optional[str] = None
    meeting_id:     int
    meeting_title:  Optional[str] = None
    # DB returns a date; Pydantic serializes it as ISO "YYYY-MM-DD" in responses,
    # which matches the TypeScript `string | null` contract.
    published_date: Optional[date] = None
    action_type:    Optional[str] = None
    category:       Optional[str] = None
    vendor:         Optional[str] = None
    amount:         Optional[float] = None
    description:    Optional[str] = None
    confidence:     float = 0.0

    # Twin of database.models.FinancialItem — kept ORM-validatable for
    # consistency with MeetingRow / VoteRow. See refactor_candidates.md #4.
    model_config = {"from_attributes": True}


class FinancialListResponse(BaseModel):
    items:      List[FinancialRow]
    pagination: Pagination


class ActionTypeBucket(BaseModel):
    action_type: Optional[str] = None
    cnt:         int
    total:       Optional[float] = None


class VendorBucket(BaseModel):
    vendor: Optional[str] = None
    cnt:    int
    total:  Optional[float] = None


class LargestItem(BaseModel):
    item_id:        int
    amount:         Optional[float] = None
    action_type:    Optional[str] = None
    vendor:         Optional[str] = None
    description:    Optional[str] = None
    meeting_id:     int
    published_date: Optional[date] = None
    meeting_title:  Optional[str] = None
    school_slug:    str
    school_name:    Optional[str] = None


class FinancialsStats(BaseModel):
    """Page-level aggregate stats returned by /financials/summary.

    Mirrors `FinancialsStats` in frontend/src/types/index.ts. The single
    largest item is surfaced as a card so trustees can spot outliers — a
    $20M line item says more about priorities than a category roll-up.
    """
    by_action_type: List[ActionTypeBucket]
    top_vendors:    List[VendorBucket]
    largest_item:   Optional[LargestItem] = None
