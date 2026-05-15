"""
Phase 2 — Video Collector

Discovers board meeting videos for each verified channel and inserts one row
per video into the meetings table with status='discovered'.

Replaces v1's CSV approach — the database is the single source of truth.

Usage:
    uv run python pipeline/collector.py

Filters applied:
    - Title must contain at least one board meeting keyword
    - Duration must be >= MIN_DURATION_SECONDS (default 10 minutes)
    - Published date must be >= DATE_CUTOFF (default Jan 1 2020)
    - Skips videos already in the meetings table (idempotent)
"""
import sys
import subprocess
import json
from datetime import datetime, timezone, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
from database.models import Channel, School, Meeting, PipelineRun

# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

BOARD_KEYWORDS = [
    "board", "trustee", "trustees", "board meeting", "board of trustees",
    "regular meeting", "special meeting", "workshop", "committee meeting",
    "board session", "regents",
]

MIN_DURATION_SECONDS = 600        # 10 minutes — filters out shorts / trailers
DATE_CUTOFF = date(2023, 4, 8)    # ignore anything older than this
MAX_RESULTS_PER_PAGE = 50         # YouTube API max per request
MAX_PAGES = 20                    # cap at 1000 videos per channel


# ---------------------------------------------------------------------------
# Keyword filter
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# YouTube API helpers
# ---------------------------------------------------------------------------

def get_youtube_client():
    if not config.YOUTUBE_API_KEY:
        print("[ERROR] YOUTUBE_API_KEY not set in .env")
        sys.exit(1)
    return build("youtube", "v3", developerKey=config.YOUTUBE_API_KEY)


