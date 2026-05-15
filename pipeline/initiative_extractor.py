"""
Neo v2 — Phase 6.5: Initiative Extractor

Extracts structured initiative records from processed meetings into the
`initiatives` table. Runs AFTER Phase 6 extraction is validated.

Key design principle — four distinct evidence levels, never conflated:
  observed_action  : what the board actually did ("approved aviation program")
  stated_rationale : what they said motivated it ("addresses workforce gap")
  claimed_outcome  : what they predicted ("enrollment will increase 20%")
  measured_outcome : hard numbers FOUND IN TRANSCRIPT ("enrollment up 18%")

If only observed_action is in the text, the other three stay null.
Never infer or summarize across evidence levels.

Usage:
    uv run python pipeline/initiative_extractor.py
    uv run python pipeline/initiative_extractor.py --school houston_city_college --limit 5
    uv run python pipeline/initiative_extractor.py --reextract
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

import config
from database.models import Initiative, Meeting, PipelineRun, School
from pipeline.states import eligible_inputs
from pipeline.candidate_finder import (
    CandidateWindow, ExtractionTarget, find_all_candidates,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXTRACTOR_VERSION = "v2.5"
OLLAMA_TIMEOUT    = 150
AUTO_REVIEW_THRESHOLD = 0.60

_NULL_STRINGS = frozenset({
    "", "null", "none", "n/a", "na", "unknown", "not mentioned",
    "not specified", "not stated", "not provided", "not available",
    "not applicable", "unspecified", "unclear",
})

# Fixed vocabulary for initiative categories — critical for cross-college comparison
_INITIATIVE_CATEGORIES = frozenset({
    "workforce_development",
    "career_technical_education",
    "student_success",
    "equity_and_inclusion",
    "facilities",
    "technology",
    "curriculum",
    "accreditation",
    "financial_sustainability",
    "governance",
    "community_partnership",
    "dual_enrollment",
    "transfer_pathway",
    "other",
})

# Fixed vocabulary for action types
_ACTION_TYPES = frozenset({
    "launched", "proposed", "approved", "discussed", "continued", "cancelled", "other",
})


# ---------------------------------------------------------------------------
# Ollama client (shared pattern)
# ---------------------------------------------------------------------------

def _call_ollama(prompt: str) -> list[dict]:
    payload = {
        "model":  config.OLLAMA_MODEL,
        "stream": False,
        "format": "json",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a precise board meeting analyst specializing in "
                    "community college governance. "
                    "Return ONLY valid JSON matching the schema exactly. "
                    "No markdown fences, no extra keys. "
                    "Nullable fields must be JSON null, never the string 'null' or 'N/A'. "
                    "CRITICAL: Never conflate stated rationale with measured outcomes."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "options": {"temperature": 0.0, "num_predict": 2048},
    }
    try:
        resp = requests.post(
            f"{config.OLLAMA_HOST}/api/chat",
            json=payload,
            timeout=OLLAMA_TIMEOUT,
        )
        resp.raise_for_status()
        raw = resp.json()["message"]["content"].strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("items", "initiatives", "results", "data"):
                if key in parsed and isinstance(parsed[key], list):
                    return parsed[key]
            return [parsed]
        return []
    except requests.exceptions.ConnectionError:
        log.error("Ollama not running at %s", config.OLLAMA_HOST)
        return []
    except (requests.exceptions.Timeout, json.JSONDecodeError, KeyError, Exception) as e:
        log.warning("Ollama call failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in _NULL_STRINGS else s[:1000]


def _vocab(v: Any, allowed: frozenset, fallback: str) -> str:
    s = str(v or "").lower().strip()
    # Normalize common synonyms before vocabulary check
    s = _normalize_category(s)
    return s if s in allowed else fallback


def _normalize_category(raw: str) -> str:
    """Map common LLM variants to canonical vocabulary."""
    synonyms = {
        "workforce": "workforce_development",
        "workforce development": "workforce_development",
        "cte": "career_technical_education",
        "career technical": "career_technical_education",
        "career and technical": "career_technical_education",
        "student success": "student_success",
        "equity": "equity_and_inclusion",
        "dei": "equity_and_inclusion",
        "diversity": "equity_and_inclusion",
        "facility": "facilities",
        "construction": "facilities",
        "it": "technology",
        "information technology": "technology",
        "dual credit": "dual_enrollment",
        "dual enrollment": "dual_enrollment",
        "transfer": "transfer_pathway",
        "articulation": "transfer_pathway",
        "financial": "financial_sustainability",
        "budget": "financial_sustainability",
        "partnership": "community_partnership",
        "community": "community_partnership",
    }
    return synonyms.get(raw, raw)


def _confidence(item: dict, window_score: float) -> tuple[float, bool]:
    """Confidence for an initiative extraction."""
    present = sum([
        bool(_clean(item.get("initiative_name"))),
        bool(_clean(item.get("observed_action"))),
        bool(_clean(item.get("description"))),
    ])
    field_score  = present / 3.0
    pattern_norm = min(1.0, window_score / 8.0)

    # Penalty if claimed_outcome or measured_outcome look suspiciously similar
    # (suggests LLM conflated the levels — a hallucination risk)
    claimed  = (_clean(item.get("claimed_outcome"))  or "").lower()
    measured = (_clean(item.get("measured_outcome")) or "").lower()
    conflation_penalty = 0.0
    if claimed and measured and claimed == measured:
        conflation_penalty = 0.25   # same text in both fields is a red flag

    conf = round((field_score * 0.6 + pattern_norm * 0.4) - conflation_penalty, 3)
    conf = max(0.0, min(1.0, conf))
    return conf, conf < AUTO_REVIEW_THRESHOLD


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_INITIATIVE_PROMPT = """\
Extract all significant programmatic and strategic initiatives discussed or approved \
in this board meeting transcript excerpt.

