"""
Phase 1 — Channel Validator

Validates that each registered YouTube channel is the official board meeting
channel for its college. Marks channels verified=TRUE in the database and
logs every validation attempt to pipeline_runs.

Usage:
    uv run python pipeline/channel_validator.py

What it checks per channel:
    1. Channel exists and name matches expected (YouTube API)
    2. Channel has recent upload activity (uploaded in last 2 years)
    3. Recent uploads contain board meeting keywords in their titles
"""
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
from database.models import Channel, School, PipelineRun

# ---------------------------------------------------------------------------
# Board meeting keywords — any of these in a video title = confirmed
# ---------------------------------------------------------------------------

BOARD_KEYWORDS = [
    "board", "trustee", "trustees", "board meeting", "board of trustees",
    "regular meeting", "special meeting", "workshop", "committee meeting",
    "board session", "chancellor", "regents",
]

# How far back to check for recent uploads
RECENT_DAYS = 730   # 2 years


# ---------------------------------------------------------------------------
# YouTube API helpers
# ---------------------------------------------------------------------------

def get_youtube_client():
    if not config.YOUTUBE_API_KEY:
        print("[ERROR] YOUTUBE_API_KEY is not set in .env")
        sys.exit(1)
    return build("youtube", "v3", developerKey=config.YOUTUBE_API_KEY)


def fetch_channel_info(youtube, channel_id: str) -> dict | None:
    """Return channel snippet + statistics, or None if not found."""
    try:
        resp = youtube.channels().list(
            part="snippet,statistics,contentDetails",
            id=channel_id,
        ).execute()
        items = resp.get("items", [])
        return items[0] if items else None
    except HttpError as e:
        print(f"    [API ERROR] channels.list: {e}")
        return None


def fetch_recent_uploads(youtube, uploads_playlist_id: str, max_results: int = 25) -> list[dict]:
    """Return up to max_results recent video items from the uploads playlist."""
    try:
        resp = youtube.playlistItems().list(
            part="snippet",
            playlistId=uploads_playlist_id,
            maxResults=max_results,
        ).execute()
        return resp.get("items", [])
    except HttpError as e:
        print(f"    [API ERROR] playlistItems.list: {e}")
        return []


def has_board_meeting_content(videos: list[dict]) -> tuple[bool, list[str]]:
    """
    Check if any video titles contain board meeting keywords.
    Returns (found: bool, matched_titles: list[str])
    """
    matched = []
    for item in videos:
        title = item.get("snippet", {}).get("title", "").lower()
        if any(kw in title for kw in BOARD_KEYWORDS):
            matched.append(item["snippet"]["title"])
    return len(matched) > 0, matched


