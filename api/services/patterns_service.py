"""Service layer for pattern signals."""
from __future__ import annotations
from typing import Optional
from sqlalchemy.orm import Session

from api.db.queries.patterns import (
    get_pattern_detail, get_pattern_evidence, get_patterns_summary, list_patterns,
)
from api.schemas.patterns import (
    PatternDetail, PatternEvidence, PatternListResponse, PatternRow,
    PatternSchool, SupportingInitiative,
)
from api.schemas.common import Pagination

# How many quotations to attach to a detail response. Signals can span a
# hundred initiatives; the detail view wants a readable sample, not the corpus.
DETAIL_EVIDENCE_LIMIT = 12


def get_patterns(
    db: Session,
    signal_type:  Optional[str] = None,
    category:     Optional[str] = None,
    needs_review: Optional[bool] = None,
    min_schools:  Optional[int] = None,
    limit:        int = 50,
    offset:       int = 0,
) -> PatternListResponse:
    rows, total = list_patterns(
        db, signal_type=signal_type, category=category,
        needs_review=needs_review, min_schools=min_schools,
        limit=limit, offset=offset,
    )
    return PatternListResponse(
        patterns=[PatternRow(**r) for r in rows],
        pagination=Pagination.build(total=total, limit=limit, offset=offset),
    )


def get_detail(db: Session, signal_id: int) -> Optional[PatternDetail]:
    data = get_pattern_detail(db, signal_id)
    if data is None:
        return None

    evidence = get_pattern_evidence(db, signal_id, limit=DETAIL_EVIDENCE_LIMIT) or []

    return PatternDetail(
        **{k: v for k, v in data.items()
           if k not in ("schools", "supporting_initiatives", "evidence")},
        schools=[PatternSchool(**s) for s in data["schools"]],
        supporting_initiatives=[
            SupportingInitiative(**i) for i in data["supporting_initiatives"]
        ],
        evidence=[PatternEvidence(**e) for e in evidence],
    )


def get_evidence(
    db: Session, signal_id: int, limit: int = 20
) -> Optional[list[PatternEvidence]]:
    rows = get_pattern_evidence(db, signal_id, limit=limit)
    if rows is None:
        return None
    return [PatternEvidence(**r) for r in rows]


def get_summary(db: Session) -> dict:
    return get_patterns_summary(db)