FIXED VOCABULARY for category — choose exactly one:
  workforce_development | career_technical_education | student_success |
  equity_and_inclusion | facilities | technology | curriculum | accreditation |
  financial_sustainability | governance | community_partnership |
  dual_enrollment | transfer_pathway | other

FIXED VOCABULARY for action_type — choose exactly one:
  launched | proposed | approved | discussed | continued | cancelled | other

CRITICAL — four evidence levels, never conflate them:
  observed_action  = what the board or college ACTUALLY DID ("approved the aviation program")
  stated_rationale = what they SAID motivated it ("board said this addresses workforce shortage")
  claimed_outcome  = what they PREDICTED would happen ("projected 200 new students annually")
  measured_outcome = ONLY hard numbers/metrics FOUND IN THIS TEXT ("enrollment increased 18%")

Return a JSON ARRAY (may be []).  Each element EXACTLY:
{{
  "initiative_name":   "short descriptive label ≤60 chars",
  "category":          "one value from the category vocabulary",
  "description":       "1–3 sentence neutral description of what this initiative is",
  "action_type":       "one value from the action_type vocabulary",
  "observed_action":   "what the board actually did, verbatim or close paraphrase, or null",
  "stated_rationale":  "reason they gave, or null",
  "claimed_outcome":   "projected benefit they stated, or null",
  "measured_outcome":  "ONLY if hard numbers are explicitly in the text above, else null",
  "outcome_timeframe": "e.g. 'Fall 2026', 'within 2 years', or null"
}}

RULES:
- Include only substantive initiatives, not routine procedural items.
- Do NOT set measured_outcome unless actual numbers appear in the excerpt.
- Do NOT copy stated_rationale into claimed_outcome or vice versa.
- If only the action is mentioned with no rationale or outcome, that is fine.
- Nullable fields must be JSON null.
- Same initiative mentioned multiple times = include ONCE.

TRANSCRIPT:
{window}

