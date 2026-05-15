"""
Caption-source adapter registry.

Each adapter implementation is loaded eagerly here so that downstream
imports (``from pipeline.sources import for_school``) can dispatch by
source_type without callers knowing which modules exist.

PR 1: only YouTube. PR 3/4 add Panopto and Ravnur.
"""
from __future__ import annotations

from typing import Dict

from database.models import School
from pipeline.sources.base import CaptionSourceAdapter
from pipeline.sources.panopto import PanoptoAdapter, adapter as _panopto
from pipeline.sources.ravnur import adapter as _ravnur
from pipeline.sources.youtube import adapter as _youtube


DEFAULT_SOURCE_TYPE: str = "youtube_caption"


ADAPTERS: Dict[str, CaptionSourceAdapter] = {
    _youtube.source_type: _youtube,
    _panopto.source_type: _panopto,
    _ravnur.source_type: _ravnur,
}


def get_adapter(source_type: str) -> CaptionSourceAdapter:
    """Look up an adapter by source_type. Raises KeyError if unknown."""
    return ADAPTERS[source_type]


def for_school(school: School) -> CaptionSourceAdapter:
    """Resolve the adapter that should drive discovery + caption fetch for
    this school.

    Resolution order:
      1. ``School.default_source_type`` if set and registered (post-0004)
      2. legacy ``School.source_type`` if set and registered
      3. ``DEFAULT_SOURCE_TYPE`` ("youtube_caption")
    """
    candidate = (
        getattr(school, "default_source_type", None)
        or getattr(school, "source_type", None)
        or DEFAULT_SOURCE_TYPE
    )
    return ADAPTERS.get(candidate, ADAPTERS[DEFAULT_SOURCE_TYPE])
