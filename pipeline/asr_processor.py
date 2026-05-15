"""
Phase 4 — WhisperX ASR Processor

For every meeting with status='needs_asr':
    1. Download best-quality audio via yt-dlp (no video — saves time/disk)
    2. Transcribe with WhisperX large-v3 on CUDA (word-level timestamps)
    3. Align word timestamps with WhisperX aligner
    4. Diarize with pyannote.audio (SPEAKER_00, SPEAKER_01, ...)
    5. Assign speaker labels to every word segment
    6. Save JSON → data/raw/<school_slug>/<video_id>_whisperx.json
    7. Update meeting: status='transcribed', source_type='whisperx_asr'
    8. Delete temp audio file (saves disk space)
    9. Log to pipeline_runs with duration + GPU time

Input:  meetings.status = 'needs_asr'
Output: meetings.status = 'transcribed'
        data/raw/<school>/<video_id>_whisperx.json

Usage:
    uv run python pipeline/asr_processor.py
    uv run python pipeline/asr_processor.py --school central_texas_college
    uv run python pipeline/asr_processor.py --limit 5
    uv run python pipeline/asr_processor.py --no-diarize   # skip speaker labels
"""
import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
from database.models import Meeting, School, PipelineRun

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WHISPER_BATCH_SIZE = 16   # reduce to 8 if CUDA OOM on very long videos
WHISPER_CHUNK_SIZE = 30   # seconds per chunk for streaming decode

# VAD sensitivity — lower = picks up quieter / more distant speech.
# Board meetings often have long silent intros or gavel pauses.
# silero defaults: onset=0.5, offset=0.363 → too aggressive for AV rooms.
VAD_ONSET  = 0.1   # speech-start threshold  (was 0.5 → lowered)
VAD_OFFSET = 0.05  # speech-end threshold    (was 0.363 → lowered)


# ---------------------------------------------------------------------------
# Audio download
# ---------------------------------------------------------------------------

def download_audio(video_id: str, audio_dir: Path) -> Path | None:
    """
    Download best-quality audio for a YouTube video using yt-dlp.
    Returns path to downloaded WAV file, or None on failure.
    Downloads audio-only (no video) — much faster for long board meetings.
    """
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        log.error("yt-dlp not installed")
        return None

    audio_dir.mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"

    ydl_opts = {
        "format":         "bestaudio/best",
        "outtmpl":        str(audio_dir / f"{video_id}.%(ext)s"),
        "quiet":          False,
        "no_warnings":    False,
        "retries":        5,
        "socket_timeout": 60,
        "ignoreerrors":   False,
        # android_vr client doesn't require a JS runtime (Node/Deno).
        # yt-dlp CLI auto-falls back to this; Python API needs it explicit.
        "extractor_args": {
            "youtube": {
                "player_client": ["android_vr", "android"],
            }
        },
        "postprocessors": [{
            "key":              "FFmpegExtractAudio",
            "preferredcodec":   "wav",
            "preferredquality": "0",
        }],
    }

    # NOTE: Do NOT pass cookiesfrombrowser here.
    # android_vr / android clients don't support cookies and are skipped
    # if cookies are attached, leaving yt-dlp with no valid client.
    # HCC board meetings are public videos — no cookies required.

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        log.error("Audio download failed for %s: %s", video_id, e)
        return None

    wav_path = audio_dir / f"{video_id}.wav"
    if wav_path.exists():
        return wav_path

    # Fallback: find any audio file downloaded for this video_id
    for ext in ("m4a", "webm", "opus", "mp3"):
        p = audio_dir / f"{video_id}.{ext}"
        if p.exists():
            return p

    return None


# ---------------------------------------------------------------------------
# WhisperX transcription
# ---------------------------------------------------------------------------

