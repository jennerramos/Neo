"""
YouTube caption source adapter.

Wraps two operations on YouTube channels:

  discover_meetings(school)
      Pages each verified channel's uploads playlist (YouTube Data API),
      keyword-filters titles, date-filters against DATE_CUTOFF, probes
      surviving candidates via yt-dlp for duration/live-event status,
      and yields one DiscoveredMeeting per surviving video.

  fetch_captions(meeting)
      Tries youtube-transcript-api first (fast, hits timedtext directly),
      falls back to yt-dlp (browser cookies + proxy) when the primary
      path is IP-blocked or the source isn't actually YouTube. Returns
      VTT bytes; disk write + hashing happens in the orchestrator.

Reason strings on FetchResult are kept identical to the pre-adapter
caption_downloader (``ip_blocked``, ``no_captions_available``,
``transcripts_disabled``, ``video_unavailable``, ``library_missing``,
``ytdlp_missing``, ``fetch_error:<exc>: <msg>``, and ``ytdlp_<label>``
on success) so PipelineRun rows stay grep-compatible with v1 data.
"""
from __future__ import annotations

import itertools
import json
import logging
import re
import subprocess
import tempfile
from datetime import datetime
from datetime import date as _date
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import unquote, urlsplit

import config
from database.models import Meeting, School
from pipeline.sources.base import DiscoveredMeeting, FetchResult

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Discovery filters — migrated from the old collector.py.
# ---------------------------------------------------------------------------

BOARD_KEYWORDS = [
    "board", "trustee", "trustees", "board meeting", "board of trustees",
    "regular meeting", "special meeting", "workshop", "committee meeting",
    "board session", "regents",
]

MIN_DURATION_SECONDS = 600        # 10 minutes — filters out shorts / trailers
DATE_CUTOFF = _date(2023, 4, 8)   # ignore anything older than this
MAX_RESULTS_PER_PAGE = 50         # YouTube API max per request
MAX_PAGES = 20                    # cap at 1000 videos per channel


def is_board_meeting(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in BOARD_KEYWORDS)


def detect_meeting_type(title: str) -> str:
    t = title.lower()
    if "special" in t:
        return "special"
    if "workshop" in t:
        return "workshop"
    if "committee" in t:
        return "committee"
    return "regular"


