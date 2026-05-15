"""Insights router — matrix + detail."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.db.session import get_db
from api.services.insights_service import build_matrix, get_detail
from api.schemas.insights import InsightMatrix, InsightDetail

router = APIRouter(prefix="/insights", tags=["insights"])


@router.get("/matrix", response_model=InsightMatrix)
def insight_matrix(db: Session = Depends(get_db)):
    """Cross-college initiative matrix — rows = themes, cols = schools."""
    return build_matrix(db)


@router.get("/detail/{insight_id}", response_model=InsightDetail)
def insight_detail(insight_id: str, db: Session = Depends(get_db)):
    """Full detail for a single insight cell."""
    result = get_detail(db, insight_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Insight not found")
    return result
