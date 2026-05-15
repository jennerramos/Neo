"""
Neo v2 — Phase 5: Data Packager

Reads every meeting with status='transcribed' or status='captioned',
runs it through the cleaner, and produces three structured data products:

    data/processed/<school>/<video_id>/
        meeting.txt      — human-readable transcript (speaker labels + timestamps)
        meeting.json     — metadata envelope
        chunks.jsonl     — pre-built RAG chunks (one JSON object per line)

Also inserts Chunk rows into the PostgreSQL chunks table so Phase 6 (Qdrant
indexing) can query them directly without re-reading files.

Improvements over the Phase 5 spec:
  • Quality gate: meetings below MIN_QUALITY_SCORE auto-marked 'rejected'
  • Chunk overlap: 1 sentence carried forward for context continuity in RAG
  • Per-segment confidence from WhisperX avg_logprob
  • SHA-256 file hash stored for reproducibility / re-run detection
  • Chunk rows inserted into DB chunks table (ready for Phase 6)

Usage:
    uv run python pipeline/data_packager.py
    uv run python pipeline/data_packager.py --school houston_city_college
    uv run python pipeline/data_packager.py --limit 5
    uv run python pipeline/data_packager.py --reprocess     # re-run completed ones
    uv run python pipeline/data_packager.py --no-db-chunks  # skip DB chunk insert
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
from database.models import Chunk, Meeting, PipelineRun, School
from pipeline.states import eligible_inputs
from pipeline.cleaner import (
    CLEANER_VERSION,
    CleanedSegment,
    clean_vtt,
    clean_whisperx,
    segments_to_plain_text,
    sha256_file,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROCESSING_VERSION = f"whisperx_v1_cleaner_v{CLEANER_VERSION}"
TARGET_TOKENS      = 400   # target tokens per chunk
MAX_TOKENS         = 500   # hard cap before forced split
OVERLAP_SENTENCES  = 1     # sentences of overlap between adjacent chunks


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

def _count_tokens(text: str) -> int:
    """
    Count tokens using tiktoken (cl100k_base — GPT-4/Claude compatible).
    Falls back to word-count * 1.3 heuristic if tiktoken is not installed.
    """
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except ImportError:
        # ~1.3 tokens per word is a reasonable approximation
        return int(len(text.split()) * 1.3)


# ---------------------------------------------------------------------------
# Timestamp formatting
# ---------------------------------------------------------------------------

def _fmt_ts(seconds: float) -> str:
    """Format seconds → HH:MM:SS or MM:SS."""
    total = int(round(seconds))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _fmt_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "unknown"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


# ---------------------------------------------------------------------------
# meeting.txt builder
# ---------------------------------------------------------------------------

def build_meeting_txt(
    segments:  List[CleanedSegment],
    meeting:   Meeting,
    school:    School,
    meta:      dict,
) -> str:
    """
    Human-readable transcript with speaker labels and timestamps.

    Format:
        ==========================================================
        Houston City College Board of Trustees Meeting
        October 16, 2024
        ==========================================================
        School     : Houston City College
        ...

        [00:01:23] SPEAKER_00: Good afternoon...
        [00:02:15] SPEAKER_01: Thank you, Chair...
    """
    title  = meeting.title or f"{school.name} Board Meeting"
    date   = str(meeting.published_date) if meeting.published_date else "Unknown date"
    border = "=" * 62

    header_lines = [
        border,
        title,
        date,
        border,
        f"School     : {school.name}",
        f"Date       : {date}",
        f"Duration   : {_fmt_duration(meeting.duration_seconds)}",
        f"Source     : {meeting.source_type or 'unknown'}",
        f"Words      : {meta['word_count']:,}",
        f"Speakers   : {meta['speaker_count'] or 'N/A (VTT - no speaker data)'}",
        f"Quality    : {meta['quality_score']:.2f}",
        f"Processed  : {meta['processed_at']}",
        f"Version    : {PROCESSING_VERSION}",
        border,
        "",
    ]

    body_lines = []
    prev_speaker = None

    for seg in segments:
        ts      = _fmt_ts(seg.start)
        speaker = seg.speaker

        if speaker:
            if speaker != prev_speaker:
                # New speaker block — add a blank line for readability
                if body_lines:
                    body_lines.append("")
                body_lines.append(f"[{ts}] {speaker}:")
                prev_speaker = speaker
            body_lines.append(f"  {seg.text}")
        else:
            # VTT — no speaker, just timestamp prefix
            body_lines.append(f"[{ts}] {seg.text}")

    return "\n".join(header_lines + body_lines) + "\n"


# ---------------------------------------------------------------------------
# meeting.json builder
# ---------------------------------------------------------------------------

def build_meeting_json(
    meeting:     Meeting,
    school:      School,
    meta:        dict,
    source_file: Optional[Path] = None,
) -> dict:
    """
    Metadata envelope stored alongside meeting.txt.
    Contains all fields needed to reconstruct context without the full transcript.
    """
    return {
        "video_id":           meeting.video_id,
        "video_url":          meeting.video_url,
        "school_slug":        school.slug,
        "school_name":        school.name,
        "school_state":       school.state,
        "title":              meeting.title,
        "published_date":     str(meeting.published_date) if meeting.published_date else None,
        "meeting_type":       meeting.meeting_type,
        "duration_seconds":   meeting.duration_seconds,
        "source_type":        meeting.source_type,
        "word_count":         meta["word_count"],
        "speaker_count":      meta["speaker_count"],
        "chunk_count":        meta["chunk_count"],
        "quality_score":      round(meta["quality_score"], 4),
        "processing_version": PROCESSING_VERSION,
        "processed_at":       meta["processed_at"],
        "file_hash":          meta.get("file_hash"),
    }


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------

def build_chunks(
    segments:    List[CleanedSegment],
    video_id:    str,
    school_slug: str,
    source_type: str,
) -> List[dict]:
    """
    Split CleanedSegments into overlapping RAG chunks.

    Strategy:
      - Target ~TARGET_TOKENS tokens per chunk
      - Never split within a speaker's turn if it's under MAX_TOKENS
      - Add OVERLAP_SENTENCES sentences from the previous chunk for context
      - Quality score = mean segment confidence, penalized for no speakers

    Returns list of dicts matching the chunks table + JSONL schema.
    """
    if not segments:
        return []

    chunks: List[dict] = []
    buf_segs:    List[CleanedSegment] = []
    buf_tokens:  int = 0
    overlap_tail: str = ""   # last N sentences of previous chunk

    def _flush(idx: int) -> Optional[dict]:
        nonlocal buf_segs, buf_tokens, overlap_tail

        if not buf_segs:
            return None

        text_parts = []
        if overlap_tail:
            text_parts.append(f"[…] {overlap_tail}")  # context carry-in

        text_parts.extend(s.text for s in buf_segs)
        full_text = " ".join(text_parts)

        # Dominant speaker = most-frequent speaker label in this chunk
        from collections import Counter
        speaker_counts = Counter(
            s.speaker for s in buf_segs if s.speaker
        )
        speaker = speaker_counts.most_common(1)[0][0] if speaker_counts else None

        # Quality = mean segment confidence
        confidences = [s.confidence for s in buf_segs]
        quality     = sum(confidences) / len(confidences) if confidences else 0.5

        # Penalize chunks with no speaker labels (less useful for attribution)
        if speaker is None and source_type == "whisperx_asr":
            quality *= 0.85

        # Carry-over last OVERLAP_SENTENCES sentences for next chunk
        all_text   = " ".join(s.text for s in buf_segs)
        sents      = [s.strip() for s in all_text.split(".") if s.strip()]
        overlap_tail = ". ".join(sents[-OVERLAP_SENTENCES:]) + "." if sents else ""

        chunk = {
            "chunk_id":     f"{video_id}_{idx:04d}",
            "chunk_index":  idx,
            "video_id":     video_id,
            "school_slug":  school_slug,
            "text":         full_text.strip(),
            "speaker":      speaker,
            "start_time":   round(buf_segs[0].start, 3),
            "end_time":     round(buf_segs[-1].end, 3),
            "token_count":  _count_tokens(full_text),
            "quality_score": round(quality, 4),
            "source_type":  source_type,
        }

        buf_segs   = []
        buf_tokens = 0
        return chunk

    chunk_idx = 1
    for seg in segments:
        seg_tokens = _count_tokens(seg.text)

        # Force-flush if adding this segment would exceed MAX_TOKENS
        if buf_segs and (buf_tokens + seg_tokens) > MAX_TOKENS:
            c = _flush(chunk_idx)
            if c:
                chunks.append(c)
                chunk_idx += 1

        buf_segs.append(seg)
        buf_tokens += seg_tokens

        # Flush at TARGET_TOKENS if we're at a natural sentence boundary
        if buf_tokens >= TARGET_TOKENS and re.search(r"[.!?]\s*$", seg.text):
            c = _flush(chunk_idx)
            if c:
                chunks.append(c)
                chunk_idx += 1

    # Final flush
    c = _flush(chunk_idx)
    if c:
        chunks.append(c)

    # Stamp total_chunks now that we know
    total = len(chunks)
    for ch in chunks:
        ch["total_chunks"] = total

    return chunks


# ---------------------------------------------------------------------------
# Per-meeting processor
# ---------------------------------------------------------------------------

def package_meeting(
    db_session,
    meeting:     Meeting,
    school:      School,
    do_db_chunks: bool = True,
    reprocess:   bool  = False,
) -> dict:
    """
    Full Phase 5 pipeline for one meeting.
    Returns outcome dict.
    """
    out_dir = config.PROCESSED_DIR / school.slug / meeting.video_id
    txt_path     = out_dir / "meeting.txt"
    json_path    = out_dir / "meeting.json"
    chunks_path  = out_dir / "chunks.jsonl"

    # Skip if already done (unless --reprocess)
    if txt_path.exists() and json_path.exists() and chunks_path.exists() and not reprocess:
        meeting.status             = "processed"
        meeting.clean_txt_path     = str(txt_path)
        meeting.meeting_json_path  = str(json_path)
        meeting.chunks_jsonl_path  = str(chunks_path)
        return {"status": "processed", "reason": "already_on_disk"}

    out_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc)

    # ── Step 1: Locate raw source file ────────────────────────────────────────
    raw_dir   = config.RAW_DIR / school.slug
    source_file: Optional[Path] = None

    if meeting.source_type in ("whisperx_asr", "transcribed") or meeting.status == "transcribed":
        candidate = raw_dir / f"{meeting.video_id}_whisperx.json"
        if candidate.exists():
            source_file = candidate
    if source_file is None:
        # Try VTT
        for ext in (".vtt",):
            candidate = raw_dir / f"{meeting.video_id}{ext}"
            if candidate.exists():
                source_file = candidate
                break

    if source_file is None:
        _log_run(db_session, meeting, "failed", "source_file_not_found", started_at, 0)
        meeting.status = "processing_failed"
        return {"status": "failed", "reason": "source_file_not_found"}

    # ── Step 2: Clean raw source ───────────────────────────────────────────────
    try:
        if source_file.suffix == ".json":
            segments = clean_whisperx(source_file)
            eff_source = meeting.source_type or "whisperx_asr"
        else:
            segments = clean_vtt(source_file)
            eff_source = meeting.source_type or "youtube_caption"
    except Exception as e:
        log.error("Cleaning failed for %s: %s", meeting.video_id, e)
        _log_run(db_session, meeting, "failed", f"cleaning_error: {e}", started_at, 0)
        meeting.status = "processing_failed"
        return {"status": "failed", "reason": f"cleaning_error: {e}"}

    if not segments:
        _log_run(db_session, meeting, "failed", "no_segments_after_cleaning", started_at, 0)
        meeting.status = "processing_failed"
        return {"status": "failed", "reason": "no_segments_after_cleaning"}

    # ── Step 3: Compute metadata ───────────────────────────────────────────────
    plain_text   = segments_to_plain_text(segments)
    word_count   = len(plain_text.split())

    speakers     = {s.speaker for s in segments if s.speaker}
    speaker_count = len(speakers) if speakers else None

    mean_confidence = sum(s.confidence for s in segments) / len(segments)
    # Apply word-count quality penalty
    if word_count < config.MIN_WORD_COUNT:
        word_penalty = word_count / config.MIN_WORD_COUNT  # 0–1
        quality_score = mean_confidence * (0.5 + 0.5 * word_penalty)
    else:
        quality_score = mean_confidence

    file_hash    = sha256_file(source_file)
    processed_at = datetime.now(timezone.utc).isoformat()

    # ── Step 4: Build chunks ───────────────────────────────────────────────────
    chunks = build_chunks(segments, meeting.video_id, school.slug, eff_source)

    meta = {
        "word_count":     word_count,
        "speaker_count":  speaker_count,
        "chunk_count":    len(chunks),
        "quality_score":  quality_score,
        "processed_at":   processed_at,
        "file_hash":      file_hash,
    }

    # ── Step 5: Write meeting.txt ──────────────────────────────────────────────
    meeting_txt = build_meeting_txt(segments, meeting, school, meta)
    txt_path.write_text(meeting_txt, encoding="utf-8")

    # ── Step 6: Write meeting.json ─────────────────────────────────────────────
    meeting_meta = build_meeting_json(meeting, school, meta, source_file)
    json_path.write_text(
        json.dumps(meeting_meta, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # ── Step 7: Write chunks.jsonl ─────────────────────────────────────────────
    with chunks_path.open("w", encoding="utf-8") as f:
        for ch in chunks:
            f.write(json.dumps(ch, ensure_ascii=False) + "\n")

    # ── Step 8: Insert Chunk rows into DB ──────────────────────────────────────
    if do_db_chunks:
        # Delete any existing chunks for this meeting (idempotent)
        db_session.query(Chunk).filter(Chunk.meeting_id == meeting.meeting_id).delete()
        for ch in chunks:
            db_session.add(Chunk(
                chunk_id=         ch["chunk_id"],
                meeting_id=       meeting.meeting_id,
                school_id=        school.school_id,
                chunk_index=      ch["chunk_index"],
                text=             ch["text"],
                speaker=          ch.get("speaker"),
                start_time=       ch["start_time"],
                end_time=         ch["end_time"],
                token_count=      ch["token_count"],
                quality_score=    ch["quality_score"],
                processing_version=PROCESSING_VERSION,
                qdrant_indexed=   False,
            ))

    # ── Step 9: Update meeting record ─────────────────────────────────────────
    meeting.status             = "processed"
    meeting.clean_txt_path     = str(txt_path)
    meeting.meeting_json_path  = str(json_path)
    meeting.chunks_jsonl_path  = str(chunks_path)
    meeting.word_count         = word_count
    meeting.speaker_count      = speaker_count
    meeting.quality_score      = quality_score
    meeting.processing_version = PROCESSING_VERSION
    meeting.file_hash          = file_hash
    meeting.updated_at         = datetime.now(timezone.utc)

    # ── Quality gate ───────────────────────────────────────────────────────────
    if quality_score < config.MIN_QUALITY_SCORE:
        meeting.status           = "rejected"
        meeting.rejection_reason = (
            f"quality_score {quality_score:.2f} < threshold {config.MIN_QUALITY_SCORE}"
        )
        _log_run(db_session, meeting, "success", None, started_at, 0,
                 (datetime.now(timezone.utc) - started_at).total_seconds())
        return {
            "status":        "rejected",
            "reason":        meeting.rejection_reason,
            "quality_score": quality_score,
            "word_count":    word_count,
            "chunk_count":   len(chunks),
        }

    duration = (datetime.now(timezone.utc) - started_at).total_seconds()
    _log_run(db_session, meeting, "success", None, started_at, 0, duration)

    return {
        "status":        "processed",
        "duration":      duration,
        "word_count":    word_count,
        "speaker_count": speaker_count,
        "chunk_count":   len(chunks),
        "quality_score": quality_score,
    }


def _log_run(db_session, meeting, status, error_msg, started_at, gpu_s, duration=None):
    finished_at = datetime.now(timezone.utc)
    db_session.add(PipelineRun(
        meeting_id=meeting.meeting_id,
        channel_id=None,
        phase="cleaning",
        status=status,
        error_message=error_msg,
        duration_seconds=duration or (finished_at - started_at).total_seconds(),
        gpu_time_seconds=gpu_s,
        started_at=started_at,
        finished_at=finished_at,
        retry_count=0,
    ))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Neo v2 Phase 5 — Data Packager")
    parser.add_argument("--school",       help="Process only this school slug")
    parser.add_argument("--limit",        type=int, help="Process at most N meetings")
    parser.add_argument("--reprocess",    action="store_true",
                        help="Re-run meetings already marked processed")
    parser.add_argument("--no-db-chunks", action="store_true",
                        help="Skip inserting chunk rows into the DB (write JSONL only)")
    args = parser.parse_args()

    do_db_chunks = not args.no_db_chunks

    print("Neo v2 — Phase 5: Transcript Cleaning & Data Packaging")
    print("=" * 56)
    print(f"Output dir      : {config.PROCESSED_DIR}")
    print(f"Target tokens   : {TARGET_TOKENS}")
    print(f"Min quality     : {config.MIN_QUALITY_SCORE}")
    print(f"DB chunk insert : {'yes' if do_db_chunks else 'no'}")
    print(f"Version         : {PROCESSING_VERSION}")

    engine  = create_engine(config.DATABASE_URL, echo=config.SQL_ECHO)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        # Eligibility from pipeline.states. --reprocess additionally picks up
        # already-processed/rejected meetings (e.g. after a chunker change).
        statuses = list(eligible_inputs("data_packager", rerun=args.reprocess))

        query = (
            session.query(Meeting, School)
            .join(School, School.school_id == Meeting.school_id)
            .filter(Meeting.status.in_(statuses))
            .order_by(School.school_id, Meeting.published_date.desc())
        )
        if args.school:
            query = query.filter(School.slug == args.school)
        if args.limit:
            query = query.limit(args.limit)

        meetings = query.all()

        if not meetings:
            print("\n[INFO] No meetings ready for processing.")
            print("       Run caption_downloader.py and/or asr_processor.py first.")
            sys.exit(0)

        total = len(meetings)
        print(f"\nMeetings to process : {total}\n")

        processed    = 0
        failed       = 0
        rejected     = 0
        already_done = 0
        total_chunks = 0

        for i, (meeting, school) in enumerate(meetings, 1):
            title_short = (meeting.title or "")[:52]
            print(f"[{i:>3}/{total}] {school.slug[:20]:<20} | {title_short}")

            result = package_meeting(session, meeting, school, do_db_chunks, args.reprocess)

            if result.get("reason") == "already_on_disk":
                already_done += 1
                print(f"         → already processed")
            elif result["status"] == "processed":
                processed    += 1
                total_chunks += result["chunk_count"]
                print(
                    f"         → ✅ {result['word_count']:,} words, "
                    f"{result['chunk_count']} chunks, "
                    f"q={result['quality_score']:.2f}, "
                    f"{result['duration']:.1f}s"
                )
            elif result["status"] == "rejected":
                rejected += 1
                print(f"         → ⚠️  rejected  ({result['reason']})")
            else:
                failed += 1
                print(f"         → ❌ failed   ({result['reason']})")

            if i % 10 == 0:
                session.commit()
                print(f"         [checkpoint at {i}]")

        session.commit()

    print("\n" + "=" * 56)
    print("Phase 5 complete:")
    print(f"  Processed   : {processed + already_done}")
    print(f"  Rejected    : {rejected}")
    print(f"  Failed      : {failed}")
    print(f"  Total       : {total}")
    print(f"  Total chunks: {total_chunks:,}")
    print(f"\nVerify:")
    print(f'  psql -U postgres -d neo_v2 -c "SELECT status, COUNT(*) FROM meetings GROUP BY status;"')
    print(f'  psql -U postgres -d neo_v2 -c "SELECT COUNT(*) FROM chunks;"')
    print(f"  Sample: data/processed/<school>/<video_id>/chunks.jsonl")


if __name__ == "__main__":
    main()
