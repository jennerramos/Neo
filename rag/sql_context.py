"""
Neo v2 — Phase 10: SQL Context Builder

Queries the structured extraction tables (votes, financial_items, personnel_actions,
initiatives) and formats the results as a human-readable context block for the
generator.

This is the SQL path of the query router.  Results are structured and precise —
they come from the extractor's per-chunk extraction, not free-form transcript text.

The four queries used to be hand-written copies of the same JOIN+filter pattern.
They are now driven by a single ``_TABLE_SPECS`` table; adding a fifth structured
table (e.g. ``policies``) is a one-entry change. See refactor_candidates.md #1.

Public API:
    from rag.sql_context import build_sql_context

    context, records = build_sql_context(
        tables      = ["votes", "financial_actions"],
        school_slug = "houston_city_college",
        date_from   = "2025-01-01",
        date_to     = "2026-12-31",
        query       = "What budget items did the board approve?",
        limit       = 30,
    )
    print(context)   # formatted text block ready for the generator
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import config


# ---------------------------------------------------------------------------
# DB session (lazy)
# ---------------------------------------------------------------------------

_engine  = None
_Session = None


def _get_session():
    global _engine, _Session
    if _engine is None:
        _engine  = create_engine(config.DATABASE_URL, echo=False)
        _Session = sessionmaker(bind=_engine)
    return _Session()


# ---------------------------------------------------------------------------
# Table specs — each row drives `_query_table()` below.
#
# `columns`  : SELECT list (without the trailing meeting_title/published_date/
#              school_slug, which the helper appends).
# `from`     : "<table> <alias>" used in FROM and the meetings JOIN.
# `schools_join`: how to reach the schools table — most go via meetings, but
#              `initiatives` carries a denormalized school_id, so it joins
#              schools directly. Kept explicit so the difference stays visible.
# `extras`   : extra WHERE predicates (always start with "AND").
# ---------------------------------------------------------------------------

_TABLE_SPECS: dict[str, dict[str, str]] = {
    "votes": {
        "from":         "votes v",
        "alias":        "v",
        "columns": (
            "v.motion_text, v.vote_result_text, "
            "v.yes_count, v.no_count, v.abstain_count, "
            "v.passed, v.unanimous, v.moved_by, "
            "v.confidence, v.needs_review"
        ),
        "schools_join": "JOIN schools s ON s.school_id = m.school_id",
        "extras":       "AND v.needs_review = FALSE",
    },
    "financial_items": {
        "from":         "financial_items fi",
        "alias":        "fi",
        "columns": (
            "fi.action_type, fi.category, fi.vendor, fi.amount, "
            "fi.description, fi.confidence, fi.needs_review"
        ),
        "schools_join": "JOIN schools s ON s.school_id = m.school_id",
        # `discussed` items are speculative chatter, not actions — exclude from RAG context.
        "extras":       "AND fi.needs_review = FALSE AND fi.action_type != 'discussed'",
    },
    "personnel_actions": {
        "from":         "personnel_actions pa",
        "alias":        "pa",
        "columns": (
            "pa.action_type, pa.person_name, pa.position, pa.department, "
            "pa.effective_date, pa.is_interim, pa.confidence, pa.needs_review"
        ),
        "schools_join": "JOIN schools s ON s.school_id = m.school_id",
        "extras":       "AND pa.needs_review = FALSE",
    },
    "initiatives": {
        "from":         "initiatives i",
        "alias":        "i",
        "columns": (
            "i.initiative_name, i.category, i.description, i.action_type, "
            "i.observed_action, i.stated_rationale, i.claimed_outcome, "
            "i.confidence, i.needs_review"
        ),
        # initiatives carries its own school_id (denormalized) — join schools directly.
        "schools_join": "JOIN schools s ON s.school_id = i.school_id",
        "extras":       "AND i.needs_review = FALSE",
    },
}


def _query_table(
    session,
    table_key:   str,
    school_slug: Optional[str],
    date_from:   Optional[str],
    date_to:     Optional[str],
    limit:       int,
) -> list[dict]:
    """Run the standard JOIN+filter+order query against any spec'd table."""
    spec  = _TABLE_SPECS[table_key]
    alias = spec["alias"]
    sql = text(f"""
        SELECT
            {spec['columns']},
            m.title          AS meeting_title,
            m.published_date,
            s.slug           AS school_slug
        FROM {spec['from']}
        JOIN meetings m ON m.meeting_id = {alias}.meeting_id
        {spec['schools_join']}
        WHERE 1=1
          AND (:school IS NULL  OR s.slug          = :school)
          AND (:df IS NULL      OR m.published_date >= CAST(:df AS date))
          AND (:dt IS NULL      OR m.published_date <= CAST(:dt AS date))
          {spec['extras']}
        ORDER BY m.published_date DESC
        LIMIT :lim
    """)
    rows = session.execute(sql, {
        "school": school_slug, "df": date_from, "dt": date_to, "lim": limit,
    }).fetchall()
    return [dict(r._mapping) for r in rows]


# ---------------------------------------------------------------------------
# Context formatters
# ---------------------------------------------------------------------------

def _fmt_votes(rows: list[dict]) -> str:
    if not rows:
        return "No vote records found.\n"
    lines = ["### Board Votes\n"]
    for r in rows:
        date  = str(r.get("published_date", "?"))[:10]
        title = (r.get("meeting_title") or "Untitled")[:60]
        motion = (r.get("motion_text") or "").strip()
        result = r.get("vote_result_text") or ""
        yes, no, ab = r.get("yes_count"), r.get("no_count"), r.get("abstain_count")
        passed   = r.get("passed")
        unanimous = r.get("unanimous")
        tally = ""
        if yes is not None:
            tally = f"  ({yes} yes / {no} no / {ab} abstain)"
        if unanimous:
            tally += "  [UNANIMOUS]"
        elif passed is not None:
            tally += "  [PASSED]" if passed else "  [FAILED]"
        lines.append(f"• {date} [{title}]")
        lines.append(f"  Motion : {motion}")
        if result:
            lines.append(f"  Result : {result}{tally}")
        lines.append("")
    return "\n".join(lines)