def probe_video(video_id: str) -> dict:
    """yt-dlp metadata probe — exact duration + format availability."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--dump-json",
                "--no-download",
                "--quiet",
                "--no-warnings",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            return {
                "duration_seconds": data.get("duration"),
                "has_formats":      bool(data.get("formats")),
                "view_count":       data.get("view_count"),
                # is_live=True means the stream hasn't started yet
                "is_live":          data.get("is_live") or data.get("live_status") in ("is_upcoming", "is_live"),
            }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass

    return {"duration_seconds": None, "has_formats": None, "view_count": None}


# ---------------------------------------------------------------------------
# Caption-fetch helpers — migrated from caption_downloader.py.
# ---------------------------------------------------------------------------

def _make_ytt_api(proxy_url: Optional[str] = None):
    from youtube_transcript_api import YouTubeTranscriptApi

    if not proxy_url:
        return YouTubeTranscriptApi()

    from youtube_transcript_api.proxies import GenericProxyConfig, WebshareProxyConfig

    parts = urlsplit(proxy_url)
    host = (parts.hostname or "").lower()
    username = unquote(parts.username or "")
    password = unquote(parts.password or "")

    # Webshare Residential package: use WebshareProxyConfig so each request
    # rotates to a fresh residential IP and we get built-in retry-on-block.
    #
    # Username format matters: WebshareProxyConfig appends "-rotate" to the
    # bare username. So ".env" must have the BASE username (e.g. "qqsgsmnw"),
    # NOT a specific-proxy suffix like "qqsgsmnw-1" (which would build the
    # malformed "qqsgsmnw-1-rotate" → Webshare returns 400).
    if host == "p.webshare.io" and username and password:
        clean_username = re.sub(r"-\d+$", "", username)
        return YouTubeTranscriptApi(
            proxy_config=WebshareProxyConfig(
                proxy_username=clean_username,
                proxy_password=password,
                proxy_port=parts.port or 80,
                domain_name=parts.hostname or "p.webshare.io",
                retries_when_blocked=3,
            )
        )

    return YouTubeTranscriptApi(
        proxy_config=GenericProxyConfig(
            http_url=proxy_url,
            https_url=proxy_url,
        )
    )


def _select_transcript(transcript_list, NoTranscriptFound):
    transcript = None
    label = None

    try:
        transcript = transcript_list.find_manually_created_transcript(
            ["en", "en-US", "en-GB"]
        )
        label = "manual_en"
    except NoTranscriptFound:
        pass

    if transcript is None:
        try:
            transcript = transcript_list.find_generated_transcript(
                ["en", "en-US", "en-GB"]
            )
            label = "auto_en"
        except NoTranscriptFound:
            pass

    if transcript is None:
        for t in transcript_list:
            if not t.is_generated:
                transcript = t
                label = f"manual_{t.language_code}"
                break

    if transcript is None:
        for t in transcript_list:
            if t.is_generated and t.language_code != "live_chat":
                transcript = t
                label = f"auto_{t.language_code}"
                break

    return transcript, label


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class YouTubeAdapter:
    """YouTube caption + discovery adapter.

    State held on the instance:
      - ``_proxy_iter``: cycle iterator over config.PROXY_LIST, lazily
        initialized on first call to _next_proxy().
      - ``_yt``: googleapiclient YouTube v3 client, lazily built on first
        call to _get_yt(). Avoids requiring YOUTUBE_API_KEY just to import
        the adapter (caption-only flows don't need the API).
    """

    source_type: str = "youtube_caption"

    def __init__(self) -> None:
        self._proxy_iter: Optional[itertools.cycle] = None
        self._yt = None

    # ── proxy + API plumbing ────────────────────────────────────────────────

    def _next_proxy(self) -> Optional[str]:
        if not config.PROXY_LIST:
            return None
        if self._proxy_iter is None:
            self._proxy_iter = itertools.cycle(config.PROXY_LIST)
        return next(self._proxy_iter)

    def _get_yt(self):
        if self._yt is None:
            from googleapiclient.discovery import build
            if not config.YOUTUBE_API_KEY:
                raise RuntimeError("YOUTUBE_API_KEY not set in .env")
            self._yt = build("youtube", "v3", developerKey=config.YOUTUBE_API_KEY)
        return self._yt

    # ── discovery ───────────────────────────────────────────────────────────

    def _get_uploads_playlist_id(self, channel_id: str) -> Optional[str]:
        from googleapiclient.errors import HttpError
        try:
            resp = self._get_yt().channels().list(
                part="contentDetails",
                id=channel_id,
            ).execute()
            items = resp.get("items", [])
            if not items:
                return None
            return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        except HttpError as e:
            print(f"    [API ERROR] getting uploads playlist: {e}")
            return None

    def _fetch_all_uploads(self, playlist_id: str) -> list[dict]:
        from googleapiclient.errors import HttpError
        videos: list[dict] = []
        page_token: Optional[str] = None
        page = 0

        while page < MAX_PAGES:
            try:
                kwargs = dict(
                    part="snippet",
                    playlistId=playlist_id,
                    maxResults=MAX_RESULTS_PER_PAGE,
                )
                if page_token:
                    kwargs["pageToken"] = page_token

                resp = self._get_yt().playlistItems().list(**kwargs).execute()
                items = resp.get("items", [])
                hit_cutoff = False

                for item in items:
                    published_str = item["snippet"].get("publishedAt", "")
                    if published_str:
                        published = datetime.fromisoformat(
                            published_str.replace("Z", "+00:00")
                        ).date()
                        if published < DATE_CUTOFF:
                            hit_cutoff = True
                            break
                    videos.append(item)

                if hit_cutoff:
                    break

                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
                page += 1

            except HttpError as e:
                print(f"    [API ERROR] fetching uploads page {page}: {e}")
                break

        return videos

    def discover_meetings(self, school: School) -> Iterable[DiscoveredMeeting]:
        """Yield candidates that pass every platform-side filter.

        Caller is responsible for DB deduplication and inserting Meeting rows.
        Filters applied here: title keywords, DATE_CUTOFF, future/live event,
        MIN_DURATION_SECONDS. Matches the v1 collect_channel() filter chain.
        """
        for channel in school.channels:
            if not channel.verified:
                continue

            print(f"\n  [{school.slug}] {school.name}")
            print(f"  Channel: {channel.channel_name} ({channel.youtube_channel_id})")

            playlist_id = self._get_uploads_playlist_id(channel.youtube_channel_id)
            if not playlist_id:
                print(f"    [ERROR] Could not get uploads playlist. Skipping.")
                continue

            print(f"    Fetching uploads playlist...", end=" ", flush=True)
            uploads = self._fetch_all_uploads(playlist_id)
            print(f"{len(uploads)} videos found.")

            for item in uploads:
                snippet = item["snippet"]
                video_id = snippet.get("resourceId", {}).get("videoId", "")
                title = snippet.get("title", "")
                published_str = snippet.get("publishedAt", "")

                if not video_id or not title:
                    continue
                if not is_board_meeting(title):
                    continue

                published_date: Optional[_date] = None
                if published_str:
                    try:
                        published_date = datetime.fromisoformat(
                            published_str.replace("Z", "+00:00")
                        ).date()
                        if published_date < DATE_CUTOFF:
                            continue
                    except ValueError:
                        pass

                print(f"    + Probing: {title[:65]}...")
                probe = probe_video(video_id)
                duration = probe["duration_seconds"]

                # yt-dlp sets is_live=True and duration=None for scheduled streams
                # that haven't aired yet — skip them; they get re-collected once live.
                if probe.get("is_live") or (
                    probe.get("duration_seconds") is None and not probe.get("has_formats")
                ):
                    print(f"      ✗ Future/live event, skipping.")
                    continue

                if duration is not None and duration < MIN_DURATION_SECONDS:
                    print(f"      ✗ Too short ({duration:.0f}s), skipping.")
                    continue

                yield DiscoveredMeeting(
                    video_id=video_id,
                    video_url=f"https://www.youtube.com/watch?v={video_id}",
                    title=title,
                    published_date=published_date,
                    duration_seconds=int(duration) if duration else None,
                    raw_metadata={
                        "snippet": snippet,
                        "probe": probe,
                        "meeting_type": detect_meeting_type(title),
                    },
                )

    # ── caption fetch ───────────────────────────────────────────────────────

    def _attempt_via_transcript_api(
        self, video_id: str
    ) -> tuple[bool, str, Optional[bytes]]:
        """Primary: youtube-transcript-api. Rotates proxy on IpBlocked."""
        try:
            from youtube_transcript_api import (
                NoTranscriptFound,
                TranscriptsDisabled,
                VideoUnavailable,
            )
            from youtube_transcript_api.formatters import WebVTTFormatter
            try:
                from youtube_transcript_api import IpBlocked
            except ImportError:
                IpBlocked = None
        except ImportError:
            log.warning("youtube-transcript-api not installed")
            return False, "library_missing", None

        transcript_list = None
        max_attempts = max(len(config.PROXY_LIST) + 1, 2)

        for attempt in range(max_attempts):
            # First attempt uses the first proxy (or None); subsequent attempts rotate
            proxy = config.PROXY_LIST[0] if (attempt == 0 and config.PROXY_LIST) else self._next_proxy()
            try:
                transcript_list = _make_ytt_api(proxy).list(video_id)
                break
            except TranscriptsDisabled:
                return False, "transcripts_disabled", None
            except VideoUnavailable:
                return False, "video_unavailable", None
            except Exception as e:
                ename = type(e).__name__
                is_blocked = (
                    (IpBlocked and isinstance(e, IpBlocked)) or
                    any(x in ename for x in ("IpBlocked", "RequestBlocked"))
                )
                if is_blocked and attempt + 1 < max_attempts and config.PROXY_LIST:
                    print(f"           ⚡ IpBlocked — rotating proxy (attempt {attempt + 1}/{max_attempts - 1})")
                    continue
                short_msg = str(e)[:80].replace("\n", " ")
                if is_blocked:
                    return False, "ip_blocked", None
                return False, f"{ename}: {short_msg}", None

        transcript, label = _select_transcript(transcript_list, NoTranscriptFound)
        if transcript is None:
            return False, "no_captions_available", None

        max_fetch_attempts = max(len(config.PROXY_LIST) + 1, 2)

        for attempt in range(max_fetch_attempts):
            try:
                fetched = transcript.fetch()
                vtt_content = WebVTTFormatter().format_transcript(fetched)
                return True, label, vtt_content.encode("utf-8")
            except Exception as e:
                ename = type(e).__name__
                is_blocked = (
                    (IpBlocked and isinstance(e, IpBlocked)) or
                    any(x in ename for x in ("IpBlocked", "RequestBlocked"))
                )

                if not is_blocked:
                    short_msg = str(e)[:80].replace("\n", " ")
                    return False, f"fetch_error:{ename}: {short_msg}", None

                if attempt + 1 >= max_fetch_attempts or not config.PROXY_LIST:
                    return False, "ip_blocked", None

                print(f"           -> IpBlocked during fetch, rotating proxy ({attempt + 1}/{max_fetch_attempts - 1})")

                proxy = self._next_proxy()
                try:
                    transcript_list = _make_ytt_api(proxy).list(video_id)
                except TranscriptsDisabled:
                    return False, "transcripts_disabled", None
                except VideoUnavailable:
                    return False, "video_unavailable", None
                except Exception:
                    continue

                transcript, label = _select_transcript(transcript_list, NoTranscriptFound)
                if transcript is None:
                    return False, "no_captions_available", None

        return False, "ip_blocked", None

    def _attempt_via_ytdlp(
        self, video_url: str, video_id: str
    ) -> tuple[bool, str, Optional[bytes]]:
        """Fallback: yt-dlp. Writes to a temp dir, returns bytes."""
        try:
            from yt_dlp import YoutubeDL
        except ImportError:
            return False, "ytdlp_missing", None

        with tempfile.TemporaryDirectory(prefix=f"neo_ytdlp_{video_id}_") as tmpdir:
            raw_dir = Path(tmpdir)
            out_base = str(raw_dir / video_id)
            browser = config.YT_DLP_COOKIES_BROWSER
            cookies_opt = {"cookiesfrombrowser": (browser,)} if browser else {}

            # Pipe yt-dlp through the same proxy as youtube-transcript-api when one
            # is configured. For Webshare Residential we rewrite the username from
            # "qqsgsmnw-1" (specific static proxy) to "qqsgsmnw-rotate" (residential
            # rotation) so each yt-dlp retry hits a fresh exit IP — matches what
            # WebshareProxyConfig does for the primary path.
            def _rotate_url(u: str) -> str:
                parts = urlsplit(u)
                if (parts.hostname or "").lower() != "p.webshare.io" or not parts.username:
                    return u
                base = re.sub(r"-\d+$", "", unquote(parts.username))
                if base.endswith("-rotate"):
                    base = base[: -len("-rotate")]
                new_user = f"{base}-rotate"
                creds = f"{new_user}:{unquote(parts.password or '')}"
                port = f":{parts.port}" if parts.port else ""
                return f"{parts.scheme}://{creds}@{parts.hostname}{port}{parts.path or ''}"

            proxy_opt = {"proxy": _rotate_url(config.PROXY_LIST[0])} if config.PROXY_LIST else {}

            base_opts = {
                "skip_download": True,
                "subtitlesformat": "vtt",
                "outtmpl": {"subtitle": out_base + ".%(ext)s"},
                "quiet": True,
                "no_warnings": True,
                "retries": 3,
                "socket_timeout": 30,
                "ignoreerrors": True,
                "check_formats": False,
                **cookies_opt,
                **proxy_opt,
            }

            strategies = [
                (True,  False, ["en", "en-US", "en-GB"],     "manual_en"),
                (False, True,  ["en", "en-US", "en-GB"],     "auto_en"),
                (True,  False, ["all", "-live_chat"],         "manual_any"),
                (False, True,  ["all", "-live_chat"],         "auto_any"),
            ]

            for write_manual, write_auto, langs, label in strategies:
                opts = {
                    **base_opts,
                    "writesubtitles":    write_manual,
                    "writeautomaticsub": write_auto,
                    "subtitleslangs":    langs,
                }
                try:
                    with YoutubeDL(opts) as ydl:
                        ydl.download([video_url])
                except Exception as e:
                    log.debug("yt-dlp error (%s) for %s: %s", label, video_id, e)
                    continue

                final = raw_dir / f"{video_id}.vtt"
                if not final.exists():
                    for cand in raw_dir.glob(f"{video_id}.*.vtt"):
                        try:
                            cand.rename(final)
                        except Exception:
                            final = cand
                        break
                if final.exists():
                    return True, f"ytdlp_{label}", final.read_bytes()

            return False, "no_captions_available", None

    def fetch_captions(self, meeting: Meeting) -> FetchResult:
        """Try transcript-api, fall back to yt-dlp under the same conditions
        as the legacy caption_downloader.process_meeting() flow."""
        success, reason, vtt_bytes = self._attempt_via_transcript_api(meeting.video_id)

        if not success:
            existing_url = getattr(meeting, "video_url", None)
            is_youtube = bool(
                existing_url and ("youtube.com" in existing_url or "youtu.be" in existing_url)
            )
            url = existing_url or f"https://www.youtube.com/watch?v={meeting.video_id}"

            should_try_ytdlp = (existing_url and not is_youtube) or (
                (is_youtube or not existing_url) and reason == "ip_blocked"
            )

            if should_try_ytdlp:
                print(f"           ↻ retrying via yt-dlp (cookies={config.YT_DLP_COOKIES_BROWSER or 'none'})")
                success, reason, vtt_bytes = self._attempt_via_ytdlp(url, meeting.video_id)

        if success and vtt_bytes:
            return FetchResult(success=True, reason=reason, vtt_bytes=vtt_bytes)
        return FetchResult(success=False, reason=reason, vtt_bytes=None)


# Module-level singleton — the registry binds this instance to source_type.
adapter = YouTubeAdapter()
