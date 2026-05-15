"""
Reset meeting status — useful when a pipeline phase ran into a transient
problem (proxy down, network blip, bad cookies) and incorrectly demoted
meetings into a status that won't be retried.

Usage:
    uv run python scripts/reset_status.py needs_asr discovered
    uv run python scripts/reset_status.py rejected discovered
    uv run python scripts/reset_status.py extracted processed --school houston_city_college
    uv run python scripts/reset_status.py --dry-run needs_asr discovered
"""
from __future__ import annotations

import argparse
import sys

from api.db.session import SessionLocal
from pipeline.states import ALL_STATUSES
from sqlalchemy import text

# Pipeline statuses that any phase recognizes. Sourced from pipeline.states so
# adding a new status is a one-file change. Anything not in this set is almost
# certainly a typo — we refuse rather than silently leaving meetings stranded
# in a status no script will ever pick up.
_KNOWN_STATUSES: set[str] = set(ALL_STATUSES)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("from_status", help="Current status to match")
    ap.add_argument("to_status",   help="Status to flip them to")
    ap.add_argument("--school",    default=None, help="Limit to one school slug")
    ap.add_argument("--dry-run",   action="store_true", help="Count only, do not write")
    ap.add_argument("--force",     action="store_true",
                    help="Bypass status validation (use only when adding a new status mid-migration)")
    args = ap.parse_args()

    # Catch typos like "discovere" → would otherwise silently strand 6 meetings
    # in a status no pipeline phase recognizes.
    if not args.force:
        for label, value in (("from_status", args.from_status), ("to_status", args.to_status)):
            if value not in _KNOWN_STATUSES:
                print(f"ERROR: '{value}' is not a known meeting status (--{label}).", file=sys.stderr)
                print(f"Known: {sorted(_KNOWN_STATUSES)}", file=sys.stderr)
                print(f"If you really meant it, re-run with --force.", file=sys.stderr)
                return 2

    db = SessionLocal()
    try:
        # Show what we'd touch first (always safe, useful as audit).
        params = {"f": args.from_status, "school": args.school}
        rows = db.execute(text("""
            SELECT s.slug, COUNT(*) AS n
            FROM meetings m
            JOIN schools s ON s.school_id = m.school_id
            WHERE m.status = :f
              AND (:school IS NULL OR s.slug = :school)
            GROUP BY s.slug
            ORDER BY n DESC
        """), params).fetchall()

        if not rows:
            print(f"No meetings in status='{args.from_status}'"
                  + (f" for school={args.school}" if args.school else "") + ".")
            return 0

        print(f"Would update from '{args.from_status}' -> '{args.to_status}':")
        for r in rows:
            print(f"  {r._mapping['slug']:<28}  {r._mapping['n']:>3}")
        total = sum(r._mapping["n"] for r in rows)
        print(f"  {'total':<28}  {total:>3}")

        if args.dry_run:
            print("\n(dry-run — no changes written)")
            return 0

        n = db.execute(text("""
            UPDATE meetings SET status = :t
            WHERE meeting_id IN (
                SELECT m.meeting_id FROM meetings m
                JOIN schools s ON s.school_id = m.school_id
                WHERE m.status = :f
                  AND (:school IS NULL OR s.slug = :school)
            )
        """), {"f": args.from_status, "t": args.to_status, "school": args.school}).rowcount
        db.commit()
        print(f"\nReset {n} meetings: '{args.from_status}' -> '{args.to_status}'.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
