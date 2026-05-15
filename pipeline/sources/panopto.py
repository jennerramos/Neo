from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Iterable, Optional

from database.models import Meeting, School
from pipeline.sources.base import DiscoveredMeeting, FetchResult


USER_AGENT = "Mozilla/5.0"
DEFAULT_MIN_DURATION_SECONDS = 600
CAPTION_PAGE_SIZE = 100
MAX_CAPTION_PAGES = 200
TICKS_PER_SECOND = 10_000_000
REQUEST_TIMEOUT_SECONDS = 30.0
RETRY_BACKOFF_SECONDS = (2.0, 5.0, 15.0)

# Panopto's SessionStartTime is seconds since the Windows FILETIME epoch
# (1601-01-01 UTC), not Unix epoch. Subtract this to get Unix seconds.
FILETIME_TO_UNIX_OFFSET_SECONDS = 11_644_473_600

# Discovery date cutoff — matches pipeline.sources.youtube.DATE_CUTOFF.
# Anything older than this is filtered out during discovery.
from datetime import date as _date
DATE_CUTOFF = _date(2023, 4, 8)


def _open_with_retry(request: urllib.request.Request) -> tuple[bytes, dict]:
    """urlopen + per-attempt timeout + retry on transient network errors.

    Retries TimeoutError, ConnectionError, and non-HTTP URLError (DNS, reset,
    timeout). 4xx/5xx HTTPError is re-raised on the first attempt — the caller
    should branch on status code, not retry blindly.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(len(RETRY_BACKOFF_SECONDS) + 1):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return response.read(), dict(response.headers)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_exc = exc
            if attempt < len(RETRY_BACKOFF_SECONDS):
                time.sleep(RETRY_BACKOFF_SECONDS[attempt])
                continue
            raise
    raise RuntimeError("unreachable") from last_exc

GUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
VIEWER_GUID_RE = re.compile(r"Viewer\.aspx\?id=(" + GUID_RE + r")", re.IGNORECASE)
VIDEO_URL_HOST_RE = re.compile(r"^https?://([^/]+)", re.IGNORECASE)


def _extract_guids(html: str) -> list[str]:
    seen = set()
    guids = []
    for match in VIEWER_GUID_RE.finditer(html):
        guid = match.group(1).lower()
        if guid not in seen:
            seen.add(guid)
            guids.append(guid)
    return guids


def _clean_host(host: str) -> str:
    host = re.sub(r"^https?://", "", host.strip(), flags=re.IGNORECASE)
    return host.split("/", 1)[0]


def _host_from_video_url(video_url: str) -> str:
    match = VIDEO_URL_HOST_RE.search(video_url or "")
    if not match:
        return ""
    return _clean_host(match.group(1))


def _read_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    raw, _ = _open_with_retry(request)
    return raw.decode("utf-8", errors="replace")


def _post_delivery_info(host: str, guid: str) -> Optional[dict]:
    url = f"https://{_clean_host(host)}/Panopto/Pages/Viewer/DeliveryInfo.aspx"
    body = f"deliveryId={guid}&isEmbed=true&responseType=json".encode("ascii")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    raw, _ = _open_with_retry(request)
    if raw.lstrip().startswith(b"<"):
        return None
    return json.loads(raw.decode("utf-8"))


def _parse_published_date(session_name: Optional[str]):
    if not session_name:
        return None
    for pattern in ("%B %d, %Y", "%m/%d/%Y", "%m.%d.%Y"):
        match = re.search(r"\b([A-Za-z]+ \d{1,2}, \d{4}|\d{1,2}[/.]\d{1,2}[/.]\d{4})\b", session_name)
        if not match:
            return None
        try:
            return datetime.strptime(match.group(1), pattern).date()
        except ValueError:
            continue
    return None


def _date_from_session_start_time(session_start_time):
    """Convert Panopto's FILETIME-epoch seconds to a UTC calendar date.

    Slight tz skew: a meeting starting after ~6pm local in CT will report as
    the next day in UTC. Acceptable for filtering by date cutoff; downstream
    display ignores time-of-day anyway.
    """
    if session_start_time is None:
        return None
    try:
        unix_seconds = float(session_start_time) - FILETIME_TO_UNIX_OFFSET_SECONDS
        if unix_seconds <= 0:
            return None
        from datetime import timezone
        return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).date()
    except (TypeError, ValueError, OSError):
        return None


def _format_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    millis = int(round(seconds * 1000))
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, ms = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def _events_to_vtt(events: list[dict], session_start_time: float) -> bytes:
    cues = []
    for event in events:
        metadata = str(event.get("Metadata") or "")
        if not metadata.strip():
            continue
        start = float(event.get("TimelineTime", 0)) / TICKS_PER_SECOND - session_start_time
        start = max(start, 0.0)
        end = start + float(event.get("Duration") or 0)
        cues.append((start, end, metadata))

    parts = ["WEBVTT\n\n"]
    for start, end, metadata in sorted(cues, key=lambda cue: cue[0]):
        parts.append(f"{_format_timestamp(start)} --> {_format_timestamp(end)}\n{metadata}\n\n")
    return "".join(parts).encode("utf-8")


class PanoptoAdapter:
    source_type: str = "panopto"

    def discover_meetings(self, school: School) -> Iterable[DiscoveredMeeting]:
        config = school.discovery_config or {}
        board_page_url = config["board_page_url"]
        host = _clean_host(config["panopto_host"])
        min_duration = int(config.get("min_duration_seconds", DEFAULT_MIN_DURATION_SECONDS))

        html = _read_text(board_page_url)
        for guid in _extract_guids(html):
            delivery_info = _post_delivery_info(host, guid)
            if delivery_info is None:
                continue

            delivery = delivery_info.get("Delivery") or {}
            if not delivery.get("HasCaptions"):
                continue

            duration = float(delivery.get("Duration") or 0)
            if duration < min_duration:
                continue

            session_name = delivery.get("SessionName")
            published_date = (
                _parse_published_date(session_name)
                or _date_from_session_start_time(delivery.get("SessionStartTime"))
            )
            if published_date is not None and published_date < DATE_CUTOFF:
                continue

            yield DiscoveredMeeting(
                video_id=guid,
                video_url=f"https://{host}/Panopto/Pages/Viewer.aspx?id={guid}",
                title=session_name,
                published_date=published_date,
                duration_seconds=int(duration),
                raw_metadata={
                    "session_id": delivery_info.get("SessionId"),
                    "available_captions": delivery.get("AvailableCaptions"),
                },
            )

    def _fetch_caption_page(self, host: str, guid: str, page: int, page_size: int) -> tuple[list, bool]:
        url = (
            f"https://{_clean_host(host)}/Panopto/api/v1-beta/sessions/{guid}/captions"
            f"?language=0&page={page}&pageSize={page_size}"
        )
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        raw, headers = _open_with_retry(request)
        has_more = headers.get("Has-More-Pages", "False") == "True"
        return json.loads(raw.decode("utf-8")), has_more

    def fetch_captions(self, meeting: Meeting) -> FetchResult:
        host = _host_from_video_url(meeting.video_url or "")
        try:
            delivery_info = _post_delivery_info(host, meeting.video_id)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            return FetchResult(False, f"error:connection_timeout: {type(exc).__name__}")
        if delivery_info is None:
            return FetchResult(False, "error:invalid_session")

        delivery = delivery_info.get("Delivery") or {}
        if not delivery.get("HasCaptions"):
            return FetchResult(False, "no_captions")

        session_start_time = float(delivery.get("SessionStartTime") or 0)
        events = []
        for page in range(MAX_CAPTION_PAGES):
            try:
                page_events, has_more = self._fetch_caption_page(
                    host, meeting.video_id, page, CAPTION_PAGE_SIZE
                )
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                return FetchResult(False, f"error:connection_timeout: {type(exc).__name__}")
            events.extend(page_events)
            if not has_more:
                break

        return FetchResult(True, "fetched", vtt_bytes=_events_to_vtt(events, session_start_time))


adapter = PanoptoAdapter()
