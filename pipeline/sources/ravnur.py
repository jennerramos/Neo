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
DEFAULT_ORGANIZATION = "Board Meetings"
DEFAULT_MIN_DURATION_SECONDS = 600
DEFAULT_PAGE_COUNT = 300
REQUEST_TIMEOUT_SECONDS = 30.0
RETRY_BACKOFF_SECONDS = (2.0, 5.0, 15.0)
VIDEO_URL_HOST_RE = re.compile(r"^https?://([^/]+)", re.IGNORECASE)

# Discovery date cutoff — matches pipeline.sources.youtube.DATE_CUTOFF.
from datetime import date as _date
DATE_CUTOFF = _date(2023, 4, 8)


def _open_with_retry(request: urllib.request.Request) -> bytes:
    """urlopen + per-attempt timeout + retry on transient network errors.

    4xx/5xx HTTPError is re-raised on the first attempt — the caller branches
    on status code rather than retry blindly.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(len(RETRY_BACKOFF_SECONDS) + 1):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return response.read()
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_exc = exc
            if attempt < len(RETRY_BACKOFF_SECONDS):
                time.sleep(RETRY_BACKOFF_SECONDS[attempt])
                continue
            raise
    raise RuntimeError("unreachable") from last_exc


def _host_from_video_url(video_url: str) -> str:
    match = VIDEO_URL_HOST_RE.search(video_url or "")
    if not match:
        return ""
    return match.group(1)


def _dynamic_property(item: dict, title: str) -> Optional[str]:
    for prop in item.get("dynamicProperties") or []:
        if prop.get("title") == title:
            return prop.get("value")
    return None


def _parse_published_date(value: Optional[str]):
    if not value:
        return None
    clean = re.sub(r"\.\d+", "", value).rstrip("Z")
    try:
        return datetime.fromisoformat(clean).date()
    except ValueError:
        return None


def _error_result(exc: Exception) -> FetchResult:
    return FetchResult(False, f"error:{type(exc).__name__}: {str(exc)[:80]}")


class RavnurAdapter:
    """Ravnur caption + discovery adapter.

    Discovery uses the portal media listing API and filters to captioned
    videos in the configured organization. Caption fetch follows the source
    detail API to the approved VTT track and returns the raw bytes unchanged.
    """

    source_type: str = "ravnur"

    def _get_json(self, url: str) -> dict:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        return json.loads(_open_with_retry(request).decode("utf-8"))

    def _get_bytes(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        return _open_with_retry(request)

    def discover_meetings(self, school: School) -> Iterable[DiscoveredMeeting]:
        config = school.discovery_config or {}
        portal_url = config["portal_url"].rstrip("/")
        organization = config.get("organization", DEFAULT_ORGANIZATION)
        min_duration = int(config.get("min_duration_seconds", DEFAULT_MIN_DURATION_SECONDS))
        page_count = int(config.get("page_count", DEFAULT_PAGE_COUNT))

        offset = 0
        total = None
        while total is None or offset < total:
            data = self._get_json(f"{portal_url}/api/v1.0/media?Offset={offset}&Count={page_count}")
            total = int(data.get("total") or 0)

            for item in data.get("items") or []:
                if item.get("type") != "Video" or item.get("hasCC") is not True:
                    continue

                org = _dynamic_property(item, "Organization")
                if org != organization:
                    continue

                duration = float(item.get("duration") or 0)
                if duration < min_duration:
                    continue

                video_id = item.get("id")
                if not video_id:
                    continue

                published_date = _parse_published_date(item.get("createdDate"))
                if published_date is not None and published_date < DATE_CUTOFF:
                    continue

                category = _dynamic_property(item, "Category")
                thumbnail = item.get("thumbnail") or {}
                yield DiscoveredMeeting(
                    video_id=video_id,
                    video_url=f"{portal_url}/media/{video_id}",
                    title=item.get("title"),
                    published_date=published_date,
                    duration_seconds=int(duration),
                    raw_metadata={
                        "ravnur_organization": org,
                        "ravnur_category": category,
                        "thumbnail_url": thumbnail.get("url"),
                    },
                )

            offset += page_count
            if page_count <= 0:
                break

    def fetch_captions(self, meeting: Meeting) -> FetchResult:
        host = _host_from_video_url(meeting.video_url or "")
        try:
            source = self._get_json(f"https://{host}/api/v1.0/source/{meeting.video_id}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return FetchResult(False, "error:source_not_found")
            return _error_result(exc)
        except Exception as exc:
            return _error_result(exc)

        media_sources = source.get("mediaSources") or []
        if not media_sources:
            return FetchResult(False, "no_captions")

        tracks = media_sources[0].get("cc") or []
        if not tracks:
            return FetchResult(False, "no_captions")

        approved = [track for track in tracks if track.get("stateName") == "Approved"]
        chosen = next(
            (track for track in approved if str(track.get("srclang") or "").startswith("en")),
            None,
        )
        if chosen is None and approved:
            chosen = approved[0]
        if chosen is None:
            return FetchResult(False, "no_captions")

        try:
            body = self._get_bytes(chosen["src"])
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return FetchResult(False, "no_captions")
            return _error_result(exc)
        except Exception as exc:
            return _error_result(exc)

        if body.startswith(b"WEBVTT"):
            return FetchResult(True, "fetched", vtt_bytes=body)
        return FetchResult(False, "error:invalid_vtt")


adapter = RavnurAdapter()