def _fmt_financial(rows: list[dict]) -> str:
    if not rows:
        return "No financial action records found.\n"
    lines = ["### Financial Actions\n"]
    for r in rows:
        date   = str(r.get("published_date", "?"))[:10]
        title  = (r.get("meeting_title") or "Untitled")[:60]
        action = (r.get("action_type") or "approved").upper()
        cat    = r.get("category") or ""
        vendor = r.get("vendor") or ""
        amount = r.get("amount")
        desc   = (r.get("description") or "").strip()
        parts  = [p for p in [cat, vendor] if p]
        detail = " / ".join(parts)
        amt_str = f"  ${amount:,.2f}" if amount else ""
        lines.append(f"• {date} [{title}]")
        lines.append(f"  [{action}]{amt_str}  {detail}")
        if desc:
            lines.append(f"  {desc}")
        lines.append("")
    return "\n".join(lines)


def _fmt_personnel(rows: list[dict]) -> str:
    if not rows:
        return "No personnel action records found.\n"
    lines = ["### Personnel Actions\n"]
    for r in rows:
        date   = str(r.get("published_date", "?"))[:10]
        title  = (r.get("meeting_title") or "Untitled")[:60]
        action   = (r.get("action_type") or "").upper()
        name     = r.get("person_name") or "Unknown"
        pos      = r.get("position") or ""
        dept     = r.get("department") or ""
        eff      = r.get("effective_date") or ""
        interim  = r.get("is_interim", False)
        detail   = ", ".join(p for p in [pos, dept] if p)
        eff_str  = f"  (effective {eff})" if eff else ""
        int_str  = "  [INTERIM]" if interim else ""
        lines.append(f"• {date} [{title}]")
        lines.append(f"  [{action}]{int_str} {name} — {detail}{eff_str}")
        lines.append("")
    return "\n".join(lines)


def _fmt_initiatives(rows: list[dict]) -> str:
    if not rows:
        return "No initiative records found.\n"
    lines = ["### Initiatives & Strategic Programs\n"]
    for r in rows:
        date     = str(r.get("published_date", "?"))[:10]
        title    = (r.get("meeting_title") or "Untitled")[:60]
        name     = r.get("initiative_name") or "Unnamed initiative"
        cat      = r.get("category") or ""
        action   = (r.get("action_type") or "discussed").upper()
        desc     = (r.get("description") or "").strip()
        observed = (r.get("observed_action") or "").strip()
        rationale = (r.get("stated_rationale") or "").strip()
        outcome  = (r.get("claimed_outcome") or "").strip()
        lines.append(f"• {date} [{title}]")
        lines.append(f"  [{action}] {name}  ({cat})")
        if desc:
            lines.append(f"  {desc}")
        if observed:
            lines.append(f"  Action   : {observed}")
        if rationale:
            lines.append(f"  Rationale: {rationale}")
        if outcome:
            lines.append(f"  Outcome  : {outcome}")
        lines.append("")
    return "\n".join(lines)


# Map of public-facing context section name -> (table_spec_key, formatter, records_key).
# `records_key` is the key under which raw rows are returned to the caller for
# citations/debugging (kept for backward-compat with prior return shape).
_SECTIONS: dict[str, tuple[str, callable, str]] = {
    "votes":             ("votes",             _fmt_votes,       "votes"),
    "financial_actions": ("financial_items",   _fmt_financial,   "financial_items"),
    "financial_items":   ("financial_items",   _fmt_financial,   "financial_items"),
    "personnel_actions": ("personnel_actions", _fmt_personnel,   "personnel_actions"),
    "initiatives":       ("initiatives",       _fmt_initiatives, "initiatives"),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_sql_context(
    tables:      list[str],
    school_slug: Optional[str] = None,
    date_from:   Optional[str] = None,
    date_to:     Optional[str] = None,
    query:       Optional[str] = None,
    limit:       int = 100,
) -> tuple[str, dict[str, list[dict]]]:
    """
    Query structured extraction tables and return formatted context.

    Args:
        tables:      Which tables to query. Any subset of:
                     ["votes", "financial_actions", "personnel_actions", "initiatives"]
        school_slug: Restrict to one school.
        date_from:   ISO date string YYYY-MM-DD.
        date_to:     ISO date string YYYY-MM-DD.
        query:       Original user query (future: used for SQL text search).
        limit:       Max rows per table. Default 100 — chosen so that anchor
                     rows (e.g. an annual budget approval) don't fall off the
                     date-ordered window when a school has a busy quarter.

    Returns:
        (context_text, records_dict)
        context_text : formatted string ready for the generator prompt
        records_dict : raw dicts keyed by table name (for citations/debugging)
    """
    session  = _get_session()
    records  : dict[str, list[dict]] = {}
    sections : list[str]             = []

    # De-dupe in case a caller passes both "financial_actions" and "financial_items".
    seen_records_keys: set[str] = set()
    try:
        for name in tables:
            entry = _SECTIONS.get(name)
            if entry is None:
                continue
            spec_key, formatter, records_key = entry
            if records_key in seen_records_keys:
                continue
            seen_records_keys.add(records_key)
            rows = _query_table(session, spec_key, school_slug, date_from, date_to, limit)
            records[records_key] = rows
            sections.append(formatter(rows))
    finally:
        session.close()

    context = "\n".join(sections) if sections else "No structured records found.\n"
    return context, records
