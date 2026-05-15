"""
Phase 3 — Caption Downloader (orchestrator)

For every meeting with status='discovered', dispatch caption fetching to
the adapter registered for the meeting's school, write the returned VTT
bytes to disk, and update the Meeting + PipelineRun rows uniformly.

Platform-specific logic (youtube-transcript-api, yt-dlp, Panopto, Ravnur)
lives in ``pipeline.sources``. This file owns:
  * status-driven query for eligible meetings
  * adapter dispatch
  * disk write + sha256
  * meeting state update + PipelineRun logging

On success  → status='captioned',  raw_vtt_path set, source_type=<adapter.source_type>
On failure  → status='needs_asr'   (goes to WhisperX in Phase 4)

Usage:
    uv run python pipeline/caption_downloader.py            # all 'discovered' meetings
    uv run python pipeline/caption_downloader.py --school houston_city_college
    uv run python pipeline/caption_downloader.py --limit 50
"""
import argparse
import hashlib
import logging
import random
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
from database.models import Meeting, School, PipelineRun
from pipeline.sources import for_school
from pipeline.sources.base import FetchResult

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


def _looks_like_vtt(body: bytes) -> bool:
    """True if ``body`` starts with the WEBVTT header (BOM tolerated).

    Guards the disk-write step against a 200-OK response whose body isn't
    actually VTT — observed in the wild on some platforms when a caption
    isn't ready yet. Downstream cleaner.py would otherwise produce zero
    chunks for that meeting and silently strand it at status='captioned'.
    """
    if body.startswith(b"\xef\xbb\xbf"):
        body = body[3:]
    return body.lstrip().startswith(b"WEBVTT")


# ---------------------------------------------------------------------------
# Per-meeting processing
# ---------------------------------------------------------------------------

def process_meeting(session, meeting: Meeting, school: School) -> dict:
    raw_dir = config.RAW_DIR / school.slug
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_path = raw_dir / f"{meeting.video_id}.vtt"

    adapter = for_school(school)

    # Already downloaded? — short-circuit before any platform calls.
    if out_path.exists():
        meeting.raw_vtt_path = str(out_path)
        meeting.status = "captioned"
        meeting.source_type = adapter.source_type
        return {"status": "captioned", "reason": "already_on_disk"}

    started_at = datetime.now(timezone.utc)

    result = adapter.fetch_captions(meeting)

    if result.success and result.vtt_bytes and not _looks_like_vtt(result.vtt_bytes):
        result = FetchResult(False, "error:invalid_vtt", None)

    if result.success and result.vtt_bytes:
        out_path.write_bytes(result.vtt_bytes)

    finished_at = datetime.now(timezone.utc)
    duration = (finished_at - started_at).total_seconds()

    if result.success and out_path.exists():
        meeting.raw_vtt_path    = str(out_path)
        meeting.status          = "captioned"
        meeting.source_type     = adapter.source_type
        meeting.file_size_bytes = out_path.stat().st_size
        meeting.file_hash       = _file_hash(out_path)
    else:
        meeting.status = "needs_asr"

    meeting.updated_at = finished_at

    run = PipelineRun(
        meeting_id=meeting.meeting_id,
        channel_id=None,
        phase="caption_download",
        status="success" if result.success else "failed",
        error_message=None if result.success else result.reason,
        duration_seconds=duration,
        started_at=started_at,
        finished_at=finished_at,
        retry_count=0,
    )
    session.add(run)

    return {"status": meeting.status, "reason": result.reason, "duration": duration}


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
    print("Method        : per-school adapter dispatch (pipeline.sources)")
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
    print(f"  Captioned   : {total_captioned}  (captions found)")
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
