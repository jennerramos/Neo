"""
Phase 3 — Caption Downloader

For every meeting with status='discovered', attempts to download official
YouTube captions using youtube-transcript-api (primary) with yt-dlp as
a fallback for edge cases.

youtube-transcript-api hits YouTube's timedtext endpoint directly —
no video format resolution, no "Requested format is not available" errors,
works on archived live streams and premieres.

Priority order:
    1. Manual EN captions       (highest quality — human-made)
    2. Auto-generated EN        (YouTube ASR — still good)
    3. Manual any language      (non-English content)
    4. Auto-generated any lang  (absolute last resort before WhisperX)

On success  → status='captioned',  raw_vtt_path set, source_type='youtube_caption'
On failure  → status='needs_asr'   (goes to WhisperX in Phase 4)

Every attempt is logged to pipeline_runs.

Usage:
    uv run python pipeline/caption_downloader.py            # all 'discovered' meetings
    uv run python pipeline/caption_downloader.py --school houston_city_college
    uv run python pipeline/caption_downloader.py --limit 50
"""
import argparse
import hashlib
import itertools
import logging
import random
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
from database.models import Meeting, School, PipelineRun

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Proxy rotation
# ---------------------------------------------------------------------------

_proxy_iter = None


def _get_next_proxy():
    """Cycle through PROXY_LIST; returns None if list is empty."""
    global _proxy_iter
    if not config.PROXY_LIST:
        return None
    if _proxy_iter is None:
        _proxy_iter = itertools.cycle(config.PROXY_LIST)
    return next(_proxy_iter)


def _make_ytt_api(proxy_url=None):
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
    #
    # If you ever switch to a Webshare datacenter / static plan, fall through
    # to the GenericProxyConfig branch below and put the full
    # "user:pass@host:port" in .env exactly as the dashboard shows it.
    if host == "p.webshare.io" and username and password:
        # Strip any specific-proxy suffix the user may have copied from the
        # dashboard ("qqsgsmnw-1" → "qqsgsmnw"). Without this we'd produce
        # "qqsgsmnw-1-rotate" which Webshare rejects with HTTP 400.
        import re as _re
        clean_username = _re.sub(r"-\d+$", "", username)

        return YouTubeTranscriptApi(
            proxy_config=WebshareProxyConfig(
                proxy_username=clean_username,
                proxy_password=password,
                proxy_port=parts.port or 80,
                domain_name=parts.hostname or "p.webshare.io",
                retries_when_blocked=3,
            )
        )

    # All other proxies (Tor, custom HTTP, Webshare datacenter): just pass
    # the URL through, same shape as curl -x.
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
# Primary: youtube-transcript-api
# ---------------------------------------------------------------------------

def _attempt_via_transcript_api(video_id: str, out_path: Path) -> tuple[bool, str]:
    """
    Use youtube-transcript-api to fetch captions.
    Rotates through PROXY_LIST automatically on IpBlocked.

    Returns (success, label) where label describes which strategy worked.
    """
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
        return False, "library_missing"

    # Fetch transcript list — rotate proxy on IpBlocked
    transcript_list = None
    max_attempts = max(len(config.PROXY_LIST) + 1, 2)

    for attempt in range(max_attempts):
        # First attempt uses the first proxy (or None); subsequent attempts rotate
        proxy = config.PROXY_LIST[0] if (attempt == 0 and config.PROXY_LIST) else _get_next_proxy()
        try:
            transcript_list = _make_ytt_api(proxy).list(video_id)
            break
        except TranscriptsDisabled:
            return False, "transcripts_disabled"
        except VideoUnavailable:
            return False, "video_unavailable"
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
                return False, "ip_blocked"
            return False, f"{ename}: {short_msg}"

    transcript, label = _select_transcript(transcript_list, NoTranscriptFound)

    if transcript is None:
        return False, "no_captions_available"

    max_fetch_attempts = max(len(config.PROXY_LIST) + 1, 2)

    for attempt in range(max_fetch_attempts):
        try:
            fetched = transcript.fetch()
            vtt_content = WebVTTFormatter().format_transcript(fetched)
            out_path.write_text(vtt_content, encoding="utf-8")
            return True, label
        except Exception as e:
            ename = type(e).__name__
            is_blocked = (
                (IpBlocked and isinstance(e, IpBlocked)) or
                any(x in ename for x in ("IpBlocked", "RequestBlocked"))
            )

            if not is_blocked:
                short_msg = str(e)[:80].replace("\n", " ")
                return False, f"fetch_error:{ename}: {short_msg}"

            if attempt + 1 >= max_fetch_attempts or not config.PROXY_LIST:
                return False, "ip_blocked"

            print(f"           -> IpBlocked during fetch, rotating proxy ({attempt + 1}/{max_fetch_attempts - 1})")

            proxy = _get_next_proxy()
            try:
                transcript_list = _make_ytt_api(proxy).list(video_id)
            except TranscriptsDisabled:
                return False, "transcripts_disabled"
            except VideoUnavailable:
                return False, "video_unavailable"
            except Exception:
                continue

            transcript, label = _select_transcript(transcript_list, NoTranscriptFound)
            if transcript is None:
                return False, "no_captions_available"

    return False, "ip_blocked"



