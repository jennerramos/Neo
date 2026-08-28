"""Schemas for /insights endpoints — the matrix and detail views."""
from __future__ import annotations
from typing import Optional, List, Dict
from pydantic import BaseModel


# Themes matching the matrix rows. Kept in sync with
# api/db/queries/insights.py::THEMES, which owns the category mapping.
THEMES = {
    "T1": "Academic & Student Affairs",
    "T2": "Career Services & Workforce",
    "T3": "Budget & Policy",
    "T4": "Technology & Innovation",
    "T5": "Community & Partnerships",
    "T6": "Facilities",
    "T7": "Governance & Strategy",
    "T8": "Other Business",
}

# What the page shows in place of a confidence percentage.
#   measured   — a figure was reported for this initiative
#   action     — the board approved or launched it, on the record
#   discussion — it was described or debated, nothing more
EVIDENCE_LEVELS = ("measured", "action", "discussion")


class InsightCell(BaseModel):
    insight_id:    str            # stable: "{school_slug}__{theme_key}__{slug}"
    school_slug:   str
    school_name:   str
    theme_key:     str            # "T1" … "T8"
    theme_label:   str
    label:         str            # short title shown in the cell
    action_type:   str            # "approved"|"launched"|"proposed"|"discussed"|"continued"|"cancelled"|"other"
    meeting_count: int            # distinct meetings that produced this insight

    # Dates the insight actually covers. The matrix had none at all, which let
    # a March-2021 COVID item render as "CONTINUED" in present tense.
    first_meeting_date: Optional[str] = None
    last_meeting_date:  Optional[str] = None

    # Replaces the old `confidence` float. That number was extraction
    # form-completeness (0.6 x fields populated + 0.4 x keyword density) and
    # rendered as "100% confidence", which reads as a truth claim. It is no
    # longer serialized at all so it cannot be surfaced by accident; it still
    # ranks cells inside the query layer.
    evidence_level: str           # one of EVIDENCE_LEVELS
    evidence_label: str           # human-readable, e.g. "Board action recorded"

    has_detail:    bool = True


class ThemeRow(BaseModel):
    theme_key:   str
    theme_label: str
    # school_slug → every initiative for that (theme × school), ranked
    # strongest first. Empty list = no signal. Not truncated: the page shows
    # `preview_limit` of them collapsed and the reader can open the rest.
    cells:       Dict[str, List[InsightCell]]


class SchoolCoverage(BaseModel):
    """How much recent material a college actually contributed.

    Without this a short column reads as "this college does less" when it
    usually means "we have processed fewer of its recordings".
    """
    school_slug:         str
    school_name:         str
    meeting_count:       int
    # The most recent meeting this college HELD in the window, not the most
    # recent upload — see api/db/queries/insights.py::resolve_held_date.
    latest_meeting_date: Optional[str] = None
    insight_count:       int


class InsightMatrix(BaseModel):
    school_slugs:  List[str]
    school_names:  Dict[str, str]          # slug → display name
    themes:        List[ThemeRow]
    generated_at:  str                     # ISO timestamp

    # The rolling window the page covers, so it can say so on screen. Measured
    # on the date the board met, not the date the recording was uploaded.
    window_start:  str
    window_end:    str
    window_months: int

    # How many insights a cell shows collapsed. A display default, not a
    # filter — every insight in the window is delivered.
    preview_limit:   int

    insight_count:   int                   # insights delivered
    available_count: int                   # distinct insights in the window
    coverage:        List[SchoolCoverage]


class EvidenceChunk(BaseModel):
    text:          str
    speaker:       Optional[str] = None
    timestamp_sec: Optional[float] = None
    meeting_title: Optional[str] = None
    meeting_date:  Optional[str] = None
    meeting_id:    Optional[int] = None

    # Source-chunk provenance (migration 0006). `verified=True` means the text
    # was located character-for-character in `chunk_id`, which is what makes
    # the transcript deep-link on the page trustworthy. `supports` names the
    # claims this quote backs.
    chunk_id:      Optional[str] = None
    supports:      List[str] = []
    verified:      bool = False


class TimelineStep(BaseModel):
    """One meeting's contribution to an insight, oldest first.

    The card shows a single action state for the whole item — the strongest
    row folded into it — which cannot show whether something was proposed and
    then approved or merely discussed three times.
    """
    meeting_id:  int
    date:        str
    title:       Optional[str] = None
    action_type: str


class SupportingMeeting(BaseModel):
    meeting_id:    int
    title:         Optional[str] = None
    date:          Optional[str] = None
    school_slug:   str


class PeerCell(BaseModel):
    """Same theme, different school — for comparison hints."""
    school_slug:    str
    school_name:    str
    label:          str
    action_type:    str
    evidence_level: str
    evidence_label: str
    insight_id:     str


class InsightDetail(BaseModel):
    insight_id:          str
    school_slug:         str
    school_name:         str
    theme_key:           str
    theme_label:         str
    label:               str
    action_type:         str

    evidence_level:      str
    evidence_label:      str
    evidence_note:       str

    meeting_count:       int
    first_meeting_date:  Optional[str] = None
    last_meeting_date:   Optional[str] = None

    # The extractor keeps four evidence levels deliberately apart
    # (pipeline/initiative_extractor.py). They stay apart here: a claimed
    # outcome is a prediction the college made, and must not read as a result.
    summary:             str
    description:         Optional[str] = None
    observed_action:     Optional[str] = None
    stated_rationale:    Optional[str] = None
    claimed_outcome:     Optional[str] = None
    measured_outcome:    Optional[str] = None

    why_it_appears:      str

    # How the item moved, meeting by meeting. Single-meeting insights get a
    # one-step timeline; the UI does not render a progression for those.
    timeline:            List[TimelineStep] = []

    supporting_meetings: List[SupportingMeeting]
    evidence:            List[EvidenceChunk]

    # Rows quoted from the same transcript passage as this insight — not
    # everything in the theme. Empty is the normal case and means the passage
    # cited no vote or dollar figure.
    related_votes:       List[dict]
    related_financials:  List[dict]

    peer_cells:          List[PeerCell]   # same theme, other schools
