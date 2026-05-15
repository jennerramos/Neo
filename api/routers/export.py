"""Export router — CSV/JSON downloads for trustees."""
from typing import Optional
import csv
import io
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from api.db.session import get_db
from api.db.queries.votes import list_votes
from api.db.queries.financials import list_financials

router = APIRouter(prefix="/export", tags=["export"])


def _stream_csv(headers: list[str], rows: list[dict]) -> StreamingResponse:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="export.csv"'},
    )


@router.get("/votes.csv")
def export_votes_csv(
    school:    Optional[str]  = Query(None),
    date_from: Optional[str]  = Query(None),
    date_to:   Optional[str]  = Query(None),
    passed:    Optional[bool] = Query(None),
    db: Session = Depends(get_db),
):
    rows, _ = list_votes(
        db, school_slug=school, meeting_id=None,
        passed=passed, date_from=date_from, date_to=date_to,
        limit=5000, offset=0,
    )
    headers = [
        "vote_id", "school_slug", "school_name", "meeting_id",
        "meeting_title", "published_date", "motion_text",
        "vote_result_text", "yes_count", "no_count", "abstain_count",
        "passed", "unanimous", "moved_by", "confidence",
    ]
    return _stream_csv(headers, rows)


@router.get("/financials.csv")
def export_financials_csv(
    school:      Optional[str]   = Query(None),
    action_type: Optional[str]   = Query(None),
    date_from:   Optional[str]   = Query(None),
    date_to:     Optional[str]   = Query(None),
    db: Session = Depends(get_db),
):
    rows, _ = list_financials(
        db, school_slug=school, meeting_id=None,
        action_type=action_type, category=None,
        amount_min=None, amount_max=None,
        date_from=date_from, date_to=date_to,
        limit=5000, offset=0,
    )
    headers = [
        "item_id", "school_slug", "school_name", "meeting_id",
        "meeting_title", "published_date", "action_type",
        "category", "vendor", "amount", "description", "confidence",
    ]
    return _stream_csv(headers, rows)
