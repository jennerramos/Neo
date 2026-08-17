"""DB queries for meetings endpoints."""
from __future__ import annotations
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text

# json / config / the sys.path hack are gone with the last chunks.jsonl read:
# transcript text now comes from the `chunks` table, so this module no longer
# reaches into data/ and no longer needs the repo root on sys.path.


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

    # Key transcript chunks (top 5 by quality_score). From the `chunks` table
    # for the same reason as get_meeting_transcript below: the JSONL under
    # data/ is workstation-only and absent from any container, so this silently
    # returned an empty highlights list everywhere but a dev machine.
    key = db.execute(text("""
        SELECT chunk_id, speaker, start_time, text, quality_score
        FROM chunks
        WHERE meeting_id = :mid
        ORDER BY quality_score DESC NULLS LAST, chunk_index
        LIMIT 5
    """), {"mid": meeting_id}).fetchall()
    chunks = [dict(r._mapping) for r in key]

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

    # Segments come from the `chunks` table, not from
    # PROCESSED_DIR/<school>/<video_id>/chunks.jsonl as they used to.
    #
    # The JSONL lives in data/, which is workstation-only: it is gigabytes of
    # audio and transcripts, it is in .dockerignore, and it is not part of the
    # pg_dump that seeds the VPS. So the file read returned zero segments for
    # every meeting in a container, and the UI showed "Transcript isn't
    # available for this meeting yet" on every citation click-through -- the
    # main path from an answer back to its source.
    #
    # Postgres already holds the same text (12,769 rows across 534 meetings),
    # written by the same indexer pass, and it travels with the database.
    rows = db.execute(text("""
        SELECT chunk_id, chunk_index, speaker, start_time, end_time,
               text, token_count, quality_score
        FROM chunks
        WHERE meeting_id = :mid
        ORDER BY chunk_index, start_time
    """), {"mid": meeting_id}).fetchall()

    segments = [dict(r._mapping) for r in rows]

    return {"meeting": meeting, "segments": segments}
