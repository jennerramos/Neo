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
from api.db.queries.insights import get_insight_matrix

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


@router.get("/insights.csv")
def export_insights_csv(db: Session = Depends(get_db)):
    """The Insights matrix, flattened one row per item, for board packets.

    Built from get_insight_matrix so the export and the page cannot disagree
    about the rolling window, the meeting eligibility or the ranking. It
    carries the whole window, not the three items a cell shows collapsed.

    `confidence` is deliberately absent. It measured how completely the
    extractor filled a form, not whether a claim holds, and putting it in a
    spreadsheet is how it would come back — a number in a CSV column reads as
    a measurement. `evidence_level` is the defensible version.
    """
    matrix = get_insight_matrix(db)
    rows = [
        {**cell, "window_start": matrix["window_start"],
                 "window_end":   matrix["window_end"]}
        for theme in matrix["themes"]
        for cells in theme["cells"].values()
        for cell in cells
    ]
    headers = [
        "insight_id", "school_slug", "school_name",
        "theme_key", "theme_label", "label", "action_type",
        "evidence_level", "evidence_label", "meeting_count",
        "first_meeting_date", "last_meeting_date",
        "window_start", "window_end",
    ]
    return _stream_csv(headers, rows)
