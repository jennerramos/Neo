"""Schemas for /patterns endpoints — cross-college pattern signals.

A pattern signal is an aggregate produced by pipeline/pattern_builder.py: the
same initiative category, financial category, or personnel action recurring
across several colleges.  Signals are the only place Neo makes a claim that
spans institutions, so the schema deliberately carries the caveats with the
claim rather than leaving them to the caller:

  - `needs_review` is the gate.  For initiative signals it means measured
    outcomes came from fewer than two different schools; for budget and
    personnel signals it means fewer than two schools at all.
  - `traceable` says whether the signal can be expanded to the underlying
    rows.  Only initiative signals carry supporting_initiative_ids today.
"""
from __future__ import annotations
from datetime import date
from typing import Optional, List
from pydantic import BaseModel

from .common import Pagination


class PatternRow(BaseModel):
    signal_id:         int
    signal_type:       str            # "recurring_initiative"|"budget_trend"|"personnel_trend"
    category:          str
    description:       str
    school_count:      int
    meeting_count:     int
    first_observed_date: Optional[date] = None
    last_observed_date:  Optional[date] = None
    confidence:        float = 0.0
    needs_review:      bool  = True
    extractor_version: Optional[str] = None
    # True when the signal links back to specific rows the reader can inspect.
    traceable:         bool  = False
    supporting_count:  int   = 0

    model_config = {"from_attributes": True}


class PatternListResponse(BaseModel):
    patterns:   List[PatternRow]
    pagination: Pagination


class PatternSchool(BaseModel):
    """A school contributing to a signal, derived from its supporting rows."""
    school_slug:      str
    school_name:      Optional[str] = None
    initiative_count: int
    measured_count:   int           # how many carry a measured outcome


class SupportingInitiative(BaseModel):
    initiative_id:   int
    school_slug:     str
    school_name:     Optional[str] = None
    meeting_id:      int
    meeting_title:   Optional[str] = None
    published_date:  Optional[date] = None
    initiative_name: Optional[str] = None
    category:        Optional[str] = None
    observed_action: Optional[str] = None
    measured_outcome: Optional[str] = None
    confidence:      float = 0.0
    evidence_count:  int = 0


class PatternEvidence(BaseModel):
    """A verified quotation behind a signal.

    Every field here comes from extraction_evidence (migration 0006), meaning
    the quote was located character-for-character inside the named chunk.  A
    reader can open that meeting at `timestamp_sec` and hear it.
    """
    initiative_id: int
    chunk_id:      str
    text:          str
    supports:      List[str] = []
    timestamp_sec: Optional[float] = None
    meeting_id:    Optional[int] = None
    meeting_title: Optional[str] = None
    school_slug:   Optional[str] = None
    verified:      bool = True


class PatternDetail(PatternRow):
    schools:                List[PatternSchool] = []
    supporting_initiatives: List[SupportingInitiative] = []
    evidence:               List[PatternEvidence] = []
    # Set when the signal type cannot be expanded, so the UI can say why the
    # lists above are empty instead of rendering a blank panel.
    trace_note:             Optional[str] = None


class PatternTypeStat(BaseModel):
    signal_type:   str
    total:         int
    trustee_ready: int


class PatternsStats(BaseModel):
    """Headline stats for a patterns dashboard."""
    total:           int
    trustee_ready:   int
    needs_review:    int
    by_type:         List[PatternTypeStat]
    categories:      int
    max_school_count: int
    extractor_versions: List[str]
