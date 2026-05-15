"""
Shared filter-builder helpers for raw-SQL list/summary queries.

Every list and summary endpoint in api/db/queries/ filters by some combination
of school_slug + published_date range, plus its own table-specific predicates.
Before this module the school/date pattern was copy-pasted six times and a
seventh in rag/sql_context.py. Now it lives here.

The helper appends to a caller-owned `(filters, params)` tuple rather than
returning a string so the caller can mix in its own predicates without losing
parameter-binding safety. Order of WHERE clauses doesn't matter to the planner;
keeping the helper additive avoids interfering with table-specific filters.

See refactor_candidates.md #1.
"""
from __future__ import annotations
from typing import Optional


def apply_school_date_filters(
    filters:     list[str],
    params:      dict,
    *,
    school_slug: Optional[str] = None,
    date_from:   Optional[str] = None,
    date_to:     Optional[str] = None,
    school_alias:  str = "s",
    meeting_alias: str = "m",
) -> None:
    """Append school + published-date filters to caller-owned filters/params.

    Assumes the SQL already JOINs schools (alias=`s`) and meetings (alias=`m`).
    Param names used: ``:school``, ``:df``, ``:dt`` — caller must not reuse those.
    """
    if school_slug:
        filters.append(f"{school_alias}.slug = :school")
        params["school"] = school_slug
    if date_from:
        filters.append(f"{meeting_alias}.published_date >= CAST(:df AS date)")
        params["df"] = date_from
    if date_to:
        filters.append(f"{meeting_alias}.published_date <= CAST(:dt AS date)")
        params["dt"] = date_to
