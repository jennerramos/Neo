"""
Source-chunk evidence provenance.

Every extracted claim must point at the exact chunk(s) whose words support it,
so a reader can open the transcript and check the record instead of trusting it.

The model proposes evidence; this module decides whether to believe it.  Nothing
here uses embeddings or similarity scores — a quote is either present in the
chunk the model named, character for character after conservative normalization,
or it is not evidence.

Three failure modes, three review reasons, no silent drops:

    invalid_evidence_chunk      the cited chunk_id is unknown, belongs to a
                                different meeting, or was not in the window the
                                model was shown
    evidence_quote_not_found    the chunk is real but does not contain the quote
    missing_verified_evidence   nothing the model offered survived validation

Two properties of this corpus shape the implementation:

1.  chunk_id is POSITIONAL — f"{video_id}_{idx:04d}" (data_packager.py:282).
    Re-chunking a meeting with different settings reassigns the same ID to
    different text, which would leave evidence rows pointing at the right ID and
    the wrong words.  Every stored reference therefore carries a hash of the
    chunk text it was verified against, so staleness is detectable on read
    rather than invisible.

2.  Chunks OVERLAP.  Each one begins with "[…] " plus the last few sentences of
    its predecessor (data_packager.py:256).  A quote near a boundary genuinely
    lives in two chunks and both would validate.  We prefer the chunk that owns
    the audio — the one where the quote falls outside the carried-in prefix —
    and deduplicate on the quote, not on (chunk, quote).
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Review reasons
# ---------------------------------------------------------------------------

REVIEW_REASON_NO_EVIDENCE    = "missing_verified_evidence"
REVIEW_REASON_QUOTE_NOT_FOUND = "evidence_quote_not_found"
REVIEW_REASON_INVALID_CHUNK  = "invalid_evidence_chunk"

# Marker prefix a chunk carries when its opening sentences are copied from the
# previous chunk (data_packager.py:256), and how many sentences that covers.
# Mirrors data_packager.OVERLAP_SENTENCES; imported rather than duplicated so a
# change to the chunker cannot silently desynchronize this.
OVERLAP_MARKER = "[…]"
try:
    from pipeline.data_packager import OVERLAP_SENTENCES
except ImportError:                                   # pragma: no cover
    OVERLAP_SENTENCES = 1

# Shortest quote we will accept.  Below this a "quote" matches everywhere and
# proves nothing — "the board" is not evidence.
MIN_QUOTE_CHARS = 12

# Width of the human-readable preview built around a verified quote.
PREVIEW_CHARS = 500


# ---------------------------------------------------------------------------
# Prompt contract
# ---------------------------------------------------------------------------
#
# Injected into every extraction prompt via .format(evidence_contract=...).
# Contains literal braces; passed as a format ARGUMENT (never re-formatted), so
# they need no escaping here.

EVIDENCE_CONTRACT = """\
EVIDENCE (required for every item):
The transcript below is divided into chunks.  Each chunk starts with a header:

  [[CHUNK_ID: <id>]]
  [[START_TIME: <seconds or null>]]
  [[END_TIME: <seconds or null>]]

Every item you return MUST carry an "evidence" array.  Each element:
{
  "chunk_id":    "the exact [[CHUNK_ID]] value the words came from",
  "exact_quote": "verbatim words copied from that chunk",
  "supports":    ["field names from the object above that this quote proves"],
  "start_timestamp": number or null,
  "end_timestamp":   number or null
}

RULES FOR EVIDENCE — these are checked mechanically and failures are flagged:
- exact_quote must be COPIED CHARACTER FOR CHARACTER from the chunk text.
  The transcript is raw speech-to-text: keep its typos, false starts, repeated
  words and missing punctuation exactly as written.  Do NOT clean up, correct,
  summarize, or re-punctuate the quote.  A tidied quote will fail verification.
- Quote at least 12 characters; a few words prove nothing.
- Use the chunk_id of the chunk the words are actually in, not a nearby one.
- One item may cite SEVERAL chunks.  Prefer this when the evidence is split:
  the motion in one chunk and its outcome in another, an amount in one chunk and
  its approval in another, a person's name in one chunk and their new title in
  another.  Add one evidence object per distinct supporting quote.
