"""
Archive meetings that took place before config.MEETING_YEAR_CUTOFF.

The pilot only wants recent board business, but the pipeline's age filter used
to compare the *upload* date against the cutoff, so backfilled channels smuggled
old meetings in. This retires them from the corpus:

  * meetings.status     -> 'archived_old'   (in no phase's INPUTS, so no
                                             --reprocess/--recheck revives them)
  * meetings.is_active  -> False
  * meetings.deleted_at -> now
  * their Qdrant points are deleted, so they stop being retrievable

Soft delete on purpose: chunks and extraction rows are left intact, so raising
the cutoff later is a status flip plus a re-index, not a re-ingest.

Usage:
    uv run python scripts/archive_old_meetings.py --dry-run
    uv run python scripts/archive_old_meetings.py
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
from database.models import Meeting, School
from pipeline.meeting_dates import resolve_meeting_year
from pipeline.states import ARCHIVED_OLD, INDEXED, REJECTED


def _delete_qdrant_points(meeting_ids: list[int]) -> int:
    """Remove every point whose payload meeting_id is in the list."""
    from qdrant_client import QdrantClient
    from qdrant_client.models import (
        FilterSelector, Filter, FieldCondition, MatchAny,
    )

    client = QdrantClient(host=config.QDRANT_HOST, port=config.QDRANT_PORT)
    before = client.get_collection(config.QDRANT_COLLECTION).points_count
    client.delete(
        collection_name=config.QDRANT_COLLECTION,
        points_selector=FilterSelector(filter=Filter(must=[
            FieldCondition(key="meeting_id", match=MatchAny(any=meeting_ids))
        ])),
        wait=True,
    )
    after = client.get_collection(config.QDRANT_COLLECTION).points_count
    return before - after


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, touch nothing")
    ap.add_argument("--cutoff", type=int, default=config.MEETING_YEAR_CUTOFF,
                    help=f"year cutoff (default {config.MEETING_YEAR_CUTOFF})")
    args = ap.parse_args()

    print(f"Archiving meetings before {args.cutoff}")
    print("=" * 60)

    engine  = create_engine(config.DATABASE_URL, echo=config.SQL_ECHO)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        rows = (
            session.query(Meeting, School)
            .join(School, School.school_id == Meeting.school_id)
            .filter(Meeting.status.in_((INDEXED, REJECTED)))
            .all()
        )

        to_archive, unresolved = [], []
        for meeting, school in rows:
            year, how = resolve_meeting_year(
                meeting.title, meeting.published_date, school.slug
            )
            if year is None:
                # Upload date is a hard upper bound even when otherwise
                # untrusted: the meeting cannot postdate its own recording.
                if meeting.published_date and meeting.published_date.year < args.cutoff:
                    to_archive.append((meeting, school, meeting.published_date.year,
                                       "upload-date upper bound"))
                else:
                    unresolved.append((meeting, school))
                continue
            if year < args.cutoff:
                to_archive.append((meeting, school, year, how))

        by_status = Counter(m.status for m, _, _, _ in to_archive)
        by_school = Counter(s.slug for _, s, _, _ in to_archive)

        print(f"\nto archive: {len(to_archive)}")
        for status, n in sorted(by_status.items()):
            print(f"  was {status:<10} {n}")
        print("\nby school:")
        for slug, n in by_school.most_common():
            print(f"  {slug:<28} {n}")
        print(f"\nleft unresolved (untouched): {len(unresolved)}")
        for m, s in unresolved:
            print(f"  {m.meeting_id:>5} {s.slug:<24} {(m.title or '')[:44]}")

        indexed_ids = [m.meeting_id for m, _, _, _ in to_archive
                       if m.status == INDEXED]

        if args.dry_run:
            print(f"\n[dry run] would delete Qdrant points for "
                  f"{len(indexed_ids)} indexed meeting(s); nothing written")
            return

        removed = 0
        if indexed_ids:
            removed = _delete_qdrant_points(indexed_ids)
            print(f"\nQdrant: removed {removed} point(s) for "
                  f"{len(indexed_ids)} meeting(s)")

        now = datetime.now(timezone.utc)
        for meeting, _, _, _ in to_archive:
            meeting.status     = ARCHIVED_OLD
            meeting.is_active  = False
            meeting.deleted_at = now
        session.commit()
        print(f"Postgres: archived {len(to_archive)} meeting(s)")
        print("\nARCHIVE_OK")


if __name__ == "__main__":
    main()
