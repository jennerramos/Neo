"""
Resolving when a meeting actually happened.

``Meeting.published_date`` is the date the recording was *uploaded*, not the
date the board met. For channels that upload as they go the two coincide, but
a backfill breaks them apart: Lone Star put 41 meetings on the single upload
date 2023-10-20, spanning ten different months of actual board business, some
of it from 2017.

That matters because the pipeline filters on age. Filtering on
``published_date`` alone let 52 pre-2020 meetings through the 2023 cutoff,
because they had merely been *uploaded* recently.

Measured agreement between the title's year and ``published_date``'s year,
over every meeting whose title states a year (2026-08-17):

    dallas_college             96/96   100%
    el_paso_community_college  49/49   100%
    central_texas_college      13/13   100%
    austin_community_college   41/41   100%
    houston_city_college       39/39   100%
    alamo_colleges             35/37    95%
    lone_star_college         109/166   66%   <- backfill
    mt_san_antonio_college      1/4     25%   <- only 4 meetings, no confidence

So the title wins when it states a year, and ``published_date`` is a usable
fallback everywhere except the two schools below.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional, Tuple

# Schools where published_date must NOT stand in for the meeting date:
# lone_star bulk-backfilled, mt_san_antonio has too few meetings to trust.
UNRELIABLE_PUBLISHED_DATE_SCHOOLS = frozenset({
    "lone_star_college",
    "mt_san_antonio_college",
})

_YEAR_RE = re.compile(r"\b(20[0-2]\d)\b")


def resolve_meeting_year(
    title:          Optional[str],
    published_date: Optional[date],
    school_slug:    Optional[str],
) -> Tuple[Optional[int], str]:
    """
    Best estimate of the year the meeting took place.

    Returns ``(year, how)`` where *how* is "title", "published_date" or
    "unresolved". A None year means we genuinely do not know — callers must
    not silently treat that as old, or as recent.
    """
    m = _YEAR_RE.search(title or "")
    if m:
        return int(m.group(1)), "title"

    if published_date and school_slug not in UNRELIABLE_PUBLISHED_DATE_SCHOOLS:
        return published_date.year, "published_date"

    return None, "unresolved"


def is_too_old(
    title:          Optional[str],
    published_date: Optional[date],
    school_slug:    Optional[str],
    cutoff_year:    int,
) -> bool:
    """
    True only when we can show the meeting predates *cutoff_year*.

    Deliberately conservative: an unresolved date returns False, so an
    undateable meeting is processed rather than silently dropped. Dropping is
    the irreversible direction — a meeting we wrongly keep is visible and
    fixable, one we wrongly discard is never noticed.

    The upload date is still a hard upper bound: a meeting cannot have taken
    place after the recording of it was published. So a pre-cutoff
    published_date proves the meeting is too old even when the school's
    published_date is otherwise untrustworthy.
    """
    year, how = resolve_meeting_year(title, published_date, school_slug)
    if year is not None:
        return year < cutoff_year

    if published_date is not None and published_date.year < cutoff_year:
        return True   # upper bound is already before the cutoff

    return False
