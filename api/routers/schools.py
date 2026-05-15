"""Schools router — list all colleges."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import text

from api.db.session import get_db
from api.schemas.common import School

router = APIRouter(prefix="/schools", tags=["schools"])


@router.get("", response_model=list[School])
def list_schools(db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT school_id, slug, name, website, state
        FROM schools WHERE is_active = TRUE ORDER BY name
    """)).fetchall()
    return [School(**dict(r._mapping)) for r in rows]
