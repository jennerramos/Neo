"""
Recover meetings stuck in terminal-failure states by resetting them to the
input status of the phase that failed. The next pipeline run picks them up.

Three failure states have no automatic recovery path:

    asr_failed         → needs_asr        (re-run pipeline/asr_processor.py)
    processing_failed  → transcribed      (re-run pipeline/data_packager.py)
                         or captioned      (chosen by source_type)
    rejected           → processed        (re-gate via pipeline/quality_gate.py;
                                           equivalent to `quality_gate --recheck`)

Use --dry-run first. Always.

Usage:
    uv run python scripts/recover_failed.py asr_failed
    uv run python scripts/recover_failed.py processing_failed --school houston_city_college
    uv run python scripts/recover_failed.py rejected --dry-run
    uv run python scripts/recover_failed.py all
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from sqlalchemy import text

from api.db.session import SessionLocal
from pipeline.states import RECOVERY_TARGETS as _RECOVERY_PLAN

# Mapping: failure-state → (recovery-status, optional source_type override map).
# Defined in pipeline.states so the state-machine spec is single-sourced.
# For processing_failed, the packager accepts both "captioned" and "transcribed"
# inputs, so we route by source_type when available. Default to "transcribed"
# (the WhisperX path) since that is the dominant volume in production.


def _summarize(db, from_status: str, school: Optional[str]) -> list[dict]:
    """Group affected meetings by school and source_type."""
    rows = db.execute(text("""
        SELECT s.slug                AS school_slug,
               COALESCE(m.source_type, '(null)') AS source_type,
               COUNT(*)              AS n
        FROM meetings m
        JOIN schools s ON s.school_id = m.school_id
        WHERE m.status = :f
          AND (:school IS NULL OR s.slug = :school)
        GROUP BY s.slug, m.source_type
        ORDER BY s.slug, source_type
    """), {"f": from_status, "school": school}).fetchall()
    return [dict(r._mapping) for r in rows]


def _resolve_target(plan: dict, source_type: Optional[str]) -> str:
    """Pick the recovery status for a single meeting given its source_type."""
    by_src = plan.get("by_source_type")
    if by_src and source_type and source_type in by_src:
        return by_src[source_type]
    return plan["to"]


def _recover_one(db, from_status: str, school: Optional[str], dry_run: bool) -> int:
    plan = _RECOVERY_PLAN[from_status]
    rows = _summarize(db, from_status, school)

    if not rows:
        print(f"  [{from_status}] no meetings to recover"
              + (f" for school={school}" if school else "") + ".")
        return 0

    print(f"  [{from_status}] would recover:")
    total = 0
    # Group display by destination status for readability.
    by_dest: dict[str, list[dict]] = {}
    for r in rows:
        dest = _resolve_target(plan, r["source_type"] if r["source_type"] != "(null)" else None)
        by_dest.setdefault(dest, []).append(r)
        total += r["n"]

    for dest, group in by_dest.items():
        gtotal = sum(r["n"] for r in group)
        print(f"    → {dest}  ({gtotal})")
        for r in group:
            print(f"        {r['school_slug']:<28} src={r['source_type']:<18} {r['n']:>3}")

    if dry_run:
        return total

    # Actual update.  Done in one statement per destination so the source_type
    # routing is honoured atomically and we keep transaction boundaries small.
    written = 0
    for dest, group in by_dest.items():
        # Build the predicate that picks meetings whose source_type would route
        # to this destination.  When the plan has no source_type routing, all
        # meetings go to the single default destination.
        if plan.get("by_source_type"):
            src_types_for_dest = [
                src for src, d in plan["by_source_type"].items() if d == dest
            ]
            # Meetings with NULL source_type or a source_type not in the routing
            # table fall through to the default destination.
            if dest == plan["to"]:
                # Default bucket: NULL source_type, or anything not explicitly mapped.
                explicitly_mapped = list(plan["by_source_type"].keys())
                n = db.execute(text("""
                    UPDATE meetings SET status = :t
                    WHERE meeting_id IN (
                        SELECT m.meeting_id FROM meetings m
                        JOIN schools s ON s.school_id = m.school_id
                        WHERE m.status = :f
                          AND (:school IS NULL OR s.slug = :school)
                          AND (m.source_type IS NULL OR m.source_type NOT IN :explicit_sources)
                    )
                """).bindparams(__import__("sqlalchemy").bindparam("explicit_sources", expanding=True)),
                    {"f": from_status, "t": dest, "school": school,
                     "explicit_sources": explicitly_mapped or [""]}).rowcount
            else:
                n = db.execute(text("""
                    UPDATE meetings SET status = :t
                    WHERE meeting_id IN (
                        SELECT m.meeting_id FROM meetings m
                        JOIN schools s ON s.school_id = m.school_id
                        WHERE m.status = :f
                          AND (:school IS NULL OR s.slug = :school)
                          AND m.source_type IN :sources
                    )
                """).bindparams(__import__("sqlalchemy").bindparam("sources", expanding=True)),
                    {"f": from_status, "t": dest, "school": school,
                     "sources": src_types_for_dest}).rowcount
        else:
            n = db.execute(text("""
                UPDATE meetings SET status = :t
                WHERE meeting_id IN (
                    SELECT m.meeting_id FROM meetings m
                    JOIN schools s ON s.school_id = m.school_id
                    WHERE m.status = :f
                      AND (:school IS NULL OR s.slug = :school)
                )
            """), {"f": from_status, "t": dest, "school": school}).rowcount
        written += n
    db.commit()
    print(f"  [{from_status}] recovered {written} meetings.")
    return written


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "from_status",
        choices=[*_RECOVERY_PLAN.keys(), "all"],
        help="Failure status to recover (or 'all' for every terminal-failure state)",
    )
    ap.add_argument("--school",  default=None, help="Limit to one school slug")
    ap.add_argument("--dry-run", action="store_true", help="Count only, do not write")
    args = ap.parse_args()

    targets = list(_RECOVERY_PLAN.keys()) if args.from_status == "all" else [args.from_status]

    db = SessionLocal()
    try:
        if args.dry_run:
            print("(dry-run — no changes will be written)\n")
        grand_total = 0
        for status in targets:
            grand_total += _recover_one(db, status, args.school, args.dry_run)
            print()
        if args.dry_run:
            print(f"Dry-run total: {grand_total} meetings would be recovered.")
        else:
            print(f"Recovered {grand_total} meetings total.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
