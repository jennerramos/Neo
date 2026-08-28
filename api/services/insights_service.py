"""Service layer for insights — matrix + detail."""
from __future__ import annotations
from typing import Optional
from sqlalchemy.orm import Session

from api.db.queries.insights import get_insight_matrix, get_insight_detail
from api.schemas.insights import (
    InsightMatrix, ThemeRow, InsightCell, SchoolCoverage,
    InsightDetail, EvidenceChunk, SupportingMeeting, PeerCell,
)


def build_matrix(db: Session) -> InsightMatrix:
    data = get_insight_matrix(db)

    theme_rows = []
    for t in data["themes"]:
        # Each (school × theme) cell is a list of up to 3 initiatives,
        # ranked by evidence strength then recency. Empty list = no signal
        # in the rolling window.
        cells: dict = {}
        for slug, cell_list in t["cells"].items():
            cells[slug] = [InsightCell(**c) for c in (cell_list or [])]
        theme_rows.append(ThemeRow(
            theme_key=t["theme_key"],
            theme_label=t["theme_label"],
            cells=cells,
        ))

    return InsightMatrix(
        school_slugs=data["school_slugs"],
        school_names=data["school_names"],
        themes=theme_rows,
        generated_at=data["generated_at"],
        window_start=data["window_start"],
        window_end=data["window_end"],
        window_months=data["window_months"],
        insight_count=data["insight_count"],
        available_count=data["available_count"],
        coverage=[SchoolCoverage(**c) for c in data["coverage"]],
    )


def get_detail(db: Session, insight_id: str) -> Optional[InsightDetail]:
    data = get_insight_detail(db, insight_id)
    if data is None:
        return None
    return InsightDetail(
        insight_id=data["insight_id"],
        school_slug=data["school_slug"],
        school_name=data["school_name"],
        theme_key=data["theme_key"],
        theme_label=data["theme_label"],
        label=data["label"],
        action_type=data["action_type"],
        evidence_level=data["evidence_level"],
        evidence_label=data["evidence_label"],
        evidence_note=data["evidence_note"],
        meeting_count=data["meeting_count"],
        first_meeting_date=data["first_meeting_date"],
        last_meeting_date=data["last_meeting_date"],
        summary=data["summary"],
        description=data["description"],
        observed_action=data["observed_action"],
        stated_rationale=data["stated_rationale"],
        claimed_outcome=data["claimed_outcome"],
        measured_outcome=data["measured_outcome"],
        why_it_appears=data["why_it_appears"],
        supporting_meetings=[SupportingMeeting(**m) for m in data["supporting_meetings"]],
        evidence=[EvidenceChunk(**e) for e in data["evidence"]],
        related_votes=data["related_votes"],
        related_financials=data["related_financials"],
        peer_cells=[PeerCell(**p) for p in data["peer_cells"]],
    )
