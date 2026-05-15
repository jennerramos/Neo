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
    Return the most recent successfully processed meeting.

    If school_slug is provided, scope to that school. Otherwise the latest
    across all tracked schools is returned. Excludes meetings that failed
    the pipeline (rejected, failed, etc.) — only meetings with extracted
    transcripts and structured records are eligible.

    Returns None if no matching meeting exists.
    """
    session = _get_session()
    try:
        # Pipeline terminal-success statuses vary ("processed" from data_packager,
        # "complete" kept for forward-compat). Anything NOT in the reject set with
        # a published_date is a valid candidate.
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
            WHERE m.status NOT IN ('rejected', 'failed', 'pending', 'downloading', 'transcribing')
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
        return dict(row._mapping)
    finally:
        session.close()