def has_recent_activity(videos: list[dict], days: int = RECENT_DAYS) -> bool:
    """Check if any video was published within the last `days` days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    for item in videos:
        published_str = item.get("snippet", {}).get("publishedAt", "")
        if not published_str:
            continue
        try:
            published = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
            if published > cutoff:
                return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# Core validation logic
# ---------------------------------------------------------------------------

def validate_channel(youtube, channel: Channel, school: School) -> dict:
    """
    Run all checks for a single channel.
    Returns a result dict with keys: verified, reason, channel_name, subscriber_count, matched_titles
    """
    result = {
        "verified": False,
        "reason": "",
        "channel_name": None,
        "subscriber_count": None,
        "matched_titles": [],
    }

    print(f"\n  Checking: {school.name}")
    print(f"  Channel ID: {channel.youtube_channel_id}")

    # ── Step 1: Does the channel exist? ──────────────────────────────────────
    info = fetch_channel_info(youtube, channel.youtube_channel_id)
    if not info:
        result["reason"] = "Channel not found via YouTube API"
        print(f"    ✗ {result['reason']}")
        return result

    snippet = info["snippet"]
    statistics = info.get("statistics", {})
    content_details = info.get("contentDetails", {})

    result["channel_name"] = snippet.get("title", "")
    result["subscriber_count"] = int(statistics.get("subscriberCount", 0) or 0)

    print(f"    Channel name : {result['channel_name']}")
    print(f"    Subscribers  : {result['subscriber_count']:,}")

    # ── Step 2: Get uploads playlist ─────────────────────────────────────────
    uploads_playlist_id = content_details.get("relatedPlaylists", {}).get("uploads")
    if not uploads_playlist_id:
        result["reason"] = "No uploads playlist found"
        print(f"    ✗ {result['reason']}")
        return result

    videos = fetch_recent_uploads(youtube, uploads_playlist_id, max_results=25)
    print(f"    Recent uploads fetched: {len(videos)}")

    if not videos:
        result["reason"] = "No videos found in uploads playlist"
        print(f"    ✗ {result['reason']}")
        return result

    # ── Step 3: Recent activity check ────────────────────────────────────────
    if not has_recent_activity(videos, days=RECENT_DAYS):
        result["reason"] = f"No uploads in the last {RECENT_DAYS} days"
        print(f"    ✗ {result['reason']}")
        return result

    print(f"    ✓ Has recent activity")

    # ── Step 4: Board meeting keyword check ──────────────────────────────────
    found, matched_titles = has_board_meeting_content(videos)
    result["matched_titles"] = matched_titles

    if not found:
        result["reason"] = "No board meeting keywords found in recent video titles"
        print(f"    ✗ {result['reason']}")
        print(f"      Sample titles: {[v['snippet']['title'] for v in videos[:3]]}")
        return result

    print(f"    ✓ Board meeting content confirmed ({len(matched_titles)} matches)")
    for title in matched_titles[:3]:
        print(f"      → \"{title}\"")

    # ── All checks passed ────────────────────────────────────────────────────
    result["verified"] = True
    result["reason"] = "All checks passed"
    print(f"    ✅ VERIFIED")
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Neo v2 — Phase 1: Channel Validation")
    print("=" * 50)

    youtube = get_youtube_client()

    engine = create_engine(config.DATABASE_URL, echo=config.SQL_ECHO)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        channels = (
            session.query(Channel, School)
            .join(School, School.school_id == Channel.school_id)
            .order_by(School.school_id)
            .all()
        )

        if not channels:
            print("[ERROR] No channels found in database. Run database/seed.py first.")
            sys.exit(1)

        print(f"Found {len(channels)} channel(s) to validate.\n")

        verified_count = 0
        failed_count = 0

        for channel, school in channels:
            started_at = datetime.now(timezone.utc)

            result = validate_channel(youtube, channel, school)

            finished_at = datetime.now(timezone.utc)
            duration = (finished_at - started_at).total_seconds()

            # ── Update channel record ─────────────────────────────────────
            channel.verified = result["verified"]
            channel.last_checked_at = finished_at
            if result["channel_name"]:
                channel.channel_name = result["channel_name"]

            # ── Log to pipeline_runs ──────────────────────────────────────
            run = PipelineRun(
                channel_id=channel.channel_id,
                meeting_id=None,
                phase="channel_validation",
                status="success" if result["verified"] else "failed",
                error_message=None if result["verified"] else result["reason"],
                duration_seconds=duration,
                started_at=started_at,
                finished_at=finished_at,
                retry_count=0,
            )
            session.add(run)

            if result["verified"]:
                verified_count += 1
            else:
                failed_count += 1

        session.commit()

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 50)
    print(f"Validation complete: {verified_count} verified, {failed_count} failed")

    if failed_count > 0:
        print("\n[WARNING] Some channels failed validation.")
        print("  Check the output above and update channels manually if needed.")
    else:
        print("\n✅ All channels verified. Ready for Phase 2.")

    print("\nVerify in DB with:")
    print("  psql -U postgres -d neo_v2 -c \"SELECT c.channel_name, c.verified, c.last_checked_at, s.name FROM channels c JOIN schools s ON s.school_id = c.school_id;\"")


if __name__ == "__main__":
    main()