- Never invent a chunk_id, and never quote text that is not in the transcript."""


# ---------------------------------------------------------------------------
# Conservative normalization
# ---------------------------------------------------------------------------
#
# Deliberately narrow.  We absorb the ways a model transcribes the SAME words
# differently — smart quotes, unicode dashes, collapsed whitespace, case — and
# nothing else.  We do NOT stem, drop stopwords, or strip interior punctuation:
# those would let a paraphrase pass as a verbatim quote, which is the exact
# failure this module exists to prevent.

_SMART_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "‒": "-", "―": "-",
    " ": " ", "…": "...",
}
_WS_RE = re.compile(r"\s+")


def _norm_with_map(text: str) -> tuple[str, list[int]]:
    """
    Normalize while recording, for each normalized char, its index in `text`.

    Lets us report character offsets into the ORIGINAL chunk after matching on
    the normalized form, so the UI can highlight the real substring.

    Normalization is applied PER CHARACTER, never to the whole string.  Whole
    string NFKC silently changes length — "…" becomes "..." — which would shift
    every offset after it and hand back quotes that start mid-word.
    """
    out: list[str] = []
    idx: list[int] = []
    prev_space = True          # leading whitespace is dropped

    for i, ch in enumerate(text):
        folded = _SMART_MAP.get(ch) or unicodedata.normalize("NFKC", ch)

        if folded.isspace() and len(folded) == 1:
            if prev_space:
                continue
            out.append(" ")
            idx.append(i)
            prev_space = True
            continue

        # One source char may fold to several (ellipsis, ligatures); every
        # resulting char maps back to the single original index it came from.
        for sub in folded:
            out.append(sub.lower())
            idx.append(i)
        prev_space = False

    while out and out[-1] == " ":
        out.pop()
        idx.pop()

    return "".join(out), idx


def normalize(text: str) -> str:
    """
    Fold away transcription-neutral differences.  Never changes word content.

    Defined in terms of _norm_with_map so the string used for matching and the
    offsets used for slicing can never drift apart.
    """
    if not text:
        return ""
    return _norm_with_map(text)[0]


def chunk_fingerprint(chunk_text: str) -> str:
    """
    Stable hash of a chunk's normalized text.

    Stored alongside every evidence reference so that a later re-chunk — which
    reuses positional chunk_ids for different text — is detectable instead of
    silently serving the wrong quote.
    """
    return hashlib.sha256(normalize(chunk_text).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class VerifiedEvidence:
    """One model-proposed reference that survived validation."""
    chunk_id:        str
    quote:           str          # verbatim, as it appears in the chunk
    supports:        list[str]    # extracted field names this quote backs
    start_time:      Optional[float]
    end_time:        Optional[float]
    quote_start_char: int         # offset into the ORIGINAL chunk text
    quote_end_char:   int
    chunk_sha:       str
    in_overlap:      bool = False # quote sits in text carried in from the previous chunk


@dataclass
class EvidenceOutcome:
    """What validation concluded for one extracted item."""
    verified:       list[VerifiedEvidence] = field(default_factory=list)
    review_reasons: list[str]              = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when at least one reference verified."""
        return bool(self.verified)

    @property
    def needs_review(self) -> bool:
        """
        True when ANY reference failed, even if others verified.

        Partial failure still means the model cited something we could not find,
        so the item goes to a human.  Keying review off `ok` instead would let an
        item with two good quotes and one fabricated one auto-accept while still
        carrying a review_reason — flagged in the data, invisible in the queue.
        """
        return bool(self.review_reasons)

    @property
    def reason(self) -> Optional[str]:
        """Single reason for the row's review_reason column, worst-first."""
        for r in (REVIEW_REASON_NO_EVIDENCE,
                  REVIEW_REASON_QUOTE_NOT_FOUND,
                  REVIEW_REASON_INVALID_CHUNK):
            if r in self.review_reasons:
                return r
        return None


# ---------------------------------------------------------------------------
# Quote location
# ---------------------------------------------------------------------------

def locate_quote(quote: str, chunk_text: str) -> Optional[tuple[int, int]]:
    """
    Find `quote` inside `chunk_text`, returning (start, end) offsets into the
    ORIGINAL chunk text, or None.

    Matching is exact on the normalized forms of both sides.  No fuzzy fallback:
    a near-miss is reported as not-found so it reaches a human, rather than being
    accepted as proof.
    """
    if not quote or not chunk_text:
        return None

    n_quote = normalize(quote)
    if len(n_quote) < MIN_QUOTE_CHARS:
        return None

    n_chunk, idx_map = _norm_with_map(chunk_text)
    pos = n_chunk.find(n_quote)
    if pos < 0:
        return None

    start = idx_map[pos]
    end   = idx_map[pos + len(n_quote) - 1] + 1
    return start, end