def get_uploads_playlist_id(youtube, channel_id: str) -> str | None:
    """Return the uploads playlist ID for a channel."""
    try:
        resp = youtube.channels().list(
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


def fetch_all_uploads(youtube, playlist_id: str) -> list[dict]:
    """
    Page through the uploads playlist and return all video items.
    Stops early if we hit DATE_CUTOFF or MAX_PAGES.
    """
    videos = []
    page_token = None
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

            resp = youtube.playlistItems().list(**kwargs).execute()
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


# ---------------------------------------------------------------------------
# yt-dlp metadata probe
# ---------------------------------------------------------------------------

def probe_video(video_id: str) -> dict:
    """
    Use yt-dlp to get exact duration and format availability.
    Returns dict with duration_seconds, has_formats.
    Falls back gracefully if yt-dlp fails.
    """
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
# Per-channel collection
# ---------------------------------------------------------------------------

def collect_channel(
    youtube,
    session,
    channel: Channel,
    school: School,
) -> dict:
    """
    Collect all board meeting videos for one channel.
    Returns stats dict: total_found, inserted, skipped, filtered_out.
    """
    stats = {"total_found": 0, "inserted": 0, "skipped": 0, "filtered_out": 0}

    print(f"\n  [{school.slug}] {school.name}")
    print(f"  Channel: {channel.channel_name} ({channel.youtube_channel_id})")

    # ── Get uploads playlist ──────────────────────────────────────────────────
    playlist_id = get_uploads_playlist_id(youtube, channel.youtube_channel_id)
    if not playlist_id:
        print(f"    [ERROR] Could not get uploads playlist. Skipping.")
        return stats

    # ── Fetch all uploads ─────────────────────────────────────────────────────
    print(f"    Fetching uploads playlist...", end=" ", flush=True)
    uploads = fetch_all_uploads(youtube, playlist_id)
    print(f"{len(uploads)} videos found.")
    stats["total_found"] = len(uploads)

    inserted = 0
    skipped = 0
    filtered_out = 0

    for item in uploads:
        snippet = item["snippet"]
        video_id = snippet.get("resourceId", {}).get("videoId", "")
        title = snippet.get("title", "")
        published_str = snippet.get("publishedAt", "")

        if not video_id or not title:
            filtered_out += 1
            continue

        # ── Keyword filter ────────────────────────────────────────────────────
        if not is_board_meeting(title):
            filtered_out += 1
            continue

        # ── Date filter ───────────────────────────────────────────────────────
        published_date = None
        if published_str:
            try:
                published_date = datetime.fromisoformat(
                    published_str.replace("Z", "+00:00")
                ).date()
                if published_date < DATE_CUTOFF:
                    filtered_out += 1
                    continue
            except ValueError:
                pass

        # ── Already in DB? ────────────────────────────────────────────────────
        existing = session.query(Meeting).filter_by(video_id=video_id).first()
        if existing:
            skipped += 1
            continue

        # ── yt-dlp probe for duration ─────────────────────────────────────────
        print(f"    + Probing: {title[:65]}...")
        probe = probe_video(video_id)
        duration = probe["duration_seconds"]

        # ── Future live event filter ──────────────────────────────────────────
        # yt-dlp sets is_live=True and duration=None for scheduled streams
        # that haven't aired yet. Skip them — they'll be re-collected once live.
        if probe.get("is_live") or probe.get("duration_seconds") is None and not probe.get("has_formats"):
            filtered_out += 1
            print(f"      ✗ Future/live event, skipping.")
            continue

        # ── Duration filter ───────────────────────────────────────────────────
        if duration is not None and duration < MIN_DURATION_SECONDS:
            filtered_out += 1
            print(f"      ✗ Too short ({duration:.0f}s), skipping.")
            continue

        # ── Insert into meetings ──────────────────────────────────────────────
        meeting = Meeting(
            school_id=school.school_id,
            video_id=video_id,
            video_url=f"https://www.youtube.com/watch?v={video_id}",
            title=title,
            published_date=published_date,
            duration_seconds=int(duration) if duration else None,
            meeting_type=detect_meeting_type(title),
            status="discovered",
            source_type=None,           # determined in Phase 3/4
        )
        session.add(meeting)
        session.flush()
        inserted += 1
        print(f"      ✓ Inserted (meeting_id={meeting.meeting_id}, "
              f"duration={duration:.0f}s)" if duration else
              f"      ✓ Inserted (meeting_id={meeting.meeting_id})")

    stats["inserted"] = inserted
    stats["skipped"] = skipped
    stats["filtered_out"] = filtered_out

    print(f"\n    Summary: {inserted} inserted, {skipped} already in DB, "
          f"{filtered_out} filtered out")

    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Neo v2 — Phase 2: Video Collection")
    print("=" * 50)

    youtube = get_youtube_client()
    engine = create_engine(config.DATABASE_URL, echo=config.SQL_ECHO)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        # Only collect from verified channels
        channels = (
            session.query(Channel, School)
            .join(School, School.school_id == Channel.school_id)
            .filter(Channel.verified == True)
            .order_by(School.school_id)
            .all()
        )

        if not channels:
            print("[ERROR] No verified channels found. Run channel_validator.py first.")
            sys.exit(1)

        print(f"Collecting from {len(channels)} verified channel(s).")
        print(f"Filters: keywords={BOARD_KEYWORDS[:3]}..., "
              f"min_duration={MIN_DURATION_SECONDS}s, "
              f"cutoff={DATE_CUTOFF}\n")

        total_inserted = 0
        total_skipped = 0
        total_filtered = 0

        for channel, school in channels:
            started_at = datetime.now(timezone.utc)

            stats = collect_channel(youtube, session, channel, school)

            finished_at = datetime.now(timezone.utc)
            duration_sec = (finished_at - started_at).total_seconds()

            # Log to pipeline_runs
            run = PipelineRun(
                channel_id=channel.channel_id,
                meeting_id=None,
                phase="video_collection",
                status="success" if stats["inserted"] >= 0 else "failed",
                error_message=None,
                duration_seconds=duration_sec,
                started_at=started_at,
                finished_at=finished_at,
                retry_count=0,
            )
            session.add(run)

            total_inserted += stats["inserted"]
            total_skipped += stats["skipped"]
            total_filtered += stats["filtered_out"]

        session.commit()

    # ── Final summary ─────────────────────────────────────────────────────────
    print("\n" + "=" * 50)
    print(f"Collection complete:")
    print(f"  Inserted : {total_inserted} new meetings")
    print(f"  Skipped  : {total_skipped} already in DB")
    print(f"  Filtered : {total_filtered} (wrong keywords / too short / too old)")
    print(f"\n✅ meetings table populated. Ready for Phase 3 (caption acquisition).")
    print(f"\nVerify with:")
    print(f'  psql -U postgres -d neo_v2 -c "SELECT s.name, COUNT(*) as videos FROM meetings m JOIN schools s ON s.school_id = m.school_id GROUP BY s.name ORDER BY s.name;"')


if __name__ == "__main__":
    main()
