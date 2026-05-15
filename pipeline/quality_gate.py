"""
Neo v2 — Phase 7: Quality Gate

Validates every meeting that has been processed (and optionally extracted)
before it is allowed into Qdrant.  Meetings that pass all checks are promoted
to status='approved'.  Failures are logged with a rejection_reason and the
meeting is set to status='rejected'.

Checks (all configurable via thresholds at the top):
  1. chunks.jsonl exists on disk
  2. Minimum word count (default ≥ 500)
  3. Minimum chunk count (default ≥ 10)
  4. Average chunk quality score (default ≥ 0.60)
  5. Required metadata present (published_date, school_id, source_type)
  6. Duration sanity (default ≥ 120 s — filters out 2-minute "shorts")
  7. Not already indexed in Qdrant

An overall meeting-level quality_score is also computed and stored so
downstream phases and the UI can surface low-confidence meetings.

Status flow:
  processed → (quality gate) → approved  → (Phase 8) → indexed
                             → rejected   (terminal — review required)

Usage:
    uv run python pipeline/quality_gate.py
    uv run python pipeline/quality_gate.py --school houston_city_college
    uv run python pipeline/quality_gate.py --recheck   # re-evaluate rejected meetings
    uv run python pipeline/quality_gate.py --dry-run   # print decisions without writing
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
from database.models import Meeting, PipelineRun, School
from pipeline.states import eligible_inputs

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configurable thresholds
# ---------------------------------------------------------------------------

MIN_WORD_COUNT    = 500      # Transcript must have at least this many words
MIN_CHUNK_COUNT   = 5        # Need at least 5 chunks to build a useful index
MIN_CHUNK_QUALITY = 0.50     # Average chunk quality score floor
MIN_DURATION_SEC  = 120      # Must be a real meeting, not a 2-minute clip
REQUIRED_FIELDS   = ("published_date", "school_id", "source_type")

# ---------------------------------------------------------------------------
# Per-check result helpers
# ---------------------------------------------------------------------------

def _check_file(meeting: Meeting, school: School) -> Optional[str]:
    """Check that chunks.jsonl exists on disk."""
    jsonl_path = None
    if meeting.chunks_jsonl_path:
        jsonl_path = Path(meeting.chunks_jsonl_path)
    if not jsonl_path or not jsonl_path.exists():
        jsonl_path = (
            config.PROCESSED_DIR / school.slug / meeting.video_id / "chunks.jsonl"
        )
    if not jsonl_path.exists():
        return f"chunks.jsonl not found at expected path"
    return None


def _load_chunks(meeting: Meeting, school: School) -> tuple[list[dict], Optional[str]]:
    """
    Load chunks from chunks.jsonl.
    Returns (chunks, error_reason_or_None).
    """
    jsonl_path = None
    if meeting.chunks_jsonl_path:
        jsonl_path = Path(meeting.chunks_jsonl_path)
    if not jsonl_path or not jsonl_path.exists():
        jsonl_path = (
            config.PROCESSED_DIR / school.slug / meeting.video_id / "chunks.jsonl"
        )

    chunks = []
    try:
        with open(jsonl_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    chunks.append(json.loads(line))
    except (OSError, json.JSONDecodeError) as e:
        return [], f"failed to read chunks.jsonl: {e}"

    return chunks, None


def _check_word_count(meeting: Meeting, chunks: list[dict]) -> Optional[str]:
    """Reject transcripts that are too short to be useful."""
    # Prefer the stored word_count; fall back to summing chunk words
    wc = meeting.word_count
    if not wc and chunks:
        wc = sum(len(c.get("text", "").split()) for c in chunks)
    if not wc or wc < MIN_WORD_COUNT:
        return f"word_count={wc or 0} below minimum {MIN_WORD_COUNT}"
    return None


def _check_chunk_count(chunks: list[dict]) -> Optional[str]:
    """Reject meetings with too few chunks — indexing would be near-useless."""
    if len(chunks) < MIN_CHUNK_COUNT:
        return f"only {len(chunks)} chunks — minimum is {MIN_CHUNK_COUNT}"
    return None


def _check_chunk_quality(chunks: list[dict]) -> tuple[Optional[str], float]:
    """
    Compute average chunk quality score and reject if below threshold.
    Returns (error_or_None, avg_quality_score).
    """
    scores = [c["quality_score"] for c in chunks if "quality_score" in c]
    if not scores:
        # No quality scores stored — soft pass, score = 0.5
        return None, 0.5
    avg = round(statistics.mean(scores), 4)
    if avg < MIN_CHUNK_QUALITY:
        return f"avg chunk quality {avg:.2f} below minimum {MIN_CHUNK_QUALITY}", avg
    return None, avg


def _check_metadata(meeting: Meeting) -> Optional[str]:
    """Reject meetings missing critical metadata fields."""
    missing = []
    for field in REQUIRED_FIELDS:
        val = getattr(meeting, field, None)
        if val is None:
            missing.append(field)
    if missing:
        return f"missing required metadata: {', '.join(missing)}"
    return None


def _check_duration(meeting: Meeting, chunks: list[dict]) -> Optional[str]:
    """Reject clips that are too short to be a real board meeting."""
    dur = meeting.duration_seconds
    if not dur and chunks:
        # Estimate from last chunk's end_time
        end_times = [c.get("end_time") for c in chunks if c.get("end_time")]
        if end_times:
            dur = max(end_times)
    if dur and dur < MIN_DURATION_SEC:
        return f"duration={dur:.0f}s below minimum {MIN_DURATION_SEC}s"
    return None


def _check_not_indexed(meeting: Meeting) -> Optional[str]:
    """Reject if already indexed in Qdrant — prevents duplicate upserts."""
    if meeting.status == "indexed":
        return "already indexed in Qdrant"
    return None


def _compute_quality_score(
    wc: int, chunk_count: int, avg_chunk_quality: float
) -> float:
    """
    Compute an overall meeting quality score in [0, 1].

    Weights:
      0.40  chunk quality (content richness)
      0.35  word count adequacy (capped at 3000 = 1.0)
      0.25  chunk count adequacy (capped at 50 = 1.0)
    """
    wc_score    = min(1.0, (wc or 0) / 3000)
    chunk_score = min(1.0, chunk_count / 50)
    total = (
        avg_chunk_quality * 0.40
        + wc_score         * 0.35
        + chunk_score      * 0.25
    )
    return round(total, 4)


# ---------------------------------------------------------------------------
# Per-meeting gate
# ---------------------------------------------------------------------------

def gate_meeting(
    meeting: Meeting,
    school: School,
    dry_run: bool = False,
) -> dict:
    """
    Run all quality checks on a single meeting.
    Returns a result dict with keys: approved, reason, quality_score.
    """
    # ── Check 1: file exists ─────────────────────────────────────────────
    err = _check_file(meeting, school)
    if err:
        return {"approved": False, "reason": err, "quality_score": 0.0}

    # ── Load chunks ──────────────────────────────────────────────────────
    chunks, load_err = _load_chunks(meeting, school)
    if load_err:
        return {"approved": False, "reason": load_err, "quality_score": 0.0}

    # ── Check 2: word count ──────────────────────────────────────────────
    err = _check_word_count(meeting, chunks)
    if err:
        return {"approved": False, "reason": err, "quality_score": 0.0}

    # ── Check 3: chunk count ─────────────────────────────────────────────
    err = _check_chunk_count(chunks)
    if err:
        return {"approved": False, "reason": err, "quality_score": 0.0}

    # ── Check 4: chunk quality ───────────────────────────────────────────
    err, avg_quality = _check_chunk_quality(chunks)
    if err:
        return {"approved": False, "reason": err, "quality_score": avg_quality}

    # ── Check 5: required metadata ───────────────────────────────────────
    err = _check_metadata(meeting)
    if err:
        return {"approved": False, "reason": err, "quality_score": avg_quality}

    # ── Check 6: duration sanity ─────────────────────────────────────────
    err = _check_duration(meeting, chunks)
    if err:
        return {"approved": False, "reason": err, "quality_score": avg_quality}

    # ── Check 7: not already indexed ─────────────────────────────────────
    err = _check_not_indexed(meeting)
    if err:
        return {"approved": False, "reason": err, "quality_score": avg_quality}

    # ── All checks passed ────────────────────────────────────────────────
    wc = meeting.word_count or sum(len(c.get("text","").split()) for c in chunks)
    qs = _compute_quality_score(wc, len(chunks), avg_quality)
    return {"approved": True, "reason": None, "quality_score": qs}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Neo v2 Phase 7 — Quality Gate"
    )
    parser.add_argument("--school",   help="Process only this school slug")
    parser.add_argument("--limit",    type=int, help="Process at most N meetings")
    parser.add_argument("--recheck",  action="store_true",
                        help="Re-evaluate meetings that were previously rejected")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Print decisions without writing to database")
    args = parser.parse_args()

    print("Neo v2 — Phase 7: Quality Gate")
    print("=" * 55)
    print(f"Min word count  : {MIN_WORD_COUNT}")
    print(f"Min chunk count : {MIN_CHUNK_COUNT}")
    print(f"Min chunk quality: {MIN_CHUNK_QUALITY}")
    print(f"Min duration    : {MIN_DURATION_SEC}s")
    if args.dry_run:
        print("\n[DRY RUN — no database writes]\n")

    engine  = create_engine(config.DATABASE_URL, echo=config.SQL_ECHO)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        # Eligibility from pipeline.states — quality_gate is order-independent
        # vs. the extractor (accepts both 'processed' and already-'extracted').
        # --recheck additionally re-evaluates previously rejected meetings.
        eligible_statuses = list(eligible_inputs("quality_gate", rerun=args.recheck))

        query = (
            session.query(Meeting, School)
            .join(School, School.school_id == Meeting.school_id)
            .filter(Meeting.status.in_(eligible_statuses))
            .order_by(School.school_id, Meeting.published_date.desc())
        )
        if args.school:
            query = query.filter(School.slug == args.school)
        if args.limit:
            query = query.limit(args.limit)

        meetings = query.all()

        if not meetings:
            print("[INFO] No meetings eligible for quality gate.")
            print("       Run processor.py first (status must be 'processed' or 'extracted').")
            sys.exit(0)

        total    = len(meetings)
        approved = rejected = errors = 0

        print(f"Meetings : {total}\n")

        for i, (meeting, school) in enumerate(meetings, 1):
            title_short = (meeting.title or meeting.video_id)[:50]
            started_at  = datetime.now(timezone.utc)

            result = gate_meeting(meeting, school, dry_run=args.dry_run)

            if result["approved"]:
                verdict = "✅  APPROVED"
                approved += 1
                if not args.dry_run:
                    meeting.status        = "approved"
                    meeting.quality_score = result["quality_score"]
                    meeting.rejection_reason = None
            else:
                verdict = f"❌  REJECTED  {result['reason']}"
                rejected += 1
                if not args.dry_run:
                    meeting.status           = "rejected"
                    meeting.quality_score    = result["quality_score"]
                    meeting.rejection_reason = result["reason"]

            print(f"[{i:>3}/{total}] {school.slug[:20]:<20} | {title_short}")
            print(f"          {verdict}  (quality={result['quality_score']:.2f})")

            if not args.dry_run:
                duration = (datetime.now(timezone.utc) - started_at).total_seconds()
                session.add(PipelineRun(
                    meeting_id       = meeting.meeting_id,
                    channel_id       = None,
                    phase            = "quality_gate",
                    status           = "success" if result["approved"] else "rejected",
                    error_message    = result["reason"],
                    duration_seconds = duration,
                    gpu_time_seconds = 0,
                    started_at       = started_at,
                    finished_at      = datetime.now(timezone.utc),
                    retry_count      = 0,
                ))

            if i % 20 == 0 and not args.dry_run:
                session.commit()
                print(f"          [checkpoint {i}/{total}]")

        if not args.dry_run:
            session.commit()

    print("\n" + "=" * 55)
    print("Phase 7 complete:")
    print(f"  Total     : {total}")
    print(f"  Approved  : {approved}")
    print(f"  Rejected  : {rejected}")
    print(f"  Errors    : {errors}")
    print()

    if rejected:
        print("Rejected breakdown:")
        print(
            "  psql -U postgres -d neo_v2 -c \""
            "SELECT rejection_reason, COUNT(*) FROM meetings "
            "WHERE status='rejected' GROUP BY 1 ORDER BY 2 DESC;\""
        )
        print()

    print("Ready for Phase 8 (Qdrant indexing):")
    print(
        "  psql -U postgres -d neo_v2 -c \""
        "SELECT school_slug, COUNT(*), ROUND(AVG(quality_score)::numeric,2) AS avg_q "
        "FROM meetings WHERE status='approved' GROUP BY 1 ORDER BY 2 DESC;\""
    )


if __name__ == "__main__":
    main()
