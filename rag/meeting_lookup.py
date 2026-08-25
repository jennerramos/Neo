"""
Neo v2 — Meeting lookup helpers for the RAG pipeline.

Used by the "latest_meeting" route to resolve phrases like
"the last HCC meeting" → a concrete meeting_id before retrieval runs.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import config

_engine = None
_Session = None


def _get_session():
    global _engine, _Session
    if _engine is None:
        _engine = create_engine(config.DATABASE_URL, echo=False)
        _Session = sessionmaker(bind=_engine)
    return _Session()


def get_latest_meeting(school_slug: Optional[str] = None) -> Optional[dict]:
    """
    Return the most recent meeting that is actually answerable.

    "Answerable" means status='indexed': only then do the meeting's chunks
    exist in Qdrant, so retrieval can ground an answer in them. This is an
    allow-list on purpose. It used to be a deny-list of failure statuses,
    which silently admitted every status added afterwards — needs_asr,
    asr_failed, archived_old, captioned — and would resolve "the last HCC
    meeting" to a meeting with no transcript at all.

    The returned dict carries an extra key, ``pending_newer``: the newest
    meeting for the same school that post-dates the indexed one but has no
    transcript yet, or None. Callers use it to disclose the coverage gap
    rather than implying the indexed meeting was the board's last.

    If school_slug is provided, scope to that school. Otherwise the latest
    across all tracked schools is returned.

    Returns None if no matching meeting exists.
    """
    session = _get_session()
    try:
        # Only 'indexed' meetings have vectors in Qdrant to retrieve against.
        sql = """
            SELECT
                m.meeting_id,
                m.video_id,
                m.title,
                m.published_date,
                m.status,
                s.slug  AS school_slug,
                s.name  AS school_name
            FROM meetings m
            JOIN schools s ON s.school_id = m.school_id
            WHERE m.status = 'indexed'
              AND m.published_date IS NOT NULL
        """
        params: dict = {}
        if school_slug:
            sql += " AND s.slug = :slug"
            params["slug"] = school_slug
        sql += " ORDER BY m.published_date DESC LIMIT 1"

        row = session.execute(text(sql), params).fetchone()
        if row is None:
            return None
        result = dict(row._mapping)
        result["pending_newer"] = _get_pending_newer(
            session, result["school_slug"], result["published_date"]
        )
        return result
    finally:
        session.close()


def _get_pending_newer(session, school_slug: str, after_date) -> Optional[dict]:
    """
    Newest meeting for ``school_slug`` that is more recent than ``after_date``
    but is not indexed — i.e. the board has met since the last meeting we can
    actually answer about.

    'archived_old' is excluded: those are deliberately out of pilot scope, not
    a coverage gap. Everything else that isn't indexed counts, including the
    terminal failures — from a trustee's point of view "we couldn't transcribe
    it" and "we haven't transcribed it yet" are the same missing meeting.
    """
    sql = """
        SELECT m.meeting_id, m.title, m.published_date, m.status
        FROM meetings m
        JOIN schools s ON s.school_id = m.school_id
        WHERE s.slug = :slug
          AND m.published_date > :after
          AND m.status NOT IN ('indexed', 'archived_old')
        ORDER BY m.published_date DESC
        LIMIT 1
    """
    row = session.execute(text(sql), {"slug": school_slug, "after": after_date}).fetchone()
    return dict(row._mapping) if row else None