def _overlap_prefix_len(chunk_text: str) -> int:
    """
    Length of the head of a chunk that was carried in from the previous chunk.

    data_packager.build_chunks writes f"[…] {overlap_tail}" as the first element
    of the chunk, where overlap_tail is the previous chunk's last
    OVERLAP_SENTENCES sentences joined with ". " and terminated with ".".  So
    the carried-in region ends after that many sentence terminators.
    """
    if not chunk_text.startswith(OVERLAP_MARKER):
        return 0

    pos = len(OVERLAP_MARKER)
    for _ in range(OVERLAP_SENTENCES):
        nxt = chunk_text.find(".", pos)
        if nxt < 0:
            return len(chunk_text)      # whole chunk is carry-in
        pos = nxt + 1
    return pos


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_item_evidence(
    raw_evidence: Any,
    *,
    window_chunks: dict[str, Any],
    known_field_names: Iterable[str],
    db_chunk_ids: Optional[set[str]] = None,
) -> EvidenceOutcome:
    """
    Validate the evidence array one extracted item returned.

    `window_chunks` maps chunk_id -> object with .text/.start_time/.end_time,
    and contains ONLY the chunks the model was actually shown for this item's
    window.  That single constraint enforces three of the required checks at
    once: the chunk exists, it belongs to this meeting, and it was in the window.

    `db_chunk_ids`, when given, additionally requires the chunk to be present in
    the chunks table, so persisting a foreign key can never fail mid-run.
    """
    outcome = EvidenceOutcome()

    if not isinstance(raw_evidence, list) or not raw_evidence:
        outcome.review_reasons.append(REVIEW_REASON_NO_EVIDENCE)
        return outcome

    valid_fields = set(known_field_names)
    seen_quotes: dict[str, VerifiedEvidence] = {}

    for ref in raw_evidence:
        if not isinstance(ref, dict):
            outcome.review_reasons.append(REVIEW_REASON_INVALID_CHUNK)
            continue

        chunk_id = (ref.get("chunk_id") or "").strip()
        quote    = (ref.get("exact_quote") or "").strip()

        chunk = window_chunks.get(chunk_id)
        if chunk is None or (db_chunk_ids is not None and chunk_id not in db_chunk_ids):
            outcome.review_reasons.append(REVIEW_REASON_INVALID_CHUNK)
            continue

        span = locate_quote(quote, chunk.text)
        if span is None:
            outcome.review_reasons.append(REVIEW_REASON_QUOTE_NOT_FOUND)
            continue

        supports = [
            f for f in (ref.get("supports") or [])
            if isinstance(f, str) and f in valid_fields
        ]

        start, end = span
        verified = VerifiedEvidence(
            chunk_id        = chunk_id,
            quote           = chunk.text[start:end],
            supports        = supports,
            start_time      = getattr(chunk, "start_time", None),
            end_time        = getattr(chunk, "end_time", None),
            quote_start_char= start,
            quote_end_char  = end,
            chunk_sha       = chunk_fingerprint(chunk.text),
            in_overlap      = start < _overlap_prefix_len(chunk.text),
        )

        # ── Dedup on the QUOTE, not on (chunk, quote) ──────────────────────
        # Overlapping chunks mean one quote legitimately validates against two
        # chunk_ids.  Keeping both would double-count a single piece of
        # evidence, so collapse them and keep the chunk that owns the audio
        # (the one where the quote is not carried-in text), merging `supports`.
        key = normalize(quote)
        prior = seen_quotes.get(key)
        if prior is None:
            seen_quotes[key] = verified
            continue

        merged = sorted(set(prior.supports) | set(verified.supports))
        winner = verified if (prior.in_overlap and not verified.in_overlap) else prior
        winner.supports = merged
        seen_quotes[key] = winner

    outcome.verified = list(seen_quotes.values())
    if not outcome.verified:
        outcome.review_reasons.append(REVIEW_REASON_NO_EVIDENCE)

    return outcome


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def build_preview(
    chunk_text: str,
    quote_start: int,
    quote_end: int,
    width: int = PREVIEW_CHARS,
) -> tuple[str, int, int]:
    """
    Build a ~`width`-char excerpt CENTRED ON THE VERIFIED QUOTE.

    Returns (preview_text, quote_offset_in_preview, quote_end_in_preview) so the
    UI can highlight the exact words that were verified.  The preview stays plain
    text with no markup injected, which keeps it drop-in compatible with the
    existing evidence_text consumers (api/db/queries/insights.py:321).
    """
    quote_len = quote_end - quote_start
    if quote_len >= width:
        # Quote alone fills the preview — never truncate away the thing we verified.
        return chunk_text[quote_start:quote_end], 0, quote_len

    slack = width - quote_len
    lead  = slack // 2
    start = max(0, quote_start - lead)
    end   = min(len(chunk_text), start + width)
    start = max(0, end - width)          # re-expand left if we hit the tail

    preview = chunk_text[start:end]
    if start > 0:
        preview = "… " + preview
        offset  = quote_start - start + 2
    else:
        offset  = quote_start - start

    return preview, offset, offset + quote_len
