"""DB queries for meetings endpoints."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
import config


def list_meetings(
    db: Session,
    school_slug: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
    limit: int,
    offset: int,
) -> tuple[list[dict], int]:
    filters = []
    params: dict = {"limit": limit, "offset": offset}

    if school_slug:
        filters.append("s.slug = :school")
        params["school"] = school_slug
    if date_from:
        filters.append("m.published_date >= CAST(:df AS date)")
        params["df"] = date_from
    if date_to:
        filters.append("m.published_date <= CAST(:dt AS date)")
        params["dt"] = date_to
    if status:
        filters.append("m.status = :status")
        params["status"] = status

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    count_sql = text(f"""
        SELECT COUNT(*) FROM meetings m
        JOIN schools s ON s.school_id = m.school_id
        {where}
    """)
    total = db.execute(count_sql, params).scalar()

    rows_sql = text(f"""
        SELECT
            m.meeting_id, m.video_id, s.slug AS school_slug, s.name AS school_name,
            m.title, m.published_date, m.status, m.source_type,
            m.duration_seconds, m.word_count, m.quality_score
        FROM meetings m
        JOIN schools s ON s.school_id = m.school_id
        {where}
        ORDER BY m.published_date DESC NULLS LAST
        LIMIT :limit OFFSET :offset
    """)
    rows = db.execute(rows_sql, params).fetchall()
    return [dict(r._mapping) for r in rows], total


def get_meeting_overview(db: Session, meeting_id: int) -> Optional[dict]:
    # Core meeting
    m = db.execute(text("""
        SELECT m.*, s.slug AS school_slug, s.name AS school_name
        FROM meetings m JOIN schools s ON s.school_id = m.school_id
        WHERE m.meeting_id = :mid
    """), {"mid": meeting_id}).fetchone()
    if not m:
        return None
    meeting = dict(m._mapping)

    # Votes
    votes = db.execute(text("""
        SELECT vote_id, motion_text, vote_result_text,
               yes_count, no_count, abstain_count, passed, unanimous
        FROM votes WHERE meeting_id = :mid AND needs_review = FALSE
        ORDER BY vote_id
    """), {"mid": meeting_id}).fetchall()

    # Financials
    fins = db.execute(text("""
        SELECT item_id, action_type, category, vendor, amount, description
        FROM financial_items WHERE meeting_id = :mid AND needs_review = FALSE
        ORDER BY item_id
    """), {"mid": meeting_id}).fetchall()

    # Personnel
    pers = db.execute(text("""
        SELECT action_id, action_type, person_name, position, department, is_interim
        FROM personnel_actions WHERE meeting_id = :mid AND needs_review = FALSE
        ORDER BY action_id
    """), {"mid": meeting_id}).fetchall()

    # Key transcript chunks (top 5 by quality_score)
    chunks = []
    school_slug = meeting.get("school_slug", "")
    video_id    = meeting.get("video_id", "")
    jsonl_path  = config.PROCESSED_DIR / school_slug / video_id / "chunks.jsonl"
    if jsonl_path.exists():
        all_chunks = []
        with open(jsonl_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    all_chunks.append(json.loads(line))
        top = sorted(all_chunks, key=lambda c: c.get("quality_score", 0), reverse=True)[:5]
        chunks = [
            {
                "chunk_id":    c.get("chunk_id", ""),
                "speaker":     c.get("speaker"),
                "start_time":  c.get("start_time"),
                "text":        c.get("text", ""),
                "quality_score": c.get("quality_score"),
            }
            for c in top
        ]

    return {
        "meeting":    meeting,
        "votes":      [dict(r._mapping) for r in votes],
        "financials": [dict(r._mapping) for r in fins],
        "personnel":  [dict(r._mapping) for r in pers],
        "key_chunks": chunks,
    }


def get_meeting_transcript(db: Session, meeting_id: int) -> Optional[dict]:
    """Return the meeting header + every chunk in order for /meetings/{id}/transcript."""
    m = db.execute(text("""
        SELECT m.*, s.slug AS school_slug, s.name AS school_name
        FROM meetings m JOIN schools s ON s.school_id = m.school_id
        WHERE m.meeting_id = :mid
    """), {"mid": meeting_id}).fetchone()
    if not m:
        return None
    meeting = dict(m._mapping)

    school_slug = meeting.get("school_slug", "")
    video_id    = meeting.get("video_id", "")
    jsonl_path  = config.PROCESSED_DIR / school_slug / video_id / "chunks.jsonl"

    segments: list[dict] = []
    if jsonl_path.exists():
        with open(jsonl_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                c = json.loads(line)
                segments.append({
                    "chunk_id":      c.get("chunk_id", ""),
                    "chunk_index":   c.get("chunk_index"),
                    "speaker":       c.get("speaker"),
                    "start_time":    c.get("start_time"),
                    "end_time":      c.get("end_time"),
                    "text":          c.get("text", ""),
                    "token_count":   c.get("token_count"),
                    "quality_score": c.get("quality_score"),
                })
        segments.sort(key=lambda s: (s.get("chunk_index") or 0, s.get("start_time") or 0))

    return {"meeting": meeting, "segments": segments}
