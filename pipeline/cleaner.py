"""
Neo v2 — Phase 5: Transcript Cleaner

Converts raw VTT or WhisperX JSON into a normalized list of CleanedSegment
objects ready for data_packager.py to turn into structured data products.

Handles both source types:
  • VTT  (status='captioned') — YouTube-generated captions
  • WhisperX JSON (status='transcribed') — GPU-transcribed audio

The VTT parsing logic is ported and improved from Neo v1's transcript_cleaner.py,
preserving the roll-up delta dedup and sentence normalization that worked well.
WhisperX JSON has richer data (word-level timing, speaker labels, confidence
scores) so gets lighter cleaning treatment.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Version stamp — bump when cleaning logic changes materially
# ---------------------------------------------------------------------------
CLEANER_VERSION = "5.0"

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CleanedWord:
    """A single word with timing from WhisperX (not available for VTT)."""
    word:       str
    start:      float
    end:        float
    score:      float = 0.0   # alignment confidence 0–1
    speaker:    Optional[str] = None


@dataclass
class CleanedSegment:
    """
    Canonical segment object produced by both clean_vtt() and clean_whisperx().
    data_packager.py consumes only this type — it never touches raw files.
    """
    start:       float                   # seconds from video start
    end:         float
    text:        str                     # cleaned, deduplicated text
    speaker:     Optional[str] = None    # "SPEAKER_00", "SPEAKER_01", … or None
    words:       List[CleanedWord] = field(default_factory=list)
    confidence:  float = 1.0             # 0–1; from avg_logprob for whisperx
    source_type: str   = "vtt"           # "vtt" | "whisperx_asr"


# ---------------------------------------------------------------------------
# Shared text-normalization helpers (ported from v1)
# ---------------------------------------------------------------------------

_HTML_TAG_RE   = re.compile(r"<[^>]+>")
_CUE_SPLIT_RE  = re.compile(r"\r?\n\r?\n")
_VTT_NOTE_RE   = re.compile(r"^\s*NOTE\b", re.IGNORECASE)
_ALLOWED_PUNCT = set(list(".,?!:;-—()'\""))
_SENT_END_RE   = re.compile(r"(?<=[\.!?])\s+")
_FILLERS       = {"um", "uh", "er", "erm"}
_STOPWORDS     = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for",
    "with", "at", "by", "is", "are", "was", "were", "be", "been", "being",
    "this", "that", "it", "as", "from", "we", "you", "they", "he", "she",
    "i", "our", "your", "their",
}

_ANNOTS_BRACKET_RE = re.compile(
    r"(?:\[([A-Z0-9 \-_/]{2,})\]|\(([A-Z0-9 \-_/]{2,})\))"
)

_CUE_TIME_RE = re.compile(
    r"^(?P<start>\d{2}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})\s*-->\s*"
    r"(?P<end>\d{2}:\d{2}:\d{2}\.\d{3}|\d{2}:\d{2}\.\d{3})",
    re.VERBOSE,
)

_WORD_TAG_RE = re.compile(
    r"<(\d{2}:\d{2}:\d{2}\.\d{3})>"
    r"(?:<c[^>]*>)?"
    r"([^<]+?)"
    r"(?:</c>)?",
)

_CONTRACTIONS = {
    "don't": "do not", "doesn't": "does not", "didn't": "did not",
    "won't": "will not", "wouldn't": "would not", "can't": "cannot",
    "couldn't": "could not", "shouldn't": "should not", "isn't": "is not",
    "aren't": "are not", "wasn't": "was not", "weren't": "were not",
    "i'm": "I am", "we're": "we are", "you're": "you are", "they're": "they are",
    "i've": "I have", "we've": "we have", "i'll": "I will", "we'll": "we will",
    "it's": "it is", "that's": "that is", "there's": "there is",
    "what's": "what is", "let's": "let us", "who's": "who is",
    "i'd": "I would", "we'd": "we would", "he's": "he is", "she's": "she is",
    "could've": "could have", "would've": "would have", "should've": "should have",
}
_CONTRACTION_RE = re.compile(
    r"|".join(re.escape(k) for k in sorted(_CONTRACTIONS, key=len, reverse=True)),
    re.IGNORECASE,
)


def _normalize_unicode_punct(text: str) -> str:
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u2014", "-").replace("\u2013", "-")
    text = text.replace("\u2026", "...").replace("\u00A0", " ").replace("\u200B", "")
    return text


def _html_unescape(text: str) -> str:
    return html.unescape(text)


def _strip_filler_words(text: str) -> str:
    """Remove um/uh/er at word boundaries."""
    pattern = r"\b(?:" + "|".join(_FILLERS) + r")\b[,\s]*"
    return re.sub(pattern, " ", text, flags=re.IGNORECASE).strip()


def _expand_contractions(text: str) -> str:
    return _CONTRACTION_RE.sub(
        lambda m: _CONTRACTIONS.get(m.group(0).lower(), m.group(0)), text
    )


def _tidy_spaces(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,?!:;])", r"\1", text)
    text = re.sub(r"\.{4,}", "...", text)
    text = re.sub(r"!{2,}", "!", text)
    text = re.sub(r"\?{2,}", "?", text)
    return text.strip()


def _normalize_for_compare(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def _norm_no_stop(s: str) -> str:
    toks = re.findall(r"[A-Za-z0-9']+", s.lower())
    return " ".join(t for t in toks if t not in _STOPWORDS)


def _dedupe_adjacent(sents: List[str]) -> List[str]:
    out, prev_norm, prev_ns = [], "", ""
    for s in sents:
        cur_norm = _normalize_for_compare(s)
        cur_ns   = _norm_no_stop(s)
        if cur_norm == prev_norm:
            continue
        if cur_ns and prev_ns and cur_ns == prev_ns:
            continue
        out.append(s)
        prev_norm, prev_ns = cur_norm, cur_ns
    return out


def _suppress_ngram_repeats(text: str, n: int = 2) -> str:
    toks = text.split()
    out: List[str] = []
    for tok in toks:
        out.append(tok)
        if len(out) >= 2 * n and out[-n:] == out[-2 * n:-n]:
            del out[-n:]
    return " ".join(out)


# ---------------------------------------------------------------------------
# VTT parsing helpers (ported + improved from v1)
# ---------------------------------------------------------------------------

def _parse_vtt_ts(ts: str) -> float:
    ts = ts.strip()
    m = re.match(r"(?:(\d{1,2}):)?(\d{2}):(\d{2})\.(\d{3})$", ts)
    if m:
        h   = int(m.group(1) or 0)
        mn  = int(m.group(2))
        s   = int(m.group(3))
        ms  = int(m.group(4))
        return h * 3600 + mn * 60 + s + ms / 1000.0
    return 0.0


def _extract_new_content(accumulated: str, new_cue: str) -> str:
    """
    Extract only the genuinely new words from new_cue given what's already
    in the accumulated group text.

    YouTube VTT rolling windows can RESTART from any position in the visible
    text buffer — not just from the tail of the previous cue. This function
    searches for the leading tokens of new_cue *anywhere* in the last 60 words
    of accumulated text, then returns only the unmatched tail.

    Examples handled correctly:
      accumulated = "The board meetings will begin at 1:30"
      new_cue     = "The board meetings will"         → "" (already present)
      new_cue     = "begin at 1:30 this afternoon."  → "this afternoon."
      new_cue     = "Call to order. Item one."        → full text (no overlap)
    """
    if not accumulated:
        return new_cue

    acc_tok  = _normalize_for_compare(accumulated).split()
    new_norm = _normalize_for_compare(new_cue)
    if not new_norm:
        return ""

    new_tok  = new_norm.split()
    orig_tok = new_cue.split()

    # Search the last 60 tokens of accumulated for the start of new_cue
    window = acc_tok[-60:]
    best_overlap = 0

    for start in range(len(window)):
        match_len = 0
        while (match_len < len(new_tok)
               and start + match_len < len(window)
               and window[start + match_len] == new_tok[match_len]):
            match_len += 1
        if match_len > best_overlap:
            best_overlap = match_len

    if best_overlap == 0:
        return new_cue                          # nothing in common — keep all
    if best_overlap >= len(new_tok):
        return ""                               # entire cue already accumulated
    return " ".join(orig_tok[best_overlap:])    # return only the new tail


# ---------------------------------------------------------------------------
# Public API: clean_vtt
# ---------------------------------------------------------------------------

def clean_vtt(vtt_path: Path) -> List[CleanedSegment]:
    """
    Parse a WebVTT file and return a list of CleanedSegment objects.
    Groups cues into ~sentence-length segments; handles YouTube roll-up
    delta format.  No speaker info is available from VTT.
    """
    vtt_path = Path(vtt_path)
    raw = vtt_path.read_text(encoding="utf-8", errors="replace")

    if not raw.lstrip().upper().startswith("WEBVTT"):
        raise ValueError(f"Not a valid WEBVTT file: {vtt_path}")

    cues = _CUE_SPLIT_RE.split(raw.strip())
    segments: List[CleanedSegment] = []

    group_start   = 0.0
    group_end     = 0.0
    group_texts: List[str] = []

    def _flush_group():
        nonlocal group_texts, group_start, group_end
        if not group_texts:
            return
        stitched  = " ".join(group_texts)
        stitched  = _expand_contractions(stitched)
        sents     = [s.strip() for s in _SENT_END_RE.split(stitched) if s.strip()]
        sents     = _dedupe_adjacent(sents)
        text      = _suppress_ngram_repeats(" ".join(sents), n=8)
        text      = _tidy_spaces(text)
        if len(text.split()) >= 3:
            segments.append(CleanedSegment(
                start=group_start,
                end=group_end,
                text=text,
                source_type="vtt",
                confidence=_vtt_confidence(text, group_end - group_start),
            ))
        group_texts.clear()

    FLUSH_WORDS = 50   # flush to a new segment after ~50 words

    for cue in cues:
        if _VTT_NOTE_RE.match(cue):
            continue
        lines = [ln for ln in cue.splitlines() if ln.strip()]
        if not lines:
            continue

        time_idx = None
        start_s = end_s = None
        for i, ln in enumerate(lines[:3]):
            m = _CUE_TIME_RE.match(ln.strip())
            if m:
                start_s  = _parse_vtt_ts(m.group("start"))
                end_s    = _parse_vtt_ts(m.group("end"))
                time_idx = i
                break

        if time_idx is None:
            continue

        payload = "\n".join(lines[time_idx + 1:]).strip()
        if not payload:
            continue

        # Clean the cue payload
        txt = _HTML_TAG_RE.sub("", payload)
        txt = _html_unescape(txt)
        txt = txt.replace(">>", " ")
        txt = _normalize_unicode_punct(txt)
        txt = _ANNOTS_BRACKET_RE.sub(" ", txt)
        txt = _strip_filler_words(txt)
        txt = _tidy_spaces(txt)
        if not txt:
            continue

        # Extract only the words that are genuinely new relative to what
        # we've already accumulated in this segment group.
        accumulated = " ".join(group_texts)
        delta = _extract_new_content(accumulated, txt)
        if not delta:
            continue

        if not group_texts:
            group_start = start_s
        group_end = end_s
        group_texts.append(delta)

        # Flush when we've accumulated enough words or hit a sentence end
        total_words = len(accumulated.split()) + len(delta.split())
        if total_words >= FLUSH_WORDS and re.search(r"[.!?]\s*$", delta):
            _flush_group()

    _flush_group()  # emit any remaining text

    log.info("clean_vtt: %d segments from %s", len(segments), vtt_path.name)
    return segments


def _vtt_confidence(text: str, duration: float) -> float:
    """
    Heuristic confidence for VTT segments (no logprob available).
    Based on words-per-minute rate: boardroom speech is 100–160 wpm.

    NOTE: VTT segment duration includes silence/pauses between cues, so
    wpm naturally reads low for long meetings with executive sessions or
    recesses. Floor is kept at 0.65 to avoid false rejections.
    """
    words = len(text.split())
    if duration <= 0:
        return 0.85
    wpm = words / (duration / 60.0)
    # Only penalize extreme outliers — likely noise or garbage
    if wpm > 400:
        return 0.55
    # Normal boardroom speech (wide band to accommodate pauses)
    if wpm >= 30:
        return 0.88
    # Very sparse — short clip or near-silent segment
    return 0.65


# ---------------------------------------------------------------------------
# Public API: clean_whisperx
# ---------------------------------------------------------------------------

def clean_whisperx(json_path: Path) -> List[CleanedSegment]:
    """
    Parse a WhisperX JSON file and return a list of CleanedSegment objects.
    Speaker labels and word-level timing are preserved.
    Light cleaning only — WhisperX output is already fairly clean.
    """
    json_path = Path(json_path)
    data = json.loads(json_path.read_text(encoding="utf-8"))

    raw_segments  = data.get("segments", [])
    source_type   = "whisperx_asr"
    segments: List[CleanedSegment] = []

    for seg in raw_segments:
        text = seg.get("text", "").strip()
        if not text:
            continue

        # Light text cleanup
        text = _html_unescape(text)
        text = _normalize_unicode_punct(text)
        text = _strip_filler_words(text)
        text = _tidy_spaces(text)
        if len(text.split()) < 2:
            continue

        # Confidence from avg_logprob:
        #   logprob = 0.0  → score = 1.0   (perfect)
        #   logprob = -1.0 → score = 0.5
        #   logprob = -2.0 → score = 0.0   (very uncertain)
        avg_lp = seg.get("avg_logprob", -0.5)
        confidence = min(1.0, max(0.0, (avg_lp + 2.0) / 2.0))

        # Word-level timing (may be absent if alignment was skipped)
        words: List[CleanedWord] = []
        for w in seg.get("words", []):
            word_text = w.get("word", "").strip()
            if not word_text:
                continue
            words.append(CleanedWord(
                word=word_text,
                start=float(w.get("start") or seg.get("start", 0)),
                end=float(w.get("end") or seg.get("end", 0)),
                score=float(w.get("score", 0.0)),
                speaker=w.get("speaker"),
            ))

        # Speaker: prefer per-segment label, fallback to first word's label
        speaker = seg.get("speaker")
        if not speaker and words:
            speaker = words[0].speaker

        segments.append(CleanedSegment(
            start=float(seg.get("start", 0)),
            end=float(seg.get("end", 0)),
            text=text,
            speaker=speaker,
            words=words,
            confidence=confidence,
            source_type=source_type,
        ))

    log.info("clean_whisperx: %d segments from %s", len(segments), json_path.name)
    return segments


# ---------------------------------------------------------------------------
# Utility: file hash
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    """SHA-256 hash of a file for reproducibility / change detection."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Utility: segments → plain text (for word-count)
# ---------------------------------------------------------------------------

def segments_to_plain_text(segments: List[CleanedSegment]) -> str:
    return " ".join(s.text for s in segments)
