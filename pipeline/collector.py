"""
Phase 2 — Video Collector (orchestrator)

For every active school, dispatch to the adapter
registered for that school's source_type. The adapter yields
``DiscoveredMeeting`` records; the orchestrator deduplicates against the
``meetings`` table and inserts new rows with status='discovered'.

Platform-specific discovery (YouTube Data API paging, Panopto folder
listing, Ravnur portal scrape, ...) lives in ``pipeline.sources``.

Behavior change vs the previous YouTube-only collector: PipelineRun is
now logged per-school (channel_id=None) instead of per-channel. Nothing
downstream reads channel_id off PipelineRun.

Usage:
    uv run python pipeline/collector.py

Filters applied (all enforced inside the adapter for YouTube):
    - Title must contain at least one board meeting keyword
    - Duration must be >= MIN_DURATION_SECONDS
    - Published date must be >= DATE_CUTOFF
    - Skips videos already in the meetings table (idempotent)
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
from database.models import School, Meeting, PipelineRun
from pipeline.sources import for_school


# ---------------------------------------------------------------------------
# Per-school collection
# ---------------------------------------------------------------------------

def collect_school(session, school: School) -> dict:
    """
    Walk every DiscoveredMeeting the adapter yields for this school and
    insert previously-unseen ones into the meetings table.

    Returns stats: total_found, inserted, skipped, filtered_out (kept for
    operator UX continuity with the v1 output).
    """
    stats = {"total_found": 0, "inserted": 0, "skipped": 0, "filtered_out": 0}

    adapter = for_school(school)
    print(f"\n  [{school.slug}] {school.name}   (adapter: {adapter.source_type})")

    for candidate in adapter.discover_meetings(school):
        stats["total_found"] += 1

        # Idempotency check
        existing = session.query(Meeting).filter_by(video_id=candidate.video_id).first()
        if existing:
            stats["skipped"] += 1
            continue

        meeting_type = candidate.raw_metadata.get("meeting_type") if candidate.raw_metadata else None

        meeting = Meeting(
            school_id=school.school_id,
            video_id=candidate.video_id,
            video_url=candidate.video_url,
            title=candidate.title,
            published_date=candidate.published_date,
            duration_seconds=candidate.duration_seconds,
            meeting_type=meeting_type,
            status="discovered",
            source_type=None,           # determined in Phase 3/4
        )
        session.add(meeting)
        session.flush()
        stats["inserted"] += 1
        print(
            f"      ✓ Inserted (meeting_id={meeting.meeting_id}, "
            f"duration={candidate.duration_seconds}s)"
        )

    print(
        f"\n    Summary: {stats['inserted']} inserted, "
        f"{stats['skipped']} already in DB"
    )
    return stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Neo v2 Phase 2 — Video Collector")
    parser.add_argument("--school", help="Only run discovery for this school slug")
    args = parser.parse_args()

    print("Neo v2 — Phase 2: Video Collection")
    print("=" * 50)

    engine = create_engine(config.DATABASE_URL, echo=config.SQL_ECHO)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        # Each adapter applies its own eligibility checks.
        query = (
            session.query(School)
            .filter(School.is_active == True)  # noqa: E712
            .order_by(School.school_id)
        )
        if args.school:
            query = query.filter(School.slug == args.school)
        schools = query.all()

        if not schools:
            target = f" for school={args.school!r}" if args.school else ""
            print(f"[ERROR] No active schools found{target}.")
            sys.exit(1)

        print(f"Collecting from {len(schools)} school(s).\n")

        total_inserted = 0
        total_skipped = 0
        total_filtered = 0

        for school in schools:
            started_at = datetime.now(timezone.utc)

            try:
                stats = collect_school(session, school)
                run_status = "success"
                err_msg = None
            except Exception as e:
                stats = {"total_found": 0, "inserted": 0, "skipped": 0, "filtered_out": 0}
                run_status = "failed"
                err_msg = f"{type(e).__name__}: {str(e)[:200]}"
                print(f"    [ERROR] {school.slug}: {err_msg}")

            finished_at = datetime.now(timezone.utc)
            duration_sec = (finished_at - started_at).total_seconds()

            run = PipelineRun(
                channel_id=None,
                meeting_id=None,
                phase="video_collection",
                status=run_status,
                error_message=err_msg,
                duration_seconds=duration_sec,
                started_at=started_at,
                finished_at=finished_at,
                retry_count=0,
            )
            session.add(run)

            total_inserted += stats["inserted"]
            total_skipped  += stats["skipped"]
            total_filtered += stats["filtered_out"]

        session.commit()

    print("\n" + "=" * 50)
    print(f"Collection complete:")
    print(f"  Inserted : {total_inserted} new meetings")
    print(f"  Skipped  : {total_skipped} already in DB")
    print(f"\n✅ meetings table populated. Ready for Phase 3 (caption acquisition).")
    print(f"\nVerify with:")
    print(f'  psql -U postgres -d neo_v2 -c "SELECT s.name, COUNT(*) as videos FROM meetings m JOIN schools s ON s.school_id = m.school_id GROUP BY s.name ORDER BY s.name;"')


if __name__ == "__main__":
    main()
