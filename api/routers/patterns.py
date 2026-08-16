"""Patterns router — cross-college pattern signals.

Route order matters here: "/summary" is declared before "/{signal_id}" so the
literal path wins. Registered the other way round, FastAPI would try to parse
"summary" as an int and answer 422.
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from api.db.session import get_db
from api.schemas.patterns import (
    PatternDetail, PatternEvidence, PatternListResponse, PatternsStats,
)
from api.services.patterns_service import (
    get_detail, get_evidence, get_patterns, get_summary,
)

router = APIRouter(prefix="/patterns", tags=["patterns"])


@router.get("", response_model=PatternListResponse)
def list_pattern_signals(
    signal_type:  Optional[str]  = Query(
        None, description="recurring_initiative | budget_trend | personnel_trend"),
    category:     Optional[str]  = Query(None, description="Exact category match"),
    needs_review: Optional[bool] = Query(
        None, description="false = corroborated enough to show trustees"),
    min_schools:  Optional[int]  = Query(
        None, ge=1, description="Only signals spanning at least this many schools"),
    limit:        int = Query(50, ge=1, le=200),
    offset:       int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Cross-college pattern signals, trustee-ready first then by confidence.

    Signals take no school or date filter: each one is an aggregate across
    institutions, so narrowing the inputs without re-aggregating would leave
    school_count describing a different population than the row.
    """
    return get_patterns(
        db, signal_type=signal_type, category=category,
        needs_review=needs_review, min_schools=min_schools,
        limit=limit, offset=offset,
    )


@router.get("/summary", response_model=PatternsStats)
def pattern_summary(db: Session = Depends(get_db)):
    """Headline counts — totals, trustee-ready split, and per-type breakdown."""
    return get_summary(db)


@router.get("/{signal_id}", response_model=PatternDetail)
def pattern_detail(signal_id: int, db: Session = Depends(get_db)):
    """One signal expanded into its schools, initiatives, and verified quotes.

    signal_id is only valid for the current build: pattern_signals is dropped
    and rebuilt by pipeline/pattern_builder.py, so ids are not durable
    bookmarks.
    """
    result = get_detail(db, signal_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Pattern signal not found")
    return result


@router.get("/{signal_id}/evidence", response_model=list[PatternEvidence])
def pattern_evidence(
    signal_id: int,
    limit: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Verified source-chunk quotations behind a signal.

    Every row was located character-for-character in the named chunk, so a
    reader can open the meeting at `timestamp_sec` and hear it. An empty list
    means the signal has no traceable rows (budget and personnel signals do
    not record supporting ids) — not that the evidence check failed.
    """
    result = get_evidence(db, signal_id, limit=limit)
    if result is None:
        raise HTTPException(status_code=404, detail="Pattern signal not found")
    return result