# ---------------------------------------------------------------------------
# Fallback: yt-dlp (for non-YouTube sources in future phases)
# ---------------------------------------------------------------------------

def _attempt_via_ytdlp(video_url: str, video_id: str, raw_dir: Path) -> tuple[bool, str]:
    """
    yt-dlp fallback. Used for non-YouTube sources (Vimeo, direct MP4s) AND
    for YouTube videos where youtube-transcript-api got IP-blocked — yt-dlp
    + browser cookies usually bypasses bot detection because the cookie
    represents a real logged-in YouTube session.
    """
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        return False, "ytdlp_missing"

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
        import re as _re
        base = _re.sub(r"-\d+$", "", unquote(parts.username))
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
            return True, f"ytdlp_{label}"

    return False, "no_captions_available"


# ---------------------------------------------------------------------------
# Per-meeting processing
# ---------------------------------------------------------------------------

def process_meeting(session, meeting: Meeting, school: School) -> dict:
    raw_dir = config.RAW_DIR / school.slug
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_path = raw_dir / f"{meeting.video_id}.vtt"

    # Already downloaded?
    if out_path.exists():
        meeting.raw_vtt_path = str(out_path)
        meeting.status = "captioned"
        meeting.source_type = "youtube_caption"
        return {"status": "captioned", "reason": "already_on_disk"}

    started_at = datetime.now(timezone.utc)

    # Primary: youtube-transcript-api (fast, no video resolution).
    success, reason = _attempt_via_transcript_api(meeting.video_id, out_path)

    # Fallback: yt-dlp.
    #   - Non-YouTube sources (Vimeo, direct MP4s) — primary path doesn't apply.
    #   - YouTube + ip_blocked — yt-dlp authenticated with browser cookies
    #     bypasses most bot detection because the cookie represents a real
    #     logged-in user. This is what saves us when Webshare's exit IPs are
    #     on YouTube's bot list (which they often are for datacenter plans).
    if not success:
        existing_url = getattr(meeting, "video_url", None)
        is_youtube = bool(existing_url and ("youtube.com" in existing_url or "youtu.be" in existing_url))

        # Construct a YouTube URL from the video_id if the meeting row
        # doesn't carry one — older rows may have video_url=None.
        url = existing_url or f"https://www.youtube.com/watch?v={meeting.video_id}"

        should_try_ytdlp = (existing_url and not is_youtube) or (
            (is_youtube or not existing_url) and reason == "ip_blocked"
        )

        if should_try_ytdlp:
            print(f"           ↻ retrying via yt-dlp (cookies={config.YT_DLP_COOKIES_BROWSER or 'none'})")
            success, reason = _attempt_via_ytdlp(url, meeting.video_id, raw_dir)

    finished_at = datetime.now(timezone.utc)
    duration = (finished_at - started_at).total_seconds()

    if success and out_path.exists():
        meeting.raw_vtt_path    = str(out_path)
        meeting.status          = "captioned"
        meeting.source_type     = "youtube_caption"
        meeting.file_size_bytes = out_path.stat().st_size
        meeting.file_hash       = _file_hash(out_path)
    else:
        meeting.status = "needs_asr"

    meeting.updated_at = finished_at

    run = PipelineRun(
        meeting_id=meeting.meeting_id,
        channel_id=None,
        phase="caption_download",
        status="success" if success else "failed",
        error_message=None if success else reason,
        duration_seconds=duration,
        started_at=started_at,
        finished_at=finished_at,
        retry_count=0,
    )
    session.add(run)

    return {"status": meeting.status, "reason": reason, "duration": duration}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Neo v2 Phase 3 — Caption Downloader")
    parser.add_argument("--school", help="Only process meetings for this school slug")
    parser.add_argument("--limit",  type=int, help="Stop after processing this many meetings")
    args = parser.parse_args()

    print("Neo v2 — Phase 3: Caption Acquisition")
    print("=" * 50)
    print("Method        : youtube-transcript-api (primary) + yt-dlp fallback")
    if config.PROXY_LIST:
        print(f"Proxy         : {config.PROXY_LIST[0][:40]}... ({len(config.PROXY_LIST)} endpoint(s))")
    else:
        print("Proxy         : none (set PROXY_LIST in .env to avoid IP blocks)")

    engine = create_engine(config.DATABASE_URL, echo=config.SQL_ECHO)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        query = (
            session.query(Meeting, School)
            .join(School, School.school_id == Meeting.school_id)
            .filter(Meeting.status == "discovered")
            .filter(Meeting.published_date >= date(2023, 4, 8))
            .order_by(School.school_id, Meeting.published_date.desc())
        )
        if args.school:
            query = query.filter(School.slug == args.school)
        if args.limit:
            query = query.limit(args.limit)

        meetings = query.all()

        if not meetings:
            print("[INFO] No meetings with status='discovered' found.")
            print("       All caught up, or run collector.py first.")
            sys.exit(0)

        print(f"Meetings to process : {len(meetings)}")
        print(f"Output directory    : {config.RAW_DIR}\n")

        captioned   = 0
        needs_asr   = 0
        already_had = 0
        total       = len(meetings)

        for i, (meeting, school) in enumerate(meetings, 1):
            title_short = (meeting.title or "")[:60]
            print(f"[{i:>4}/{total}] {school.slug[:20]:<20} | {title_short}")

            result = process_meeting(session, meeting, school)

            if result["reason"] == "already_on_disk":
                already_had += 1
                print(f"           → already on disk")
            elif result["status"] == "captioned":
                captioned += 1
                print(f"           → ✅ captioned  ({result['reason']}, {result['duration']:.1f}s)")
            else:
                needs_asr += 1
                print(f"           → ⚠️  needs_asr  ({result['reason']})")

            if i % 10 == 0:
                session.commit()
                print(f"           [checkpoint at {i}]")

            time.sleep(random.uniform(2.5, 5.0))  # randomised delay

        session.commit()

    total_captioned = captioned + already_had
    print("\n" + "=" * 50)
    print(f"Caption acquisition complete:")
    print(f"  Captioned   : {total_captioned}  (YouTube captions found)")
    print(f"  Needs ASR   : {needs_asr}  (no captions — will use WhisperX)")
    print(f"  Total       : {total}")

    if total > 0:
        pct = round(total_captioned / total * 100)
        print(f"\n  Caption coverage: {pct}% ✅" if pct >= 70
              else f"\n  Caption coverage: {pct}% — WhisperX will handle the rest")

    print(f"\nVerify with:")
    print(f'  psql -U postgres -d neo_v2 -c "SELECT status, COUNT(*) FROM meetings GROUP BY status ORDER BY status;"')


if __name__ == "__main__":
    main()
