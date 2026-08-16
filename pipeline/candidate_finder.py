"""
Neo v2 — Phase 6: Candidate Finder (rules engine, zero LLM)

Scores every chunk in a meeting's chunks.jsonl against keyword/regex
pattern sets for three extraction targets: votes, financial items,
and personnel actions.

Scoring is two-pass:
  1. Positive patterns add weight (signal present)
  2. Negative patterns subtract weight (noise/false-positive signals)

Net score must exceed MIN_SCORE for a chunk to become a candidate window.
This prevents financial/budget discussions from generating vote candidates,
and speaker introductions from generating personnel candidates.

No LLM calls happen here.  Fast, deterministic, fully unit-testable.

Usage:
    from pipeline.candidate_finder import find_candidates, ExtractionTarget
    windows = find_candidates(chunks_jsonl_path, ExtractionTarget.VOTES)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Extraction targets
# ---------------------------------------------------------------------------

class ExtractionTarget(Enum):
    VOTES      = "votes"
    FINANCIAL  = "financial"
    PERSONNEL  = "personnel"
    INITIATIVE = "initiative"   # Phase 6.5


# ---------------------------------------------------------------------------
# Positive pattern sets
# ---------------------------------------------------------------------------

_VOTE_PATTERNS: list[tuple[str, float]] = [
    # Strong: explicit vote-conclusion language
    (r"\bthe ayes have it\b",                                  4.0),
    (r"\bmotion (carries|passes|passed|failed|is denied)\b",   4.0),
    (r"\bvoting has concluded\b",                              4.0),
    (r"\bmotion is approved\b",                                4.0),
    # Strong: formal vote mechanics
    (r"\bayes?\b|\bnays?\b",                                   3.5),
    (r"\bfavor say aye\b|\ball in favor\b",                    3.5),
    (r"\bvote of \d",                                          3.5),
    (r"\bvoting is open\b|\bcast your vote\b",                 3.0),
    # Medium: motion language
    (r"\bI move\b|\bso moved\b|\bmove that\b",                 3.0),
    (r"\bmotion to\b",                                         2.5),
    (r"\bseconded?\b",                                         2.0),
    (r"\bunanimous(ly)?\b",                                    2.5),
    (r"\bany opposed\b|\bno objection\b",                      2.5),
    (r"\broll call\b",                                         2.0),
    # Weaker: mentions
    (r"\bmotion\b",                                            1.5),
]

_VOTE_NEGATIVE_PATTERNS: list[tuple[str, float]] = [
    # Future-tense voting = not an actual vote
    (r"\bwill (be )?vot(e|ing)\b",                            -3.0),
    (r"\bplan(ning)? to vote\b",                              -3.0),
    (r"\bwill (be )?brought (back )?to a vote\b",             -3.0),
    (r"\bnext meeting\b|\bnext (month|session|time)\b",       -2.0),
    (r"\bscheduled for (a )?(vote|approval)\b",               -2.0),
    # Intro/presentation context — no vote happening
    (r"\bplease welcome\b|\bthank you for joining\b",         -3.0),
    (r"\bwill (now )?present\b|\bwill (now )?provide\b",      -2.0),
    # Discussion about voting, not voting itself
    (r"\bhow (we|the board) (should |will )?vote\b",          -2.0),
    (r"\bvoting (procedure|process|guideline)\b",             -2.0),
]

_FINANCIAL_PATTERNS: list[tuple[str, float]] = [
    # Strong: explicit dollar amounts
    (r"\$[\d,]+",                                              3.0),
    (r"\b\d[\d,.]*\s*(million|thousand|billion)\b",            2.5),
    (r"\b\d+\.?\d*\s*[MKB]\b",                                2.0),
    # Strong: board action language tied to money
    (r"\bapprove.*contract\b|\bcontract.*approv\w+\b",         3.0),
    (r"\baward(ed)? (the |a )?contract\b",                     3.0),
    (r"\bbudget amendment\b",                                  2.5),
    (r"\bapprove.*fund\b|\bfund.*approv\w+\b",                 2.5),
    (r"\bbond (measure|issuance|sale)\b",                      2.5),
    # Medium: financial action terms
    (r"\bcontract\b",                                          1.5),
    (r"\bgrant (of|from|award|funding)\b",                     2.0),
    (r"\bpurchase order\b|\bprocurement\b",                    2.0),
    (r"\bappropriat\w+\b",                                     1.5),
    (r"\bcapital (project|outlay|expenditure)\b",              2.0),
    (r"\bsalary schedule\b",                                   2.0),
    (r"\bprofessional services\b",                             1.5),
    (r"\bfiscal year\b|\bFY\s*\d{2,4}\b",                     1.0),
]

_FINANCIAL_NEGATIVE_PATTERNS: list[tuple[str, float]] = [
    # Historical or comparative references — not a board action
    (r"\blast year (it was|the amount|we had|we received)\b", -2.5),
    (r"\bcompared to (last|prior|previous)\b",                -2.0),
    (r"\bhistorical(ly)?\b",                                  -1.5),
    (r"\bstate (gave|allocated|provided|awarded) .*to\b",     -2.5),
    (r"\bfederal (gave|provided|awarded)\b",                  -2.5),
    (r"\bfor example\b|\bsuch as\b|\bfor instance\b",         -1.0),
]

_PERSONNEL_PATTERNS: list[tuple[str, float]] = [
    # Strong: formal board action language
    (r"\bapprove the (employment|appointment|hiring)\b",       4.0),
    (r"\bmotion to (approve|accept).*(employ|appoint|hire)\b", 4.0),
    (r"\bterminat\w+\b",                                       3.5),
    (r"\bresign\w*\b",                                         3.5),
    (r"\bretir\w+\b",                                          3.0),
    (r"\beffective (date|immediately|July|August|September|January|February|March|April|May|June|October|November|December)\b", 3.0),
    # Medium: appointment/hire language
    (r"\bappoint(ment|ed|ing)?\b",                             2.5),
    (r"\bhir(e|ed|ing)\b",                                     2.5),
    (r"\bemployment of\b",                                     2.5),
    (r"\bnew (hire|position|appointment)\b",                   2.5),
    (r"\bfaculty appointment\b|\bstaff appointment\b",         2.5),
    (r"\bpersonnel (action|report|item)\b",                    2.5),
    (r"\binterim (appointment|hire|position)\b",               2.5),
    # Weaker: title/role mentions (need co-occurrence to clear threshold)
    (r"\bdirector of\b|\bdean of\b",                           1.5),
    (r"\bvice (chancellor|president) of\b",                    1.5),  # "of" = specific role
    (r"\bposition of\b",                                       1.5),
    (r"\binterim\b",                                           1.0),
]

_PERSONNEL_NEGATIVE_PATTERNS: list[tuple[str, float]] = [
    # Speaker introductions — NOT personnel actions
    (r"\bplease welcome\b",                                   -4.0),
    (r"\bthank you for joining\b|\bjoining us (today|this evening|tonight)\b", -3.5),
    (r"\bwill (now )?present\b|\bwill (now )?provide (an )?overview\b", -3.0),
    (r"\bI (would like to |want to )?introduce\b",            -3.0),
    (r"\bhere to (answer|present|provide|share)\b",           -2.5),
    # Historical mentions — not a current action
    (r"\bhas been (serving|working|with us) (for|since)\b",   -2.5),
    (r"\bworking (with|alongside) (our|the)\b",               -2.0),
    # Delegation/reference language
    (r"\bled by (our|the)\b|\bsupported by\b",                -1.5),
    (r"\bresponded? to (board |trustee )?questions\b",        -1.5),
]

_INITIATIVE_PATTERNS: list[tuple[str, float]] = [
    (r"\blaunch(ed|ing)?\b|\bpilot (program|project)\b",      2.5),
    (r"\bprogram (approval|launch|expansion)\b",              2.5),
    (r"\bstrategic (plan|initiative|goal|priority)\b",        2.5),
    (r"\bworkforce (development|training|pipeline)\b",        2.5),
    (r"\bpartnership (with|agreement)\b",                     2.0),
    (r"\bMOU\b|\bmemorandum of understanding\b",              2.5),
    (r"\bnew (program|initiative|course|certificate)\b",      2.0),
    (r"\baccreditation\b|\bcurriculum (approval|change)\b",   2.0),
    (r"\bgrant (funded|award|program)\b",                     2.0),
    (r"\bequity (initiative|plan|program)\b",                 2.0),
    (r"\btechnology (upgrade|project|initiative)\b",          2.0),
    (r"\bdual credit\b|\bdual enrollment\b",                  2.0),
    (r"\bstudent success (initiative|plan|program)\b",        2.5),
    (r"\bexpand\w+\b|\bgrow\w+\b",                            1.0),
    (r"\bconstruction\b|\brenovation\b|\bfacility\b",         1.5),
]

_INITIATIVE_NEGATIVE_PATTERNS: list[tuple[str, float]] = []   # none yet

_PATTERN_MAP: dict[ExtractionTarget, list[tuple[str, float]]] = {
    ExtractionTarget.VOTES:      _VOTE_PATTERNS,
    ExtractionTarget.FINANCIAL:  _FINANCIAL_PATTERNS,
    ExtractionTarget.PERSONNEL:  _PERSONNEL_PATTERNS,
    ExtractionTarget.INITIATIVE: _INITIATIVE_PATTERNS,
}

_NEGATIVE_MAP: dict[ExtractionTarget, list[tuple[str, float]]] = {
    ExtractionTarget.VOTES:      _VOTE_NEGATIVE_PATTERNS,
    ExtractionTarget.FINANCIAL:  _FINANCIAL_NEGATIVE_PATTERNS,
    ExtractionTarget.PERSONNEL:  _PERSONNEL_NEGATIVE_PATTERNS,
    ExtractionTarget.INITIATIVE: _INITIATIVE_NEGATIVE_PATTERNS,
}

# ---------------------------------------------------------------------------
# Thresholds and window sizes
# ---------------------------------------------------------------------------

# Net score must exceed this for a chunk to become a candidate.
# Votes is highest: budget/financial windows commonly hit vote keywords.
# Personnel is higher than financial: intro language is a major false-positive source.
_MIN_SCORE: dict[ExtractionTarget, float] = {
    ExtractionTarget.VOTES:      5.0,   # requires multiple strong co-occurring patterns
    ExtractionTarget.FINANCIAL:  2.5,
    ExtractionTarget.PERSONNEL:  3.5,   # raised to filter speaker intros
    ExtractionTarget.INITIATIVE: 2.5,
}

# Context chunks on each side of a hit chunk.
# Tighter = fewer false positives; looser = fewer missed events.
# Vote language is dense (1–2 sentences); financial amounts need minimal context.
_CONTEXT_CHUNKS: dict[ExtractionTarget, int] = {
    ExtractionTarget.VOTES:      2,    # reduced from 3 — vote language is compact
    ExtractionTarget.FINANCIAL:  1,    # reduced from 2 — dollar amounts are tight
    ExtractionTarget.PERSONNEL:  2,
    ExtractionTarget.INITIATIVE: 4,    # initiatives span multiple paragraphs
}

# Hard cap per window before recursive splitting
_MAX_WINDOW_TOKENS = 700


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ChunkRecord:
    """A single chunk from chunks.jsonl."""
    chunk_id:     str
    chunk_index:  int
    text:         str
    start_time:   Optional[float]
    end_time:     Optional[float]
    token_count:  int
    quality_score: float
    source_type:  str
    score:        float = 0.0   # net score after positive + negative patterns


@dataclass
class CandidateWindow:
    """A contiguous slice of chunks forming one evidence window for LLM extraction."""
    chunks:       list[ChunkRecord]
    target:       ExtractionTarget
    peak_score:   float          # highest individual chunk net score in window
    window_score: float          # sum of all chunk net scores in window
    text:          str = field(init=False)
    chunk_ids:     list[str] = field(init=False)
    start_time_sec: Optional[float] = field(init=False)
    end_time_sec:   Optional[float] = field(init=False)
    source_type:   str = field(init=False)

    def __post_init__(self) -> None:
        # Chunks are labelled with their STABLE chunk_id, not their position in
        # this window.  The model cites these IDs back in its evidence array and
        # we resolve them against the meeting's real chunks — a list position
        # would be meaningless the moment window boundaries move.
        self.text = "\n\n".join(
            f"[[CHUNK_ID: {c.chunk_id}]]\n"
            f"[[START_TIME: {c.start_time if c.start_time is not None else 'null'}]]\n"
            f"[[END_TIME: {c.end_time if c.end_time is not None else 'null'}]]\n"
            f"{c.text}"
            for c in self.chunks
        )
        self.chunk_ids = [c.chunk_id for c in self.chunks]
        times     = [c.start_time for c in self.chunks if c.start_time is not None]
        end_times = [c.end_time   for c in self.chunks if c.end_time   is not None]
        self.start_time_sec = min(times)     if times     else None
        self.end_time_sec   = max(end_times) if end_times else None
        types = [c.source_type for c in self.chunks if c.source_type]
        self.source_type = max(set(types), key=types.count) if types else "unknown"

    @property
    def token_count(self) -> int:
        return sum(c.token_count for c in self.chunks)

    @property
    def evidence_snippet(self) -> str:
        """
        First 500 chars of the highest net-scored chunk.

        FALLBACK ONLY.  This is a blind slice of the window — it is not derived
        from any extracted claim and frequently does not contain it.  Prefer a
        preview built around a verified quote (pipeline/evidence.py:
        build_preview); use this only when an item has no verified evidence.
        """
        best = max(self.chunks, key=lambda c: c.score)
        return best.text[:500]

    @property
    def chunks_by_id(self) -> dict[str, ChunkRecord]:
        """Chunk lookup for validating model-cited chunk_ids against this window."""
        return {c.chunk_id: c for c in self.chunks}


# ---------------------------------------------------------------------------
# Core scoring  (positive + negative net score)
# ---------------------------------------------------------------------------

def _score_chunk(
    text: str,
    pos_patterns: list[tuple[str, float]],
    neg_patterns: list[tuple[str, float]],
) -> float:
    """
    Net score = sum of matched positive weights − sum of matched negative weights.
    Score is floored at 0 so a chunk can never have a negative score
    (negative patterns only suppress, they don't penalise neighbours).
    """
    tl = text.lower()
    pos = sum(w for pat, w in pos_patterns if re.search(pat, tl))
    neg = sum(abs(w) for pat, w in neg_patterns if re.search(pat, tl))
    return max(0.0, pos - neg)


def _load_chunks(jsonl_path: Path) -> list[ChunkRecord]:
    records: list[ChunkRecord] = []
    with open(jsonl_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.append(ChunkRecord(
                chunk_id    = d.get("chunk_id", ""),
                chunk_index = d.get("chunk_index", 0),
                text        = d.get("text", ""),
                start_time  = d.get("start_time"),
                end_time    = d.get("end_time"),
                token_count = d.get("token_count") or _estimate_tokens(d.get("text", "")),
                quality_score = d.get("quality_score", 0.5),
                source_type = d.get("source_type", "unknown"),
            ))
    return sorted(records, key=lambda r: r.chunk_index)


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text.split()) * 1.3))


# ---------------------------------------------------------------------------
# Window builder
# ---------------------------------------------------------------------------

def _build_windows(
    chunks: list[ChunkRecord],
    target: ExtractionTarget,
) -> list[CandidateWindow]:
    """
    1. Score every chunk (positive − negative patterns).
    2. Identify hits: net score >= MIN_SCORE[target].
    3. Expand each hit by ±CONTEXT_CHUNKS on each side.
    4. Merge overlapping ranges.
    5. Split windows exceeding MAX_WINDOW_TOKENS recursively.
    6. Drop windows where no hit chunk survived the split (peak_score == 0).
    7. Return sorted by window_score descending.
    """
    pos_pats   = _PATTERN_MAP[target]
    neg_pats   = _NEGATIVE_MAP[target]
    min_score  = _MIN_SCORE[target]
    context    = _CONTEXT_CHUNKS[target]
    n          = len(chunks)

    # Step 1: score all chunks
    for chunk in chunks:
        chunk.score = _score_chunk(chunk.text, pos_pats, neg_pats)

    # Step 2: find hits
    hit_indices: set[int] = {i for i, c in enumerate(chunks) if c.score >= min_score}
    if not hit_indices:
        return []

    # Step 3: expand to ranges
    ranges: list[tuple[int, int]] = [
        (max(0, i - context), min(n - 1, i + context))
        for i in hit_indices
    ]

    # Step 4: merge overlapping
    ranges.sort()
    merged: list[tuple[int, int]] = []
    cur_lo, cur_hi = ranges[0]
    for lo, hi in ranges[1:]:
        if lo <= cur_hi + 1:
            cur_hi = max(cur_hi, hi)
        else:
            merged.append((cur_lo, cur_hi))
            cur_lo, cur_hi = lo, hi
    merged.append((cur_lo, cur_hi))

    # Step 5: build windows with recursive splitting
    windows: list[CandidateWindow] = []
    for lo, hi in merged:
        windows.extend(_maybe_split(chunks[lo : hi + 1], target))

    # Step 6: drop context-only sub-windows (hit chunk ended up in other half)
    windows = [w for w in windows if w.peak_score >= min_score]

    # Step 7: sort by window_score descending
    windows.sort(key=lambda w: w.window_score, reverse=True)
    return windows


def _maybe_split(
    window_chunks: list[ChunkRecord],
    target: ExtractionTarget,
) -> list[CandidateWindow]:
    """Recursively split until every piece is under MAX_WINDOW_TOKENS."""
    total = sum(c.token_count for c in window_chunks)
    if total <= _MAX_WINDOW_TOKENS or len(window_chunks) <= 2:
        return [_make_window(window_chunks, target)]
    mid = len(window_chunks) // 2
    return _maybe_split(window_chunks[:mid], target) + _maybe_split(window_chunks[mid:], target)


def _make_window(chunks: list[ChunkRecord], target: ExtractionTarget) -> CandidateWindow:
    return CandidateWindow(
        chunks=chunks,
        target=target,
        peak_score=max(c.score for c in chunks),
        window_score=sum(c.score for c in chunks),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_candidates(
    jsonl_path: Path | str,
    target: ExtractionTarget,
) -> list[CandidateWindow]:
    """Return ranked CandidateWindow list for one target. Returns [] if none found."""
    path = Path(jsonl_path)
    if not path.exists():
        return []
    chunks = _load_chunks(path)
    if not chunks:
        return []
    return _build_windows(chunks, target)


def find_all_candidates(
    jsonl_path: Path | str,
    targets: Optional[list[ExtractionTarget]] = None,
) -> dict[ExtractionTarget, list[CandidateWindow]]:
    """Run candidate finding for multiple targets loading chunks only once."""
    if targets is None:
        targets = list(ExtractionTarget)
    path = Path(jsonl_path)
    if not path.exists():
        return {t: [] for t in targets}
    chunks = _load_chunks(path)
    if not chunks:
        return {t: [] for t in targets}
    result: dict[ExtractionTarget, list[CandidateWindow]] = {}
    for target in targets:
        for c in chunks:
            c.score = 0.0
        result[target] = _build_windows(chunks, target)
    return result


# ---------------------------------------------------------------------------
# Diagnostic CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Candidate Finder diagnostic")
    parser.add_argument("jsonl", help="Path to chunks.jsonl")
    parser.add_argument(
        "--target", choices=["votes", "financial", "personnel", "initiative"],
        default="votes",
    )
    args = parser.parse_args()

    target_map = {
        "votes":      ExtractionTarget.VOTES,
        "financial":  ExtractionTarget.FINANCIAL,
        "personnel":  ExtractionTarget.PERSONNEL,
        "initiative": ExtractionTarget.INITIATIVE,
    }
    t = target_map[args.target]
    windows = find_candidates(args.jsonl, t)

    print(f"\nTarget  : {t.value}")
    print(f"Windows : {len(windows)}\n")
    for i, w in enumerate(windows, 1):
        print(f"  [{i:>2}] score={w.window_score:.1f}  peak={w.peak_score:.1f}  "
              f"tokens={w.token_count}  chunks={len(w.chunks)}")
        print(f"        {w.start_time_sec or 0:.0f}s–{w.end_time_sec or 0:.0f}s  "
              f"src={w.source_type}")
        print(f"        {w.evidence_snippet[:110]!r}")
        print()
