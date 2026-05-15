"""Schemas for /insights endpoints — the matrix and detail views."""
from __future__ import annotations
from typing import Optional, List, Dict
from pydantic import BaseModel


# Themes matching the matrix rows
THEMES = {
    "T1": "Academic & Student Affairs",
    "T2": "Career Services & Workforce",
    "T3": "Budget & Policy",
    "T4": "Technology & Innovation",
    "T5": "Community & Recognition",
    "T6": "Personnel & Facilities",
    "T7": "Procedural Items",
}


class InsightCell(BaseModel):
    insight_id:    str            # stable: "{school_slug}__{theme_key}__{slug}"
    school_slug:   str
    school_name:   str
    theme_key:     str            # "T1" … "T7"
    theme_label:   str
    label:         str            # short title shown in the cell
    action_type:   str            # "approved"|"launched"|"proposed"|"discussed"|"continued"
    meeting_count: int
    confidence:    float
    has_detail:    bool = True    # whether a detail page exists


class ThemeRow(BaseModel):
    theme_key:   str
    theme_label: str
    # school_slug → list of initiatives for that (theme × school). Empty list = no signal.
    # Up to 3 cells per (school × theme) to surface related initiatives in one place.
    cells:       Dict[str, List[InsightCell]]


class InsightMatrix(BaseModel):
    school_slugs:  List[str]
    school_names:  Dict[str, str]          # slug → display name
    themes:        List[ThemeRow]
    generated_at:  str                     # ISO timestamp


class EvidenceChunk(BaseModel):
    text:          str
    speaker:       Optional[str] = None
    timestamp_sec: Optional[float] = None
    meeting_title: Optional[str] = None
    meeting_date:  Optional[str] = None
    meeting_id:    Optional[int] = None
    score:         Optional[float] = None


class SupportingMeeting(BaseModel):
    meeting_id:    int
    title:         Optional[str] = None
    date:          Optional[str] = None
    school_slug:   str


class PeerCell(BaseModel):
    """Same theme, different school — for comparison hints."""
    school_slug:   str
    school_name:   str
    label:         str
    action_type:   str
    insight_id:    str


class InsightDetail(BaseModel):
    insight_id:          str
    school_slug:         str
    school_name:         str
    theme_key:           str
    theme_label:         str
    label:               str
    action_type:         str
    summary:             str
    why_it_appears:      str
    confidence:          float

    supporting_meetings: List[SupportingMeeting]
    evidence:            List[EvidenceChunk]
    related_votes:       List[dict]
    related_financials:  List[dict]
    related_personnel:   List[dict]
    peer_cells:          List[PeerCell]   # same theme, other schools