def transcribe_audio(
    audio_path: Path,
    device: str = "cuda",
    compute_type: str = "float16",
) -> dict | None:
    """
    Run WhisperX large-v3 on the audio file.
    Returns the aligned WhisperX result dict, or None on failure.

    VAD is set very sensitive (onset=0.1) so board meetings with quiet
    speakers or long silent intros are not silently skipped.
    """
    try:
        import whisperx
    except ImportError:
        log.error("whisperx not installed — run: uv pip install whisperx")
        return None

    try:
        model = whisperx.load_model(
            config.WHISPER_MODEL,
            device=device,
            compute_type=compute_type,
            language="en",        # board meetings are always English
            vad_method="silero",  # silero-vad; works offline without HF token
            vad_options={
                "vad_onset":  VAD_ONSET,
                "vad_offset": VAD_OFFSET,
            },
        )

        audio = whisperx.load_audio(str(audio_path))  # resamples to 16 kHz

        result = model.transcribe(
            audio,
            batch_size=WHISPER_BATCH_SIZE,
            chunk_size=WHISPER_CHUNK_SIZE,
        )

        segments = result.get("segments", [])

        # Free GPU memory BEFORE alignment (saves ~1 GB)
        del model
        import gc
        gc.collect()
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass

        # If VAD found absolutely nothing, bail early rather than aligning empty list
        if not segments:
            log.warning(
                "WhisperX VAD found no speech in %s — "
                "video may be silent, private, or have no spoken content",
                audio_path.name,
            )
            return None

        # Align word-level timestamps
        align_model, metadata = whisperx.load_align_model(
            language_code=result["language"],
            device=device,
        )
        result = whisperx.align(
            segments,
            align_model,
            metadata,
            audio,
            device,
            return_char_alignments=False,
        )

        del align_model
        gc.collect()
        try:
            import torch
            torch.cuda.empty_cache()
        except Exception:
            pass

        return result

    except Exception as e:
        log.error("WhisperX transcription failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Speaker diarization
# ---------------------------------------------------------------------------

def diarize(audio_path: Path, result: dict, device: str = "cuda") -> dict:
    """
    Run pyannote.audio speaker diarization and assign speaker labels.

    Uses whisperx.diarize.DiarizationPipeline (wraps pyannote) +
    whisperx.diarize.assign_word_speakers to tag each word segment.

    Returns updated result dict; on any failure returns original unchanged
    (transcription is still saved — just without speaker labels).

    Requires HUGGINGFACE_TOKEN in .env + accepted model terms at:
      https://huggingface.co/pyannote/speaker-diarization-community-1
    """
    if not config.HUGGINGFACE_TOKEN:
        log.warning("HUGGINGFACE_TOKEN not set — skipping diarization")
        return result

    # Import from the sub-module where it actually lives
    try:
        from whisperx.diarize import DiarizationPipeline, assign_word_speakers
        import torch
    except ImportError as e:
        log.warning("whisperx.diarize unavailable — skipping: %s", e)
        return result

    try:
        # Try the community model first (no gated access required after accepting once),
        # fall back to 3.1 if it's already unlocked on the account.
        for model_name in (
            "pyannote/speaker-diarization-community-1",
            "pyannote/speaker-diarization-3.1",
        ):
            try:
                # whisperx renamed use_auth_token → token in recent builds;
                # try both so the code works across versions
                try:
                    diarize_model = DiarizationPipeline(
                        model_name=model_name,
                        token=config.HUGGINGFACE_TOKEN,
                        device=device,
                    )
                except TypeError:
                    diarize_model = DiarizationPipeline(
                        model_name=model_name,
                        use_auth_token=config.HUGGINGFACE_TOKEN,
                        device=device,
                    )
                break
            except Exception as load_err:
                log.warning("Could not load %s: %s", model_name, load_err)
                diarize_model = None

        if diarize_model is None:
            log.warning("No pyannote diarization model could be loaded — skipping")
            return result

        diarize_segments = diarize_model(str(audio_path))
        result = assign_word_speakers(diarize_segments, result)

        # Free GPU memory
        del diarize_model
        import gc
        gc.collect()
        torch.cuda.empty_cache()

        return result

    except Exception as e:
        log.warning("Diarization failed (transcription still saved): %s", e)
        return result


# ---------------------------------------------------------------------------
# Per-meeting processing
# ---------------------------------------------------------------------------

def process_meeting(
    db_session,
    meeting: Meeting,
    school: School,
    do_diarize: bool = True,
) -> dict:
    """
    Full ASR pipeline for one meeting.
    Returns status dict with outcome details.
    """
    raw_dir   = config.RAW_DIR   / school.slug
    audio_dir = config.AUDIO_DIR / school.slug
    raw_dir.mkdir(parents=True,   exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    json_path = raw_dir / f"{meeting.video_id}_whisperx.json"

    # Already transcribed?
    if json_path.exists():
        meeting.meeting_json_path = str(json_path)
        meeting.status            = "transcribed"
        meeting.source_type       = "whisperx_asr"
        return {"status": "transcribed", "reason": "already_on_disk"}

    started_at = datetime.now(timezone.utc)
    gpu_start  = time.perf_counter()

    # Step 1 — Download audio
    print(f"           → downloading audio...")
    audio_path = download_audio(meeting.video_id, audio_dir)
    if not audio_path:
        meeting.status = "asr_failed"
        _log_run(db_session, meeting, "failed", "audio_download_failed",
                 started_at, 0)
        return {"status": "asr_failed", "reason": "audio_download_failed"}

    # Step 2 — Transcribe + align
    print(f"           → transcribing with WhisperX {config.WHISPER_MODEL}...")
    result = transcribe_audio(audio_path, config.DEVICE, config.COMPUTE_TYPE)
    if not result:
        audio_path.unlink(missing_ok=True)
        meeting.status = "asr_failed"
        _log_run(db_session, meeting, "failed", "transcription_failed",
                 started_at, 0)
        return {"status": "asr_failed", "reason": "transcription_failed"}

    # Step 3 — Diarization (optional)
    if do_diarize and config.HUGGINGFACE_TOKEN:
        print(f"           → diarizing speakers...")
        result = diarize(audio_path, result, config.DEVICE)

    gpu_seconds = time.perf_counter() - gpu_start

    # Step 4 — Count words and speakers
    word_count = sum(len(seg.get("words", [])) for seg in result.get("segments", []))
    speakers   = set()
    for seg in result.get("segments", []):
        if "speaker" in seg:
            speakers.add(seg["speaker"])
        for w in seg.get("words", []):
            if "speaker" in w:
                speakers.add(w["speaker"])
    speaker_count = len(speakers) if speakers else None

    # Guard: whisperx returned segments but no actual words
    if word_count == 0:
        audio_path.unlink(missing_ok=True)
        meeting.status = "asr_failed"
        _log_run(db_session, meeting, "failed", "empty_transcript",
                 started_at, gpu_seconds)
        return {"status": "asr_failed", "reason": "empty_transcript"}

    # Step 5 — Save JSON
    output = {
        "video_id":      meeting.video_id,
        "school_slug":   school.slug,
        "model":         config.WHISPER_MODEL,
        "diarized":      bool(speakers),
        "speaker_count": speaker_count,
        "word_count":    word_count,
        "created_at":    datetime.now(timezone.utc).isoformat(),
        "segments":      result.get("segments", []),
        "word_segments": result.get("word_segments", []),
    }
    json_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Step 6 — Update meeting record
    meeting.meeting_json_path = str(json_path)
    meeting.status            = "transcribed"
    meeting.source_type       = "whisperx_asr"
    meeting.word_count        = word_count
    meeting.speaker_count     = speaker_count
    meeting.updated_at        = datetime.now(timezone.utc)

    # Step 7 — Delete temp audio (saves disk space)
    audio_path.unlink(missing_ok=True)

    finished_at = datetime.now(timezone.utc)
    duration    = (finished_at - started_at).total_seconds()

    _log_run(db_session, meeting, "success", None, started_at,
             gpu_seconds, duration)

    return {
        "status":        "transcribed",
        "reason":        "whisperx_asr",
        "duration":      duration,
        "gpu_seconds":   gpu_seconds,
        "word_count":    word_count,
        "speaker_count": speaker_count,
        "diarized":      bool(speakers),
    }


def _log_run(db_session, meeting, status, error_msg,
             started_at, gpu_seconds, duration=None):
    finished_at = datetime.now(timezone.utc)
    run = PipelineRun(
        meeting_id=meeting.meeting_id,
        channel_id=None,
        phase="asr",
        status=status,
        error_message=error_msg,
        duration_seconds=duration or (finished_at - started_at).total_seconds(),
        gpu_time_seconds=gpu_seconds,
        started_at=started_at,
        finished_at=finished_at,
        retry_count=0,
    )
    db_session.add(run)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Neo v2 Phase 4 — WhisperX ASR")
    parser.add_argument("--school",     help="Process only this school slug")
    parser.add_argument("--limit",      type=int, help="Process at most N meetings")
    parser.add_argument("--no-diarize", action="store_true",
                        help="Skip speaker diarization (faster, no speaker labels)")
    args = parser.parse_args()

    do_diarize = not args.no_diarize

    print("Neo v2 — Phase 4: WhisperX ASR")
    print("=" * 50)
    print(f"Model         : {config.WHISPER_MODEL}")
    print(f"Device        : {config.DEVICE}  ({config.COMPUTE_TYPE})")
    print(f"VAD onset/off : {VAD_ONSET} / {VAD_OFFSET}  (sensitive — board meeting optimised)")
    print(f"Diarization   : {'enabled' if do_diarize and config.HUGGINGFACE_TOKEN else 'disabled (set HUGGINGFACE_TOKEN in .env)'}")
    print(f"Audio dir     : {config.AUDIO_DIR}")
    print(f"Output dir    : {config.RAW_DIR}")

    # Verify CUDA is available
    try:
        import torch
        if config.DEVICE == "cuda" and not torch.cuda.is_available():
            print("\n⚠️  CUDA not available — falling back to CPU (will be slow)")
            config.DEVICE       = "cpu"
            config.COMPUTE_TYPE = "int8"
        else:
            gpu_name = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "N/A"
            print(f"GPU           : {gpu_name}")
    except ImportError:
        print("⚠️  torch not installed — run: uv sync --extra asr")
        sys.exit(1)

    engine  = create_engine(config.DATABASE_URL, echo=config.SQL_ECHO)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        query = (
            session.query(Meeting, School)
            .join(School, School.school_id == Meeting.school_id)
            .filter(Meeting.status == "needs_asr")
            .order_by(School.school_id, Meeting.published_date.desc())
        )
        if args.school:
            query = query.filter(School.slug == args.school)
        if args.limit:
            query = query.limit(args.limit)

        meetings = query.all()

        if not meetings:
            print("\n[INFO] No meetings with status='needs_asr' found.")
            print("       All done, or run caption_downloader.py first.")
            sys.exit(0)

        total = len(meetings)
        print(f"\nMeetings to transcribe : {total}")
        print(f"Estimated time         : ~{total * 8}–{total * 15} min on RTX 5080\n")

        transcribed  = 0
        failed       = 0
        already_done = 0
        total_gpu    = 0.0

        for i, (meeting, school) in enumerate(meetings, 1):
            title_short = (meeting.title or "")[:55]
            print(f"\n[{i:>3}/{total}] {school.slug[:22]:<22} | {title_short}")

            result = process_meeting(session, meeting, school, do_diarize)

            if result.get("reason") == "already_on_disk":
                already_done += 1
                print(f"           → already on disk")
            elif result["status"] == "transcribed":
                transcribed += 1
                total_gpu   += result.get("gpu_seconds", 0)
                diarized_tag = "✅ speakers" if result.get("diarized") else "⚠️  no speakers"
                print(
                    f"           → ✅ transcribed  "
                    f"({result['word_count']:,} words, "
                    f"{result.get('speaker_count') or '?'} spkr, "
                    f"{result['duration']:.0f}s wall / {result.get('gpu_seconds', 0):.0f}s GPU, "
                    f"{diarized_tag})"
                )
            else:
                failed += 1
                print(f"           → ❌ failed  ({result['reason']})")

            # Commit every 5 meetings
            if i % 5 == 0:
                session.commit()
                print(f"           [checkpoint at {i}]")

        session.commit()

    # Summary
    print("\n" + "=" * 50)
    print("Phase 4 ASR complete:")
    print(f"  Transcribed : {transcribed + already_done}")
    print(f"  Failed      : {failed}")
    print(f"  Total       : {total}")
    print(f"  Total GPU   : {total_gpu / 60:.1f} min")
    print(f"\nVerify:")
    print(f'  psql -U postgres -d neo_v2 -c "SELECT status, COUNT(*) FROM meetings GROUP BY status;"')
    print(f"  Open a JSON: data/raw/<school>/<video_id>_whisperx.json")


if __name__ == "__main__":
    main()
