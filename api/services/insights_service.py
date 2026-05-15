"""Service layer for insights — matrix + detail."""
from __future__ import annotations
from typing import Optional
from sqlalchemy.orm import Session

from api.db.queries.insights import get_insight_matrix, get_insight_detail
from api.schemas.insights import (
    InsightMatrix, ThemeRow, InsightCell,
    InsightDetail, EvidenceChunk, SupportingMeeting, PeerCell,
)


def build_matrix(db: Session) -> InsightMatrix:
    data = get_insight_matrix(db)

    theme_rows = []
    for t in data["themes"]:
        # Each (school × theme) cell is a list of up to 3 initiatives,
        # ranked by confidence then meeting_count. Empty list = no signal.
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
        summary=data["summary"],
        why_it_appears=data["why_it_appears"],
        confidence=data["confidence"],
        supporting_meetings=[SupportingMeeting(**m) for m in data["supporting_meetings"]],
        evidence=[EvidenceChunk(**e) for e in data["evidence"]],
        related_votes=data["related_votes"],
        related_financials=data["related_financials"],
        related_personnel=data["related_personnel"],
        peer_cells=[PeerCell(**p) for p in data["peer_cells"]],
    )
