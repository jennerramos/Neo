"""Schemas for POST /ask."""
from __future__ import annotations
from typing import Optional, List
from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    query:        str              = Field(..., min_length=3, max_length=1000)
    school_slug:  Optional[str]   = None
    date_from:    Optional[str]   = None   # YYYY-MM-DD
    date_to:      Optional[str]   = None   # YYYY-MM-DD
    top_k:        int             = Field(default=8, ge=1, le=20)
    force_route:  Optional[str]   = Field(default=None, pattern="^(sql|rag|hybrid|compare)$")


class Citation(BaseModel):
    type:       str              # "sql" | "rag"
    index:      Optional[int]   = None
    label:      Optional[str]   = None   # SQL citations only
    title:      Optional[str]   = None   # RAG citations only
    date:       Optional[str]   = None
    speaker:    Optional[str]   = None
    chunk_id:   Optional[str]   = None
    meeting_id: Optional[int]   = None   # RAG citations — enables click-through
    score:      Optional[float] = None


class AskResponse(BaseModel):
    answer:       str
    route:        str              # "sql" | "rag" | "hybrid" | "compare" | "latest_meeting" | "none"
    citations:    List[Citation]
    model:        str
    elapsed_sec:  float
    intent:       Optional[str]   = None

    # Populated when route == "latest_meeting" so the UI can render
    # a meeting header + "View full meeting →" link.
    meeting_id:    Optional[int]  = None
    meeting_title: Optional[str]  = None
    meeting_date:  Optional[str]  = None
    school_slug:   Optional[str]  = None
    school_name:   Optional[str]  = None
