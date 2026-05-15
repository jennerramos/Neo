"""
Caption source adapter contract.

Each adapter implements one platform (YouTube, Panopto, Ravnur, ...) and
exposes two operations:

    discover_meetings(school) -> Iterable[DiscoveredMeeting]
        Find board meeting recordings on the platform. The orchestrator
        (pipeline.collector) upserts these as Meeting rows in
        status='discovered'.

    fetch_captions(meeting) -> FetchResult
        Pull captions for a single Meeting. Adapters always return VTT
        bytes — SRT / JSON / proprietary formats are converted internally.

Disk writes, sha256 hashing, and PipelineRun logging are the orchestrator's
job (uniform across platforms), so adapters return bytes, not paths.

Concrete adapters live in sibling modules. The dispatch table in
``pipeline.sources.__init__`` maps source_type strings to adapter instances.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional, Protocol, runtime_checkable

from database.models import Meeting, School


@dataclass
class DiscoveredMeeting:
    """One recording found on a platform's site, pre-DB-insert.

    The orchestrator turns this into a Meeting row with status='discovered'.
    Platform-specific fields that don't fit the common shape go in
    ``raw_metadata`` (kept as a JSON blob).
    """
    video_id: str
    video_url: str
    title: Optional[str] = None
    published_date: Optional[date] = None
    duration_seconds: Optional[int] = None
    raw_metadata: dict = field(default_factory=dict)


@dataclass
class FetchResult:
    """Outcome of one caption-fetch attempt.

    Reason taxonomy (orchestrator branches on these):
        "fetched"          — success; ``vtt_bytes`` is non-None
        "no_captions"      — platform confirmed no caption track exists
        "private"          — recording exists but the platform refuses access
        "rate_limited"     — the platform throttled us; retry later
        "error:<details>"  — any other failure; free-form details

    "no_captions" and "private" are terminal for the meeting (no retry helps);
    they map to ``caption_unavailable`` when the platform also can't be ASR'd.
    "rate_limited" and "error:*" are retryable.
    """
    success: bool
    reason: str
    vtt_bytes: Optional[bytes] = None


@runtime_checkable
class CaptionSourceAdapter(Protocol):
    """Per-platform adapter contract.

    Implementations must set ``source_type`` to the same string used in
    ``Meeting.source_type`` and ``School.default_source_type``.
    """
    source_type: str

    def discover_meetings(self, school: School) -> Iterable[DiscoveredMeeting]: ...

    def fetch_captions(self, meeting: Meeting) -> FetchResult: ...