Return ONLY a valid JSON array."""


# ---------------------------------------------------------------------------
# Core extractor
# ---------------------------------------------------------------------------

def extract_initiatives(
    meeting: Meeting,
    school: School,
    windows: list[CandidateWindow],
    db_session,
) -> int:
    """Extract initiatives from candidate windows and insert into DB."""
    db_session.execute(
        delete(Initiative).where(Initiative.meeting_id == meeting.meeting_id)
    )

    inserted = 0
    seen: set[str] = set()   # dedup by initiative_name.lower()

    for window in windows:
        items = _call_ollama(_INITIATIVE_PROMPT.format(window=window.text))

        for item in items:
            if not isinstance(item, dict):
                continue

            name = _clean(item.get("initiative_name"))
            if not name:
                continue

            dedup_key = name.lower()
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            conf, needs_review = _confidence(item, window.window_score)

            db_session.add(Initiative(
                meeting_id       = meeting.meeting_id,
                school_id        = school.school_id,
                school_slug      = school.slug,
                video_id         = meeting.video_id,
                initiative_name  = name,
                category         = _vocab(item.get("category"), _INITIATIVE_CATEGORIES, "other"),
                description      = _clean(item.get("description")),
                action_type      = _vocab(item.get("action_type"), _ACTION_TYPES, "other"),
                observed_action  = _clean(item.get("observed_action")),
                stated_rationale = _clean(item.get("stated_rationale")),
                claimed_outcome  = _clean(item.get("claimed_outcome")),
                measured_outcome = _clean(item.get("measured_outcome")),
                outcome_timeframe= _clean(item.get("outcome_timeframe")),
                chunk_ids        = window.chunk_ids,
                evidence_text    = window.evidence_snippet,
                source_type      = window.source_type,
                start_time_sec   = window.start_time_sec,
                end_time_sec     = window.end_time_sec,
                extractor_version= EXTRACTOR_VERSION,
                confidence       = conf,
                needs_review     = needs_review,
            ))
            inserted += 1

    return inserted


def extract_meeting_initiatives(
    db_session,
    meeting: Meeting,
    school: School,
) -> dict:
    started_at = datetime.now(timezone.utc)
    result = {"initiatives": 0, "status": "success"}

    jsonl_path = None
    if meeting.chunks_jsonl_path:
        jsonl_path = Path(meeting.chunks_jsonl_path)
    if not jsonl_path or not jsonl_path.exists():
        jsonl_path = (
            config.PROCESSED_DIR / school.slug / meeting.video_id / "chunks.jsonl"
        )

    if not jsonl_path.exists():
        result["status"] = "error: chunks.jsonl not found"
        return result

    try:
        all_windows = find_all_candidates(jsonl_path, [ExtractionTarget.INITIATIVE])
        windows = all_windows.get(ExtractionTarget.INITIATIVE, [])
        result["initiatives"] = extract_initiatives(meeting, school, windows, db_session)
    except Exception as e:
        result["status"] = f"error: {e}"
        log.error("Initiative extraction failed for %s: %s", meeting.video_id, e, exc_info=True)

    duration = (datetime.now(timezone.utc) - started_at).total_seconds()
    result["duration"] = duration

    db_session.add(PipelineRun(
        meeting_id       = meeting.meeting_id,
        channel_id       = None,
        phase            = "initiative_extraction",
        status           = "success" if result["status"] == "success" else "failed",
        error_message    = None if result["status"] == "success" else result["status"],
        duration_seconds = duration,
        gpu_time_seconds = 0,
        started_at       = started_at,
        finished_at      = datetime.now(timezone.utc),
        retry_count      = 0,
    ))
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Neo v2 Phase 6.5 — Initiative Extractor"
    )
    parser.add_argument("--school",    help="Process only this school slug")
    parser.add_argument("--limit",     type=int, help="Process at most N meetings")
    parser.add_argument("--reextract", action="store_true",
                        help="Re-extract meetings that already have initiatives")
    args = parser.parse_args()

    print("Neo v2 — Phase 6.5: Initiative Extraction")
    print("=" * 55)
    print(f"Model   : {config.OLLAMA_MODEL}")
    print(f"Version : {EXTRACTOR_VERSION}")
    print()

    # Verify Ollama
    try:
        resp = requests.get(f"{config.OLLAMA_HOST}/api/tags", timeout=5)
        models = [m["name"] for m in resp.json().get("models", [])]
        if not any(config.OLLAMA_MODEL in m for m in models):
            print(f"❌  Model {config.OLLAMA_MODEL} not found.")
            sys.exit(1)
    except Exception:
        print(f"❌  Ollama not reachable at {config.OLLAMA_HOST}")
        sys.exit(1)

    print(f"✅  Ollama ready — {config.OLLAMA_MODEL}\n")

    engine  = create_engine(config.DATABASE_URL, echo=config.SQL_ECHO)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        # Only run on meetings that have been through Phase 6 extraction.
        # 'extracted' is the pre-indexer state, 'indexed' the post — both imply
        # votes/financials/personnel have been extracted. Eligibility lives in
        # pipeline.states.
        query = (
            session.query(Meeting, School)
            .join(School, School.school_id == Meeting.school_id)
            .filter(Meeting.status.in_(eligible_inputs("initiative_extractor")))
            .order_by(School.school_id, Meeting.published_date.desc())
        )
        if not args.reextract:
            # Skip meetings that already have initiative rows
            from sqlalchemy import exists
            query = query.filter(
                ~exists().where(Initiative.meeting_id == Meeting.meeting_id)
            )
        if args.school:
            query = query.filter(School.slug == args.school)
        if args.limit:
            query = query.limit(args.limit)

        meetings = query.all()

        if not meetings:
            print("[INFO] No meetings ready for initiative extraction.")
            print("       Run extractor.py first (status must be 'extracted' or 'indexed').")
            sys.exit(0)

        total = len(meetings)
        print(f"Meetings : {total}\n")

        total_initiatives = errors = 0

        for i, (meeting, school) in enumerate(meetings, 1):
            title_short = (meeting.title or meeting.video_id)[:50]
            print(f"[{i:>3}/{total}] {school.slug[:20]:<20} | {title_short}")

            result = extract_meeting_initiatives(session, meeting, school)

            if result["status"] != "success":
                errors += 1
                print(f"          ❌ {result['status']}")
            else:
                total_initiatives += result["initiatives"]
                print(f"          ✅  {result['initiatives']} initiatives  ({result['duration']:.0f}s)")

            if i % 5 == 0:
                session.commit()
                print(f"          [checkpoint {i}/{total}]")

        session.commit()

    print("\n" + "=" * 55)
    print("Phase 6.5 complete:")
    print(f"  Meetings     : {total}")
    print(f"  Initiatives  : {total_initiatives}")
    print(f"  Errors       : {errors}")
    print()
    print("Validate:")
    print('  psql -U postgres -d neo_v2 -c "SELECT category, COUNT(*), ROUND(AVG(confidence)::numeric,2) AS avg_conf, SUM(needs_review::int) AS flagged FROM initiatives GROUP BY category ORDER BY 2 DESC;"')
    print('  psql -U postgres -d neo_v2 -c "SELECT school_slug, COUNT(*) FROM initiatives GROUP BY school_slug ORDER BY 2 DESC;"')
    print('  psql -U postgres -d neo_v2 -c "SELECT COUNT(*) FILTER (WHERE measured_outcome IS NOT NULL) AS has_measured, COUNT(*) AS total FROM initiatives;"')


if __name__ == "__main__":
    main()
