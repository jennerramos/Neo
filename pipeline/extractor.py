"""
Neo v2 — Phase 6: Structured Extractor v2.5

Two-step pipeline per meeting:
  1. candidate_finder.py  scores chunks with regex/keyword patterns (zero LLM)
  2. extractor.py         sends each candidate window to the configured LLM
                          (PIPELINE_LLM_*, default local Ollama) for structured JSON

Every inserted row includes full provenance:
  chunk_ids, evidence_text, source_type, start/end time,
  extractor_version, confidence, needs_review

Tables populated: votes, financial_items, personnel_actions

Acceptance rubric — three tiers per row:
  KEEP            : insert with needs_review = False
  KEEP BUT REVIEW : insert with needs_review = True
  DISCARD         : do not insert; log reason to data/extraction_discards/{video_id}.jsonl

Confidence floors (hard discard below these):
  votes      : 0.35
  financial  : 0.35
  personnel  : 0.40

Usage:
    uv run python pipeline/extractor.py
    uv run python pipeline/extractor.py --school houston_city_college --limit 1
    uv run python pipeline/extractor.py --skip-votes --skip-financial
    uv run python pipeline/extractor.py --reextract
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

import config
from database.models import (
    FinancialItem, Meeting, PersonnelAction, PipelineRun, School, Vote,
)
from pipeline.states import eligible_inputs
from pipeline.candidate_finder import (
    CandidateWindow, ExtractionTarget, find_all_candidates,
)
from pipeline.llm_json import call_json, check_llm, describe, reset_usage, usage

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXTRACTOR_VERSION     = "v2.7"
AUTO_REVIEW_THRESHOLD = 0.65  # confidence below this → needs_review = True

# Raised confidence floors — discard anything below these per rubric
_CONF_FLOOR_VOTES      = 0.35
_CONF_FLOOR_FINANCIAL  = 0.35
_CONF_FLOOR_PERSONNEL  = 0.40

# Null-like strings the LLM returns instead of null
_NULL_STRINGS = frozenset({
    "", "null", "none", "n/a", "na", "unknown", "not mentioned",
    "not specified", "not stated", "not provided", "not available",
    "not applicable", "unspecified", "unclear",
})

# Fixed vocabularies — anything outside these gets mapped to fallback
_VOTE_CATEGORIES = frozenset({
    "personnel", "financial", "policy", "facilities", "curriculum",
    "governance", "student_services", "technology", "equity",
    "accreditation", "legislative", "other",
})
_FINANCIAL_CATEGORIES = frozenset({
    "contract", "grant", "budget_amendment", "salary_schedule",
    "bond", "capital_project", "technology", "student_services", "other",
})
_FINANCIAL_ACTIONS = frozenset({
    "approved", "proposed", "discussed", "tabled", "amended",
})
_PERSONNEL_ACTIONS = frozenset({
    "hire", "appoint", "promote", "terminate", "resign", "retire", "interim", "other",
})

# ---------------------------------------------------------------------------
# Governance / intro language filters
# ---------------------------------------------------------------------------

# Board trustees, committee chairs, legislative officers are governance — not staff.
_GOVERNANCE_ROLE_RE = re.compile(
    r"""
    \b(
        trustee | board\s+member | board\s+chair(?:man|woman|person)? |
        committee\s+chair(?:man|woman|person)? |
        board\s+president | board\s+officer |
        board\s+secretary | board\s+treasurer |
        higher\s+education\s+committee |
        higher\s+education\b |          # standalone — e.g. "resign | Higher Education"
        coordinating\s+board |          # Texas Higher Education Coordinating Board
        advisory\s+board\s+member |
        state\s+(senator|representative|legislator|official) |
        governor'?s?\s+appoint |
        regent
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Intro language in evidence → speaker presentation, not a personnel action.
# "please welcome", "joining us today", "here to present", "will now provide" etc.
_INTRO_EVIDENCE_RE = re.compile(
    r"""
    \b(
        please\s+welcome |
        joining\s+us\s+(today|tonight|this\s+evening) |
        here\s+to\s+(answer|present|provide|share|discuss) |
        will\s+(now\s+)?(present|provide(\s+an)?\s+overview|answer(\s+your)?\s+questions) |
        I\s+(would\s+like\s+to\s+|want\s+to\s+)?introduce |
        has\s+been\s+(serving|working|with\s+us)\s+(for|since) |
        working\s+(with|alongside)\s+(our|the)\b |
        led\s+by\s+(our|the)\b |
        supported\s+by\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Outside-money patterns: financial amounts mentioned in passing that belong
# to state/federal allocations, not board-approved decisions.
_OUTSIDE_MONEY_RE = re.compile(
    r"""
    \b(
        state\s+(gave|allocated|provided|awarded|appropriated|funded)\b |
        federal\s+(gave|provided|awarded|appropriated|allocated)\b |
        received\s+from\s+the\s+(state|federal|government)\b |
        allocated\s+(to\s+us|to\s+the\s+college)\s+by\b |
        state\s+allocation\b |
        pass-through\s+fund |
        formula\s+fund
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _is_governance_role(position: Optional[str]) -> bool:
    if not position:
        return False
    return bool(_GOVERNANCE_ROLE_RE.search(position))


def _has_intro_language(evidence: str) -> bool:
    """Return True if evidence sounds like a speaker introduction, not a personnel action."""
    return bool(_INTRO_EVIDENCE_RE.search(evidence))


def _is_outside_money(evidence: str) -> bool:
    """Return True if the financial evidence refers to external allocations, not board decisions."""
    return bool(_OUTSIDE_MONEY_RE.search(evidence))


# ---------------------------------------------------------------------------
# Discard logger
# ---------------------------------------------------------------------------

def _log_discard(video_id: str, table: str, item: dict, reason: str) -> None:
    """
    Append one discard record to data/extraction_discards/{video_id}.jsonl.
    Never raises — logging failure must not abort extraction.
    """
    try:
        discard_dir = Path(__file__).resolve().parents[1] / "data" / "extraction_discards"
        discard_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "ts":       datetime.now(timezone.utc).isoformat(),
            "video_id": video_id,
            "table":    table,
            "reason":   reason,
            "item":     {k: v for k, v in item.items() if v is not None},
        }
        with open(discard_dir / f"{video_id}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as exc:
        log.debug("Discard log failed: %s", exc)


# ---------------------------------------------------------------------------
# LLM client — runs on the "extract" profile (PIPELINE_LLM_*, default Ollama)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a precise board meeting analyst specializing in "
    "community college governance. "
    "Return ONLY valid JSON matching the schema exactly. "
    "No explanation, no markdown fences, no extra keys. "
    "Nullable fields must be JSON null, never the string 'null' or 'N/A'."
)

# Keys the model may wrap its array in, tried in order.
_UNWRAP_KEYS = (
    "items", "votes", "financial_items", "personnel_actions",
    "results", "data", "extractions",
)


def _call_llm(prompt: str, label: str = "") -> list[dict]:
    """Structured-JSON call. Returns a list of dicts, or [] on failure."""
    return call_json(
        system=_SYSTEM_PROMPT,
        prompt=prompt,
        unwrap_keys=_UNWRAP_KEYS,
        label=label,
    )


# ---------------------------------------------------------------------------
# Post-processing helpers
# ---------------------------------------------------------------------------

def _clean(v: Any) -> Optional[str]:
    """Return None if v is null-like, else stripped string ≤500 chars."""
    if v is None:
        return None
    s = str(v).strip()
    if s.lower() in _NULL_STRINGS:
        return None
    return s[:500]


def _int_or_none(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _float_amount(v: Any) -> Optional[float]:
    """Parse dollar amounts including shorthand: 1.5M, 850K, 2B."""
    if v is None:
        return None
    s = str(v).replace(",", "").replace("$", "").strip()
    multiplier = 1
    if s.upper().endswith("B"):
        multiplier = 1_000_000_000; s = s[:-1]
    elif s.upper().endswith("M"):
        multiplier = 1_000_000;     s = s[:-1]
    elif s.upper().endswith("K"):
        multiplier = 1_000;         s = s[:-1]
    try:
        val = float(s) * multiplier
        return val if val > 0 else None
    except (ValueError, TypeError):
        return None


def _vocab(v: Any, allowed: frozenset, fallback: str) -> str:
    s = str(v or "").lower().strip()
    return s if s in allowed else fallback


def _name_in_evidence(name: Optional[str], evidence: str) -> bool:
    """Check whether a person name actually appears in the evidence text."""
    if not name:
        return True   # null name is always fine
    return name.lower() in evidence.lower()


# Known department/qualifier words that are NOT person names.
# Includes military ranks — prevents "Sergeant Major" from splitting into
# position="Sergeant", person_name="Major".
_POSITION_QUALIFIERS = frozenset({
    "finance", "financial", "instruction", "instructional", "student", "affairs",
    "services", "development", "relations", "operations", "technology", "resources",
    "communication", "marketing", "research", "planning", "equity", "diversity",
    "inclusion", "academic", "workforce", "continuing", "education", "enrollment",
    "information", "human", "facilities", "legal", "general", "external",
    "institutional", "advancement", "chancellor", "president", "dean", "director",
    "officer", "vice", "assistant", "associate", "senior", "executive",
    "interim", "acting", "northwest", "northeast", "southwest", "southeast",
    "north", "south", "east", "west", "central", "online", "i", "ii", "iii",
    # Military ranks — never a person's surname
    "major", "colonel", "lieutenant", "sergeant", "captain", "general",
    "commander", "admiral", "corporal", "private", "specialist", "warrant",
    "brigadier", "marshal", "commodore",
})

# Single-word "names" that are obviously not person names — departments,
# organizations, abstract nouns.  If person_name matches one of these,
# null it out and flag for review.
_NON_NAME_WORDS = frozenset({
    "counseling", "union", "committee", "board", "division", "department",
    "faculty", "staff", "administration", "services", "education",
    "instruction", "enrollment", "affairs", "operations", "resources",
    "technology", "finance", "marketing", "communications", "research",
    "planning", "development", "workforce", "continuing", "center",
    "program", "office", "institute", "college", "university", "district",
    "foundation", "council", "commission", "association", "organization",
    "coalition", "alliance", "network", "society", "academy",
})

# Truncated positions end with a dangling preposition/article.
# e.g. "Dean of", "Director of Student", "Adjunct Professor of"
_TRAILING_PREPOSITION_RE = re.compile(
    r"\b(of|the|and|in|for|with|at|to|by|on|from|a|an)\s*$",
    re.IGNORECASE,
)

# Bare title: job title stem with no qualifying department/division.
_BARE_TITLE_RE = re.compile(
    r"^(vice\s+chancellor|vice\s+president|dean|chancellor|president|director|"
    r"associate\s+vice|assistant\s+vice|executive\s+vice)$",
    re.IGNORECASE,
)


def _split_name_from_position(position: str) -> tuple[str, Optional[str]]:
    """
    Detect and fix the common LLM error of putting a person's surname inside
    the position field, e.g. 'Vice Chancellor Alcorta'.

    Returns (cleaned_position, extracted_name_or_None).
    """
    words = position.strip().split()
    if len(words) < 2:
        return position, None

    last = words[-1]
    if (
        last[0].isupper()
        and last.lower() not in _POSITION_QUALIFIERS
        and len(last) > 2
        and not last.endswith(".")
    ):
        cleaned = " ".join(words[:-1])
        return cleaned, last

    return position, None


def _is_bare_title(position: str) -> bool:
    return bool(_BARE_TITLE_RE.match(position.strip()))


def _is_truncated_position(position: str) -> bool:
    """Return True if the position string appears to be cut off mid-phrase."""
    return bool(_TRAILING_PREPOSITION_RE.search(position.strip()))


def _clean_position(position: str) -> str:
    """
    Strip parenthetical content from a position string.
    e.g. 'Dr. Yber Lieutenant Colonel Yra (leading and expanding the roles)'
      → 'Dr. Yber Lieutenant Colonel Yra'
    Also strips trailing punctuation and excess whitespace.
    """
    s = re.sub(r"\s*\([^)]*\)", "", position).strip()
    return s.rstrip(".,;: ")


def _is_non_name(name: str) -> bool:
    """
    Return True if name is a single word that is clearly not a person's name
    (department, organization, abstract noun).
    """
    words = name.strip().split()
    if len(words) != 1:
        return False
    return words[0].lower() in _NON_NAME_WORDS


# ---------------------------------------------------------------------------
# Confidence scoring  (rubric-aligned v2.5)
# ---------------------------------------------------------------------------

def _vote_confidence(
    item: dict, evidence: str, window_score: float
) -> tuple[float, bool]:
    """
    Compute confidence for a vote extraction.

    Bonuses:
      +0.10  motion_text has ≥8 words (substantive, not a fragment)
      +0.10  yes_count or no_count present (hard evidence)

    Penalties:
      -0.20  each invented person name (not in evidence)
      -0.15  motion_text present but fewer than 4 words (fragment)

    Auto-review regardless of score:
      - motion_text is null (can't verify what was voted on)
      - motion_text is ≤3 words — bare procedural phrase like "So moved."
    """
    motion_text = _clean(item.get("motion_text"))
    result_text = _clean(item.get("vote_result_text"))
    passed_set  = item.get("passed") is not None

    if not motion_text and not result_text:
        return 0.0, True

    present     = sum([bool(motion_text), bool(result_text), passed_set])
    field_score = present / 3.0

    pattern_norm = min(1.0, window_score / 12.0)

    # Bonuses
    motion_words = len((motion_text or "").split())
    length_bonus = 0.10 if motion_words >= 8 else 0.0
    fragment_penalty = 0.15 if (motion_text and motion_words < 4) else 0.0

    has_counts  = (
        item.get("yes_count") is not None or item.get("no_count") is not None
    )
    count_bonus = 0.10 if has_counts else 0.0

    # Name hallucination penalty
    penalty = 0.0
    for f in ("moved_by", "seconded_by"):
        name = _clean(item.get(f))
        if name and not _name_in_evidence(name, evidence):
            penalty += 0.20

    conf = (
        field_score * 0.60
        + pattern_norm * 0.40
        + length_bonus
        + count_bonus
        - fragment_penalty
        - penalty
    )
    conf = max(0.0, min(1.0, round(conf, 3)))

    # Always needs_review if we don't know what was voted on
    if not motion_text:
        return conf, True

    # Bare procedural phrase — "So moved.", "All in favor.", "I move to accept." etc.
    # Either ≤3 words, or a known content-free phrase.
    _bare = motion_text.lower().strip().rstrip(".")
    if motion_words <= 3 or _bare in {
        "i move to accept", "i so move", "so seconded",
        "motion carries", "motion passes", "all in favor",
        "i move to proceed", "i move to adjourn",
    }:
        return conf, True

    return conf, conf < AUTO_REVIEW_THRESHOLD


def _financial_confidence(
    item: dict, window_score: float
) -> tuple[float, bool]:
    """
    Compute confidence for a financial item.

    Field weights:
      amount      0.40  (primary — a dollar value anchors the record)
      description 0.35  (context — what the money is for)
      action_type 0.15  (how the board acted on it)
      vendor      0.10  (bonus — named vendor = more verifiable)

    Action-type caps (post-scoring):
      discussed   → cap at 0.50  (always needs_review)
      proposed    → cap at 0.68
      tabled      → cap at 0.68

    Category penalty: category='other' → ×0.85

    Pattern saturation denominator: 14 (prevents cheap saturation)
    """
    amount_ok   = _float_amount(item.get("amount")) is not None
    desc_ok     = bool(_clean(item.get("description")))
    action_ok   = bool(_clean(item.get("action_type")))
    vendor_ok   = bool(_clean(item.get("vendor")))

    field_score = (
        (0.40 if amount_ok  else 0.0) +
        (0.35 if desc_ok    else 0.0) +
        (0.15 if action_ok  else 0.0) +
        (0.10 if vendor_ok  else 0.0)
    )

    pattern_norm = min(1.0, window_score / 14.0)
    conf = round(field_score * 0.65 + pattern_norm * 0.35, 3)

    category = _vocab(item.get("category"), _FINANCIAL_CATEGORIES, "other")
    if category == "other":
        conf = round(conf * 0.85, 3)

    action = _vocab(item.get("action_type"), _FINANCIAL_ACTIONS, "discussed")
    if action == "discussed":
        conf = min(conf, 0.50)
    elif action in ("proposed", "tabled"):
        conf = min(conf, 0.68)

    return conf, conf < AUTO_REVIEW_THRESHOLD


def _personnel_confidence(
    item: dict, evidence: str, window_score: float
) -> tuple[float, bool]:
    """
    Compute confidence for a personnel action.

    Base fields:
      action_type  0.50
      position     0.50

    Bonuses (post-base):
      +0.20  person_name present AND found in evidence
      +0.15  effective_date present

    Penalties:
      -0.30  person_name present but NOT in evidence (hallucination risk)
      -0.15  position is a bare title (no department qualifier)

    Pattern saturation denominator: 8
    """
    action_ok   = bool(_clean(item.get("action_type")))
    position_ok = bool(_clean(item.get("position")))

    present     = sum([action_ok, position_ok])
    field_score = present / 2.0

    pattern_norm = min(1.0, window_score / 8.0)

    # Name bonus/penalty
    name          = _clean(item.get("person_name"))
    name_in_ev    = _name_in_evidence(name, evidence) if name else True
    name_bonus    = 0.20 if (name and name_in_ev)    else 0.0
    name_penalty  = 0.30 if (name and not name_in_ev) else 0.0

    # Date bonus
    date_bonus = 0.15 if bool(_clean(item.get("effective_date"))) else 0.0

    # Bare title penalty
    position_raw = _clean(item.get("position")) or ""
    bare_penalty = 0.15 if _is_bare_title(position_raw) else 0.0

    conf = (
        field_score * 0.60
        + pattern_norm * 0.40
        + name_bonus
        + date_bonus
        - name_penalty
        - bare_penalty
    )
    conf = max(0.0, min(1.0, round(conf, 3)))

    return conf, conf < AUTO_REVIEW_THRESHOLD


# ---------------------------------------------------------------------------
# Prompt templates  (v2.5 — full exclusion blocks)
# ---------------------------------------------------------------------------

_VOTE_PROMPT = """\
Extract all formal board votes from this transcript excerpt.

FIXED VOCABULARY for topic_category — choose exactly one:
  personnel | financial | policy | facilities | curriculum |
  governance | student_services | technology | equity | accreditation | legislative | other

Return a JSON ARRAY (may be []).  Each element EXACTLY:
{{
  "agenda_item":       "short what-was-voted-on label (≤80 chars) or null",
  "motion_text":       "verbatim or paraphrased motion text or null",
  "topic_category":    "one value from the fixed vocabulary above",
  "moved_by":          "trustee name ONLY if their name appears verbatim in the text above, else null",
  "seconded_by":       "trustee name ONLY if their name appears verbatim in the text above, else null",
  "vote_result_text":  "verbatim result phrase e.g. 'the motion carries unanimously' or null",
  "yes_count":         integer or null,
  "no_count":          integer or null,
  "abstain_count":     integer or null,
  "absent_count":      integer or null,
  "passed":            true | false | null,
  "unanimous":         true | false | null
}}

INCLUDE:
- Formal recorded votes with "so moved", "I move", "motion to approve", "the board votes", etc.
- Consent agenda votes that approve multiple items at once.
- Roll-call votes where individual trustee names are recorded.

EXCLUDE (return [] for these):
- Discussions about a future vote ("we will vote on this next month").
- Procedural commentary about how voting works.
- Audience or public comment about an upcoming vote.
- Editorial mentions of a past vote not being recorded in this excerpt.

RULES:
- Include ONLY actual recorded votes, not discussions about future votes.
- Do NOT invent counts, names, or outcomes not found in the text above.
- motion_text must be at least a partial sentence — not a 1-2 word fragment.
- moved_by / seconded_by: null unless the full trustee name is verbatim in the excerpt.
- Nullable fields MUST be JSON null (not the string "null" or "N/A").

TRANSCRIPT:
{window}

Return ONLY a valid JSON array."""


_FINANCIAL_PROMPT = """\
Extract all financial items where this board approved, proposed, or acted on a specific dollar amount.

FIXED VOCABULARY for category:
  contract | grant | budget_amendment | salary_schedule | bond |
  capital_project | technology | student_services | other

FIXED VOCABULARY for action_type:
  approved | proposed | discussed | tabled | amended

Return a JSON ARRAY (may be []).  Each element EXACTLY:
{{
  "amount":       numeric dollar value as a plain number (e.g. 1500000.00) or null,
  "category":     "one value from the category vocabulary",
  "description":  "1–2 sentence description of what this money is for",
  "vendor":       "company or entity receiving funds, or null",
  "fund_source":  "general fund | bond | grant | state allocation | other, or null",
  "fiscal_year":  "e.g. FY2024-25, or null",
  "action_type":  "one value from the action_type vocabulary"
}}

INCLUDE:
- Contracts awarded to vendors with stated dollar values.
- Budget amendments approved by the board.
- Grants this college is applying for or accepting.
- Bond or capital project authorizations.
- Salary schedule approvals.

EXCLUDE (return [] for these):
- Dollar amounts only mentioned in comparison ("last year it was $X").
- State or federal allocations that were simply reported, not voted on.
- Amounts that belong to a third party (e.g., a neighboring district's budget).
- Dollar figures in background slides or informational updates with no board action.

RULES:
- Include only amounts tied to THIS board's actual decisions in this meeting.
- Exclude amounts merely mentioned in passing or referencing third parties.
- Same item appearing multiple times = include ONCE.
- amount must be a number, NOT a string. Use null if genuinely unknown.
- Default action_type to "discussed" when unclear; NEVER use "approved" unless explicit.

TRANSCRIPT:
{window}

Return ONLY a valid JSON array."""


_PERSONNEL_PROMPT = """\
Extract formal staff and administrator personnel actions from this transcript excerpt.

TARGET roles (include these):
  College presidents, chancellors, vice chancellors, vice presidents,
  deans, directors, department heads, faculty appointments,
  classified or professional staff hires, terminations, retirements.

EXCLUDED roles (do NOT include — these are governance or visitor presentations):
  Board of Trustees members or trustees by name.
  Board chairs, officers, secretaries, treasurers.
  Committee chairs or co-chairs.
  State legislators, senators, representatives, governor appointees.
  Student government officers or student board representatives.
  Advisory board members.
  Guest speakers being introduced to present at this meeting.
  Staff members mentioned only because they answered a board question.
  Consultants or vendors presenting to the board (not being hired by the board).

FIXED VOCABULARY for action_type:
  hire | appoint | promote | terminate | resign | retire | interim | other

Return a JSON ARRAY (may be []).  Each element EXACTLY:
{{
  "person_name":   "full name ONLY if stated explicitly in the text, else null",
  "action_type":   "one value from the fixed vocabulary",
  "position":      "exact job title e.g. 'Vice Chancellor of Finance and Administration' or null",
  "department":    "department or division, or null",
  "effective_date":"text date e.g. 'July 1, 2025' or 'immediately', or null",
  "is_interim":    true | false
}}

RULES:
- Include ONLY formal board-approved personnel actions for college staff and administrators.
- person_name: null unless the full name is explicitly stated in the excerpt. Never infer.
- position: include the FULL title including department (e.g. 'Vice Chancellor of Finance',
  NOT just 'Vice Chancellor').
- Do NOT put the person's surname inside the position field.
- If only a position title is mentioned with no associated action, return [].
- is_interim: true only if the word "interim" directly modifies this specific position.
- action_type should be "other" only as a last resort.
- Return [] if no qualifying personnel actions are present.

TRANSCRIPT:
{window}

Return ONLY a valid JSON array."""


# ---------------------------------------------------------------------------
# Vote extractor
# ---------------------------------------------------------------------------

# Garbled ASR patterns that indicate the motion_text is transcript noise,
# not an actual motion.  Any match → hard discard.
_GARBLED_MOTION_RE = re.compile(
    r"\b(say\s+aye\b|all\s+in\s+favor\s+say|looks\s+like\s+(made|moved)\b|"
    r"aye\s+approved\b|made\s+in\s+second\b)",
    re.IGNORECASE,
)

# Procedural "motion to consider" language — the board brought an item to the
# floor but didn't necessarily approve anything.  Flag for review, don't discard.
_CONSIDER_MOTION_RE = re.compile(
    r"\b(motion\s+to\s+consider|move\s+to\s+consider|moved?\s+and\s+second.*consider)\b",
    re.IGNORECASE,
)


def extract_votes(
    meeting: Meeting,
    school: School,
    windows: list[CandidateWindow],
    db_session,
) -> int:
    db_session.execute(delete(Vote).where(Vote.meeting_id == meeting.meeting_id))

    inserted  = 0
    discarded = 0
    seen_ts:      list[Optional[float]] = []
    seen_motions: set[str]              = set()   # content-based dedup

    for window in windows:
        items = _call_llm(_VOTE_PROMPT.format(window=window.text), "votes")

        for item in items:
            if not isinstance(item, dict):
                continue

            evidence    = window.evidence_snippet
            motion_text = _clean(item.get("motion_text"))
            result_text = _clean(item.get("vote_result_text"))

            # ── Hard discard 1: empty extraction ──────────────────────────
            if not motion_text and not result_text:
                _log_discard(meeting.video_id, "votes", item,
                             "both motion_text and vote_result_text are null")
                discarded += 1
                continue

            # ── Hard discard 2: garbled ASR text ──────────────────────────
            # Catches noise like "move looks like made in second all in favor say aye"
            if motion_text and _GARBLED_MOTION_RE.search(motion_text):
                _log_discard(meeting.video_id, "votes", item,
                             f"garbled ASR detected in motion_text: '{motion_text[:80]}'")
                discarded += 1
                continue

            conf, needs_review = _vote_confidence(item, evidence, window.window_score)

            # ── Hard discard 3: confidence floor ──────────────────────────
            if conf < _CONF_FLOOR_VOTES:
                _log_discard(meeting.video_id, "votes", item,
                             f"confidence {conf:.3f} below floor {_CONF_FLOOR_VOTES}")
                discarded += 1
                continue

            # ── Flag: procedural "motion to consider" ─────────────────────
            # Bringing an item to the floor ≠ approving it.  Keep but flag.
            if motion_text and _CONSIDER_MOTION_RE.search(motion_text):
                needs_review = True

            # ── Dedup 1: same timestamp ±10s = same vote ──────────────────
            ts = window.start_time_sec
            if ts is not None and any(
                abs(ts - s) < 10 for s in seen_ts if s is not None
            ):
                continue
            seen_ts.append(ts)

            # ── Dedup 2: same motion text (normalized) within this meeting ─
            # Prevents triple-inserting the same motion captured from overlapping
            # transcript windows.
            if motion_text:
                _norm = re.sub(r"\s+", " ", motion_text.lower().strip())
                if _norm in seen_motions:
                    continue
                seen_motions.add(_norm)

            # Null-normalize hallucinated person names
            moved_by    = _clean(item.get("moved_by"))
            seconded_by = _clean(item.get("seconded_by"))
            if moved_by and not _name_in_evidence(moved_by, evidence):
                moved_by    = None
                needs_review = True
            if seconded_by and not _name_in_evidence(seconded_by, evidence):
                seconded_by = None
                needs_review = True

            db_session.add(Vote(
                meeting_id       = meeting.meeting_id,
                school_id        = school.school_id,
                school_slug      = school.slug,
                video_id         = meeting.video_id,
                agenda_item      = _clean(item.get("agenda_item")),
                motion_text      = motion_text,
                topic_category   = _vocab(item.get("topic_category"), _VOTE_CATEGORIES, "other"),
                moved_by         = moved_by,
                seconded_by      = seconded_by,
                vote_result_text = result_text,
                yes_count        = _int_or_none(item.get("yes_count")),
                no_count         = _int_or_none(item.get("no_count")),
                abstain_count    = _int_or_none(item.get("abstain_count")),
                absent_count     = _int_or_none(item.get("absent_count")),
                passed           = item.get("passed")    if isinstance(item.get("passed"),    bool) else None,
                unanimous        = item.get("unanimous") if isinstance(item.get("unanimous"), bool) else None,
                chunk_ids        = window.chunk_ids,
                evidence_text    = evidence,
                source_type      = window.source_type,
                start_time_sec   = window.start_time_sec,
                end_time_sec     = window.end_time_sec,
                extractor_version= EXTRACTOR_VERSION,
                confidence       = conf,
                needs_review     = needs_review,
            ))
            inserted += 1

    if discarded:
        log.debug("votes: %d discarded for meeting %s", discarded, meeting.video_id)

    return inserted


# ---------------------------------------------------------------------------
# Financial extractor
# ---------------------------------------------------------------------------

def extract_financial(
    meeting: Meeting,
    school: School,
    windows: list[CandidateWindow],
    db_session,
) -> int:
    db_session.execute(
        delete(FinancialItem).where(FinancialItem.meeting_id == meeting.meeting_id)
    )

    inserted  = 0
    discarded = 0
    seen: set[tuple] = set()

    for window in windows:
        evidence = window.evidence_snippet

        # ── Evidence-level discard: outside-money mention ──────────────────
        # If the window's evidence reads like a state/federal allocation report,
        # don't even send it to the LLM.  Candidate finder should have caught
        # this, but this is a safety net.
        if _is_outside_money(evidence):
            _log_discard(meeting.video_id, "financial_items", {"evidence": evidence[:200]},
                         "outside-money pattern in evidence: not a board decision")
            discarded += 1
            continue

        items = _call_llm(_FINANCIAL_PROMPT.format(window=window.text), "financial")

        for item in items:
            if not isinstance(item, dict):
                continue

            amount = _float_amount(item.get("amount"))
            if amount is None:
                _log_discard(meeting.video_id, "financial_items", item,
                             "amount is null or unparseable")
                discarded += 1
                continue

            description = _clean(item.get("description"))
            if not description:
                _log_discard(meeting.video_id, "financial_items", item,
                             "description is null — amount without context is untrustworthy")
                discarded += 1
                continue

            category = _vocab(item.get("category"), _FINANCIAL_CATEGORIES, "other")
            action   = _vocab(item.get("action_type"), _FINANCIAL_ACTIONS, "discussed")
            vendor   = _clean(item.get("vendor"))

            # ── Hard discard: discussed + no vendor ──────────────────────
            # "We discussed $X" with no named vendor is too vague to be useful.
            if action == "discussed" and not vendor:
                _log_discard(meeting.video_id, "financial_items", item,
                             "action=discussed and vendor is null — insufficient anchor")
                discarded += 1
                continue

            dedup_key = (round(amount, 2), category)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            conf, needs_review = _financial_confidence(item, window.window_score)

            # ── Hard discard: confidence floor ────────────────────────────
            if conf < _CONF_FLOOR_FINANCIAL:
                _log_discard(meeting.video_id, "financial_items", item,
                             f"confidence {conf:.3f} below floor {_CONF_FLOOR_FINANCIAL}")
                discarded += 1
                continue

            db_session.add(FinancialItem(
                meeting_id       = meeting.meeting_id,
                school_id        = school.school_id,
                school_slug      = school.slug,
                video_id         = meeting.video_id,
                amount           = amount,
                currency         = "USD",
                category         = category,
                description      = description,
                vendor           = vendor,
                fund_source      = _clean(item.get("fund_source")),
                fiscal_year      = _clean(item.get("fiscal_year")),
                action_type      = action,
                chunk_ids        = window.chunk_ids,
                evidence_text    = evidence,
                source_type      = window.source_type,
                start_time_sec   = window.start_time_sec,
                end_time_sec     = window.end_time_sec,
                extractor_version= EXTRACTOR_VERSION,
                confidence       = conf,
                needs_review     = needs_review,
            ))
            inserted += 1

    if discarded:
        log.debug("financial: %d discarded for meeting %s", discarded, meeting.video_id)

    return inserted


# ---------------------------------------------------------------------------
# Personnel extractor
# ---------------------------------------------------------------------------

def extract_personnel(
    meeting: Meeting,
    school: School,
    windows: list[CandidateWindow],
    db_session,
) -> int:
    db_session.execute(
        delete(PersonnelAction).where(PersonnelAction.meeting_id == meeting.meeting_id)
    )

    inserted  = 0
    discarded = 0
    seen: set[tuple] = set()

    for window in windows:
        evidence = window.evidence_snippet

        items = _call_llm(_PERSONNEL_PROMPT.format(window=window.text), "personnel")

        for item in items:
            if not isinstance(item, dict):
                continue

            action   = _vocab(item.get("action_type"), _PERSONNEL_ACTIONS, "other")
            position = _clean(item.get("position"))

            if not position:
                _log_discard(meeting.video_id, "personnel_actions", item,
                             "position is null")
                discarded += 1
                continue

            # ── Clean position: strip parentheticals, trailing punctuation ──
            # e.g. "Colonel Yra (leading and expanding roles)" → "Colonel Yra"
            position = _clean_position(position)

            # ── Hard discard: governance role ─────────────────────────────
            if _is_governance_role(position):
                _log_discard(meeting.video_id, "personnel_actions", item,
                             f"governance role filtered: '{position}'")
                discarded += 1
                continue

            # ── Hard discard: action_type = 'other' ────────────────────────
            if action == "other":
                _log_discard(meeting.video_id, "personnel_actions", item,
                             "action_type='other' — unclassifiable, discarded")
                discarded += 1
                continue

            # ── Fix: name embedded in position field ──────────────────────
            person_name = _clean(item.get("person_name"))
            position, extracted_name = _split_name_from_position(position)
            if extracted_name and not person_name:
                if _name_in_evidence(extracted_name, evidence):
                    person_name = extracted_name

            # ── Null-normalize non-name person_name ───────────────────────
            # e.g. person_name="Counseling" (department), "Union", "Major" (rank)
            if person_name and _is_non_name(person_name):
                _log_discard(meeting.video_id, "personnel_actions",
                             {"position": position, "person_name": person_name},
                             f"person_name='{person_name}' is not a person — nullified")
                person_name = None
                # Don't discard the row — the position may still be valid

            # ── Hard discard: intro language in evidence ───────────────────
            if _has_intro_language(evidence):
                _log_discard(meeting.video_id, "personnel_actions", item,
                             "intro language detected in evidence — likely speaker presentation")
                discarded += 1
                continue

            # ── Hard discard: bare title + no person_name + no effective_date
            is_bare       = _is_bare_title(position)
            is_truncated  = _is_truncated_position(position)
            effective_date = _clean(item.get("effective_date"))
            if is_bare and not person_name and not effective_date:
                _log_discard(meeting.video_id, "personnel_actions", item,
                             f"bare title '{position}' with no name or date — insufficient")
                discarded += 1
                continue

            # ── Dedup: normalize prepositions so "of" vs "for" match ──────
            # Prevents double-inserting e.g. "VP of Finance" and "VP for Finance"
            _pos_norm = re.sub(r"\b(of|for|in)\b", "of", position.lower()).rstrip(".,;: ")
            dedup_key = (action, _pos_norm)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            conf, needs_review = _personnel_confidence(item, evidence, window.window_score)

            # ── Hard discard: confidence floor ────────────────────────────
            if conf < _CONF_FLOOR_PERSONNEL:
                _log_discard(meeting.video_id, "personnel_actions", item,
                             f"confidence {conf:.3f} below floor {_CONF_FLOOR_PERSONNEL}")
                discarded += 1
                continue

            # Null-normalize person_name not found in evidence
            if person_name and not _name_in_evidence(person_name, evidence):
                person_name  = None
                needs_review = True

            # Bare title: always flag for human review
            if is_bare:
                needs_review = True
                conf = min(conf, 0.64)

            # Truncated position: flag for review, apply confidence penalty
            if is_truncated:
                needs_review = True
                conf = round(min(conf, 0.60), 3)

            is_interim = bool(item.get("is_interim")) or (
                "interim" in (position or "").lower()
            )

            db_session.add(PersonnelAction(
                meeting_id       = meeting.meeting_id,
                school_id        = school.school_id,
                school_slug      = school.slug,
                video_id         = meeting.video_id,
                person_name      = person_name,
                action_type      = action,
                position         = position,
                department       = _clean(item.get("department")),
                effective_date   = effective_date,
                is_interim       = is_interim,
                chunk_ids        = window.chunk_ids,
                evidence_text    = evidence,
                source_type      = window.source_type,
                start_time_sec   = window.start_time_sec,
                end_time_sec     = window.end_time_sec,
                extractor_version= EXTRACTOR_VERSION,
                confidence       = conf,
                needs_review     = needs_review,
            ))
            inserted += 1

    if discarded:
        log.debug("personnel: %d discarded for meeting %s", discarded, meeting.video_id)

    return inserted


# ---------------------------------------------------------------------------
# Per-meeting orchestrator
# ---------------------------------------------------------------------------

def extract_meeting(
    db_session,
    meeting: Meeting,
    school: School,
    do_votes:     bool = True,
    do_financial: bool = True,
    do_personnel: bool = True,
) -> dict:
    started_at = datetime.now(timezone.utc)

    # Resolve chunks.jsonl path
    jsonl_path = None
    if meeting.chunks_jsonl_path:
        jsonl_path = Path(meeting.chunks_jsonl_path)
    if not jsonl_path or not jsonl_path.exists():
        jsonl_path = (
            config.PROCESSED_DIR / school.slug / meeting.video_id / "chunks.jsonl"
        )

    result = {"votes": 0, "financial": 0, "personnel": 0, "status": "success"}

    if not jsonl_path.exists():
        result["status"] = "error: chunks.jsonl not found"
        return result

    targets = []
    if do_votes:     targets.append(ExtractionTarget.VOTES)
    if do_financial: targets.append(ExtractionTarget.FINANCIAL)
    if do_personnel: targets.append(ExtractionTarget.PERSONNEL)

    try:
        all_windows = find_all_candidates(jsonl_path, targets)

        if do_votes:
            windows = all_windows.get(ExtractionTarget.VOTES, [])
            result["votes"] = extract_votes(meeting, school, windows, db_session)

        if do_financial:
            windows = all_windows.get(ExtractionTarget.FINANCIAL, [])
            result["financial"] = extract_financial(meeting, school, windows, db_session)

        if do_personnel:
            windows = all_windows.get(ExtractionTarget.PERSONNEL, [])
            result["personnel"] = extract_personnel(meeting, school, windows, db_session)

        # Preserve 'indexed' (later pipeline stage) — don't downgrade. For
        # meetings that haven't hit indexer.py yet, advance processed → extracted.
        if meeting.status != "indexed":
            meeting.status = "extracted"

    except Exception as e:
        result["status"] = f"error: {e}"
        log.error("Extraction failed for %s: %s", meeting.video_id, e, exc_info=True)

    duration = (datetime.now(timezone.utc) - started_at).total_seconds()
    result["duration"] = duration

    db_session.add(PipelineRun(
        meeting_id       = meeting.meeting_id,
        channel_id       = None,
        phase            = "extraction",
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
        description="Neo v2 Phase 6 — Structured Extractor v2.5"
    )
    parser.add_argument("--school",         help="Process only this school slug")
    parser.add_argument("--limit",          type=int, help="Process at most N meetings")
    parser.add_argument("--reextract",      action="store_true",
                        help="Re-run meetings already marked extracted")
    parser.add_argument("--skip-votes",     action="store_true")
    parser.add_argument("--skip-financial", action="store_true")
    parser.add_argument("--skip-personnel", action="store_true")
    args = parser.parse_args()

    do_votes     = not args.skip_votes
    do_financial = not args.skip_financial
    do_personnel = not args.skip_personnel

    print("Neo v2 — Phase 6: Structured Extraction v2.5")
    print("=" * 55)
    print(f"LLM        : {describe()}")
    print(f"Extractors : "
          f"{'votes ' if do_votes else ''}"
          f"{'financial ' if do_financial else ''}"
          f"{'personnel' if do_personnel else ''}")
    print(f"Version    : {EXTRACTOR_VERSION}")
    print(f"Floors     : votes≥{_CONF_FLOOR_VOTES} "
          f"financial≥{_CONF_FLOOR_FINANCIAL} "
          f"personnel≥{_CONF_FLOOR_PERSONNEL}")

    ok, detail = check_llm()
    if not ok:
        print(f"\n❌  Extraction LLM unavailable — {detail}")
        if config.PIPELINE_LLM_PROVIDER == "ollama":
            print(f"    Start:  ollama serve")
            print(f"    Pull:   ollama pull {config.PIPELINE_LLM_MODEL}")
        else:
            print(f"    Check PIPELINE_LLM_PROVIDER / PIPELINE_LLM_MODEL / PIPELINE_LLM_API_KEY")
        sys.exit(1)

    print(f"\n✅  LLM ready — {detail}\n")
    reset_usage()

    engine  = create_engine(config.DATABASE_URL, echo=config.SQL_ECHO)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        query = (
            session.query(Meeting, School)
            .join(School, School.school_id == Meeting.school_id)
            # Eligibility comes from pipeline.states — the diamond between
            # ("processed", "approved") encodes that quality_gate and the
            # extractor are order-independent. --reextract additionally picks
            # up already-extracted/indexed meetings (e.g. after a prompt change).
            .filter(Meeting.status.in_(
                eligible_inputs("extractor", rerun=args.reextract)
            ))
            .order_by(School.school_id, Meeting.published_date.desc())
        )
        if args.school:
            query = query.filter(School.slug == args.school)
        if args.limit:
            query = query.limit(args.limit)

        meetings = query.all()

        if not meetings:
            print("[INFO] No meetings ready for extraction.")
            print("       Run processor.py first (status must be 'processed').")
            sys.exit(0)

        total = len(meetings)
        print(f"Meetings : {total}\n")

        total_votes = total_financial = total_personnel = errors = 0

        for i, (meeting, school) in enumerate(meetings, 1):
            title_short = (meeting.title or meeting.video_id)[:50]
            print(f"[{i:>3}/{total}] {school.slug[:20]:<20} | {title_short}")

            result = extract_meeting(
                session, meeting, school,
                do_votes=do_votes,
                do_financial=do_financial,
                do_personnel=do_personnel,
            )

            if result["status"] != "success":
                errors += 1
                print(f"          ❌ {result['status']}")
            else:
                total_votes      += result["votes"]
                total_financial  += result["financial"]
                total_personnel  += result["personnel"]
                print(
                    f"          ✅  "
                    f"votes={result['votes']}  "
                    f"financial={result['financial']}  "
                    f"personnel={result['personnel']}  "
                    f"({result['duration']:.0f}s)"
                )

            if i % 5 == 0:
                session.commit()
                print(f"          [checkpoint {i}/{total}]")

        session.commit()

    u = usage()
    print("\n" + "=" * 55)
    print("Phase 6 complete:")
    print(f"  Meetings   : {total}")
    print(f"  Votes      : {total_votes}")
    print(f"  Financial  : {total_financial}")
    print(f"  Personnel  : {total_personnel}")
    print(f"  Errors     : {errors}")
    print(f"  LLM        : {describe()}")
    print(f"  Usage      : {u.summary()}")
    if u.truncations:
        print(f"\n⚠️  {u.truncations} call(s) hit the {config.PIPELINE_LLM_MAX_TOKENS}-token cap and")
        print("    returned incomplete JSON — those windows produced NO rows.")
        print("    Raise PIPELINE_LLM_MAX_TOKENS and re-run with --reextract.")
    print()
    print("Validate:")
    print('  psql -U postgres -d neo_v2 -c "SELECT topic_category, COUNT(*), ROUND(AVG(confidence)::numeric,2), SUM(needs_review::int) FROM votes GROUP BY 1 ORDER BY 2 DESC;"')
    print('  psql -U postgres -d neo_v2 -c "SELECT action_type, COUNT(*), ROUND(AVG(confidence)::numeric,2), SUM(needs_review::int) FROM financial_items GROUP BY 1 ORDER BY 2 DESC;"')
    print('  psql -U postgres -d neo_v2 -c "SELECT action_type, COUNT(*), ROUND(AVG(confidence)::numeric,2), SUM(needs_review::int) FROM personnel_actions GROUP BY 1 ORDER BY 2 DESC;"')
    print()
    print("Discard logs:")
    print("  ls -la Neo_v2/data/extraction_discards/")
    print()
    print("⚠️  Review all rows with needs_review=true before surfacing to trustees.")


if __name__ == "__main__":
    main()
