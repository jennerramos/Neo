"""
Source-chunk evidence provenance.

Extraction evidence used to be a blind slice: the first 500 characters of the
highest-scoring chunk in the window, chosen before the model said anything and
often not containing the claim at all.  Every one of the 2,139 evidence rows in
the corpus was exactly 500 characters, which is what that looks like from the
outside.

These tests pin the replacement: a claim is linked to the chunk whose words
actually support it, verified by locating the quotation character-for-character,
with no similarity scoring anywhere in the path.

Lettered tests map to the acceptance list in the task.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from pipeline.evidence import (
    MIN_QUOTE_CHARS,
    REVIEW_REASON_INVALID_CHUNK,
    REVIEW_REASON_NO_EVIDENCE,
    REVIEW_REASON_QUOTE_NOT_FOUND,
    build_preview,
    chunk_fingerprint,
    locate_quote,
    normalize,
    validate_item_evidence,
)


# ---------------------------------------------------------------------------
# Fixtures — chunks shaped like the real ones (data_packager.build_chunks)
# ---------------------------------------------------------------------------

@dataclass
class FakeChunk:
    chunk_id:   str
    text:       str
    start_time: Optional[float] = None
    end_time:   Optional[float] = None
    score:      float = 0.0


VID = "j4wSOB03AKY"

# The motion is in chunk 38; the outcome is two chunks later.  The high-scoring
# chunk (39) is neither of them — this is the shape the old snippet got wrong.
CHUNK_38 = FakeChunk(
    f"{VID}_0038",
    "So the recommendation is that the board approve the settlement and release "
    "agreement with Elmtech Glass and Sage Electrochromic Incorporated. The "
    "agreement resolves a dispute relating to defective glass installed at the "
    "district's student center project.",
    start_time=8383.658, end_time=8480.0, score=3.0,
)
CHUNK_39 = FakeChunk(
    f"{VID}_0039",
    "Any discussion on that item? Seeing none, we will proceed to the vote on "
    "the item as presented by staff this evening.",
    start_time=8480.0, end_time=8520.0, score=9.9,   # highest score in window
)
CHUNK_40 = FakeChunk(
    f"{VID}_0040",
    "All those in favor please say aye. Aye. The motion carries six to nothing "
    "and the settlement is approved as presented.",
    start_time=8520.0, end_time=8581.856, score=4.0,
)

WINDOW = {c.chunk_id: c for c in (CHUNK_38, CHUNK_39, CHUNK_40)}
VOTE_FIELDS = frozenset({"motion_text", "vote_result_text", "passed", "amount",
                         "action_type", "person_name", "position"})


def _ref(chunk_id, quote, supports=None):
    return {"chunk_id": chunk_id, "exact_quote": quote,
            "supports": supports or [], "start_timestamp": None,
            "end_timestamp": None}


# ---------------------------------------------------------------------------
# A. Evidence in a chunk other than the highest-scoring one
# ---------------------------------------------------------------------------

def test_A_evidence_links_to_a_lower_scored_chunk():
    """The old snippet always came from CHUNK_39; the claim lives in CHUNK_38."""
    assert max(WINDOW.values(), key=lambda c: c.score).chunk_id == CHUNK_39.chunk_id

    out = validate_item_evidence(
        [_ref(CHUNK_38.chunk_id,
              "the board approve the settlement and release agreement",
              ["motion_text"])],
        window_chunks=WINDOW, known_field_names=VOTE_FIELDS,
    )

    assert out.ok
    assert [e.chunk_id for e in out.verified] == [CHUNK_38.chunk_id]
    assert out.reason is None


# ---------------------------------------------------------------------------
# B. Evidence past the first 500 characters
# ---------------------------------------------------------------------------

def test_B_evidence_beyond_the_old_500_char_window():
    long_chunk = FakeChunk(
        f"{VID}_0050",
        ("Filler discussion of an unrelated agenda item. " * 20)   # >500 chars
        + "The board approved a total of one million five hundred thousand dollars.",
        start_time=1.0, end_time=2.0,
    )
    quote = "approved a total of one million five hundred thousand dollars"
    assert long_chunk.text.find(quote) > 500          # outside the old snippet

    out = validate_item_evidence(
        [_ref(long_chunk.chunk_id, quote, ["amount"])],
        window_chunks={long_chunk.chunk_id: long_chunk},
        known_field_names=VOTE_FIELDS,
    )

    assert out.ok
    assert out.verified[0].quote_start_char > 500


# ---------------------------------------------------------------------------
# C. One vote, motion and outcome in separate chunks
# ---------------------------------------------------------------------------

def test_C_one_vote_links_motion_and_outcome_chunks():
    out = validate_item_evidence(
        [
            _ref(CHUNK_38.chunk_id,
                 "the board approve the settlement and release agreement",
                 ["motion_text"]),
            _ref(CHUNK_40.chunk_id,
                 "The motion carries six to nothing",
                 ["vote_result_text", "passed"]),
        ],
        window_chunks=WINDOW, known_field_names=VOTE_FIELDS,
    )

    assert len(out.verified) == 2
    by_chunk = {e.chunk_id: e for e in out.verified}
    assert by_chunk[CHUNK_38.chunk_id].supports == ["motion_text"]
    assert set(by_chunk[CHUNK_40.chunk_id].supports) == {"vote_result_text", "passed"}
    # Distinct timestamps — a reader can seek to either moment.
    assert by_chunk[CHUNK_38.chunk_id].start_time != by_chunk[CHUNK_40.chunk_id].start_time


# ---------------------------------------------------------------------------
# D. One financial item, amount and approval in separate chunks
# ---------------------------------------------------------------------------

def test_D_financial_item_links_amount_and_approval():
    amount_chunk = FakeChunk(
        f"{VID}_0060",
        "The contract with Roadrunner Construction totals one million five "
        "hundred thousand dollars for the fiscal year.",
        start_time=100.0, end_time=160.0,
    )
    approval_chunk = FakeChunk(
        f"{VID}_0061",
        "The board voted to approve that contract as part of the consent agenda.",
        start_time=160.0, end_time=200.0,
    )
    window = {c.chunk_id: c for c in (amount_chunk, approval_chunk)}

    out = validate_item_evidence(
        [
            _ref(amount_chunk.chunk_id,
                 "totals one million five hundred thousand dollars", ["amount"]),
            _ref(approval_chunk.chunk_id,
                 "The board voted to approve that contract", ["action_type"]),
        ],
        window_chunks=window, known_field_names=VOTE_FIELDS,
    )

    assert len(out.verified) == 2
    assert {f for e in out.verified for f in e.supports} == {"amount", "action_type"}


# ---------------------------------------------------------------------------
# E. Invalid chunk IDs are rejected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_id", [
    "totally_made_up_0001",
    f"{VID}_9999",          # right meeting, chunk not in this window
    "",
    "None",
])
def test_E_invalid_chunk_ids_are_rejected(bad_id):
    out = validate_item_evidence(
        [_ref(bad_id, "the board approve the settlement and release agreement")],
        window_chunks=WINDOW, known_field_names=VOTE_FIELDS,
    )

    assert not out.ok
    assert REVIEW_REASON_INVALID_CHUNK in out.review_reasons
    assert out.reason == REVIEW_REASON_NO_EVIDENCE   # nothing survived at all


def test_E2_chunk_missing_from_the_chunks_table_is_rejected():
    """Guards the FK: we never try to insert evidence for an unknown chunk."""
    out = validate_item_evidence(
        [_ref(CHUNK_38.chunk_id, "the board approve the settlement and release agreement")],
        window_chunks=WINDOW, known_field_names=VOTE_FIELDS,
        db_chunk_ids={CHUNK_39.chunk_id},        # 38 not indexed
    )
    assert not out.ok
    assert REVIEW_REASON_INVALID_CHUNK in out.review_reasons


# ---------------------------------------------------------------------------
# F. A quote absent from its claimed chunk triggers review
# ---------------------------------------------------------------------------

def test_F_quote_not_in_the_claimed_chunk():
    out = validate_item_evidence(
        [_ref(CHUNK_38.chunk_id, "the board unanimously rejected the proposal")],
        window_chunks=WINDOW, known_field_names=VOTE_FIELDS,
    )

    assert not out.ok
    assert REVIEW_REASON_QUOTE_NOT_FOUND in out.review_reasons


def test_F2_quote_in_the_window_but_the_wrong_chunk_is_cited():
    """Right words, wrong chunk — still not verified against what was cited."""
    out = validate_item_evidence(
        [_ref(CHUNK_39.chunk_id, "The motion carries six to nothing")],
        window_chunks=WINDOW, known_field_names=VOTE_FIELDS,
    )
    assert not out.ok
    assert REVIEW_REASON_QUOTE_NOT_FOUND in out.review_reasons


def test_F3_a_paraphrase_is_not_evidence():
    """The whole point: near-enough must fail, or provenance means nothing."""
    out = validate_item_evidence(
        [_ref(CHUNK_40.chunk_id, "the motion passed six to zero")],   # paraphrase
        window_chunks=WINDOW, known_field_names=VOTE_FIELDS,
    )
    assert not out.ok
    assert REVIEW_REASON_QUOTE_NOT_FOUND in out.review_reasons


def test_F4_trivially_short_quotes_are_rejected():
    out = validate_item_evidence(
        [_ref(CHUNK_40.chunk_id, "Aye.")],
        window_chunks=WINDOW, known_field_names=VOTE_FIELDS,
    )
    assert not out.ok
    assert len("Aye.") < MIN_QUOTE_CHARS


# ---------------------------------------------------------------------------
# G. Evidence from another meeting cannot be linked
# ---------------------------------------------------------------------------

def test_G_evidence_from_another_meeting_is_rejected():
    other = FakeChunk("SOME_OTHER_VIDEO_0012",
                      "The board approve the settlement and release agreement "
                      "with Elmtech Glass and Sage Electrochromic Incorporated.")
    # Same words, different meeting — and NOT in this item's window.
    out = validate_item_evidence(
        [_ref(other.chunk_id, "the board approve the settlement and release agreement")],
        window_chunks=WINDOW, known_field_names=VOTE_FIELDS,
    )

    assert not out.ok
    assert REVIEW_REASON_INVALID_CHUNK in out.review_reasons


# ---------------------------------------------------------------------------
# H. Duplicate references are consolidated
# ---------------------------------------------------------------------------

def test_H_exact_duplicate_references_collapse():
    quote = "the board approve the settlement and release agreement"
    out = validate_item_evidence(
        [_ref(CHUNK_38.chunk_id, quote, ["motion_text"]),
         _ref(CHUNK_38.chunk_id, quote, ["motion_text"])],
        window_chunks=WINDOW, known_field_names=VOTE_FIELDS,
    )
    assert len(out.verified) == 1


def test_H2_same_quote_in_two_overlapping_chunks_collapses_once():
    """
    Chunks overlap by design: each carries "[…] " plus its predecessor's last
    sentences (data_packager.py:256).  One quote therefore validates against two
    chunk_ids — it is still ONE piece of evidence, and we keep the chunk that
    owns the audio rather than the one that merely echoes it.
    """
    quote = "The motion carries six to nothing"
    echo = FakeChunk(
        f"{VID}_0041",
        f"[…] {quote} and the settlement is approved as presented. Moving on to "
        "the next agenda item.",
        start_time=8581.0, end_time=8700.0,
    )
    window = {CHUNK_40.chunk_id: CHUNK_40, echo.chunk_id: echo}

    out = validate_item_evidence(
        [_ref(echo.chunk_id, quote, ["passed"]),
         _ref(CHUNK_40.chunk_id, quote, ["vote_result_text"])],
        window_chunks=window, known_field_names=VOTE_FIELDS,
    )

    assert len(out.verified) == 1
    kept = out.verified[0]
    assert kept.chunk_id == CHUNK_40.chunk_id          # owns the audio
    assert kept.in_overlap is False
    assert set(kept.supports) == {"passed", "vote_result_text"}   # merged, not lost


def test_H3_different_quotes_from_one_chunk_are_kept_separately():
    out = validate_item_evidence(
        [_ref(CHUNK_38.chunk_id, "the board approve the settlement and release agreement"),
         _ref(CHUNK_38.chunk_id, "defective glass installed at the district's student center")],
        window_chunks=WINDOW, known_field_names=VOTE_FIELDS,
    )
    assert len(out.verified) == 2


# ---------------------------------------------------------------------------
# I. The preview contains the verified quotation
# ---------------------------------------------------------------------------

def test_I_preview_is_built_around_the_quote():
    long_chunk = ("Unrelated procedural chatter. " * 40
                  + "The board approved one million five hundred thousand dollars. "
                  + "More unrelated chatter afterwards. " * 40)
    quote = "approved one million five hundred thousand dollars"
    span = locate_quote(quote, long_chunk)
    assert span is not None

    preview, q_start, q_end = build_preview(long_chunk, *span)

    assert quote in preview                       # the point of the exercise
    assert preview[q_start:q_end] == quote        # offsets locate it for the UI
    assert len(preview) <= 502                    # 500 + the "… " prefix


def test_I2_preview_is_not_the_old_blind_head_slice():
    long_chunk = "A" * 600 + " the board approved the contract as presented today"
    span = locate_quote("the board approved the contract as presented", long_chunk)
    preview, _, _ = build_preview(long_chunk, *span)

    assert preview != long_chunk[:500]
    assert "the board approved the contract as presented" in preview


def test_I3_a_quote_longer_than_the_preview_is_never_truncated():
    huge = "x" * 50 + "y" * 600 + "z" * 50
    quote = "y" * 600
    span = locate_quote(quote, huge)
    preview, q_start, q_end = build_preview(huge, *span)

    assert preview[q_start:q_end] == quote


# ---------------------------------------------------------------------------
# J. Existing records remain readable
# ---------------------------------------------------------------------------

def test_J_items_with_no_evidence_array_are_preserved_and_flagged():
    """Pre-0006 rows: no evidence, but the record is not thrown away."""
    for missing in (None, [], "not a list", {}):
        out = validate_item_evidence(
            missing, window_chunks=WINDOW, known_field_names=VOTE_FIELDS
        )
        assert not out.ok
        assert out.reason == REVIEW_REASON_NO_EVIDENCE


def test_J2_evidence_columns_are_additive_not_replacing():
    """
    evidence_text and chunk_ids still exist on every extraction model, so
    api/db/queries/insights.py and any other reader keeps working unchanged.
    """
    from database.models import FinancialItem, Initiative, PersonnelAction, Vote

    for model in (Vote, FinancialItem, PersonnelAction, Initiative):
        assert hasattr(model, "evidence_text")
        assert hasattr(model, "chunk_ids")
        assert hasattr(model, "evidence")        # new relationship
        assert hasattr(model, "review_reason")


def test_J3_chunk_fingerprint_detects_a_rechunk():
    """
    chunk_id is positional, so re-chunking can point the same ID at different
    text.  The stored hash is what makes that visible instead of silent.
    """
    original = chunk_fingerprint(CHUNK_38.text)
    assert chunk_fingerprint(CHUNK_38.text) == original          # stable
    assert chunk_fingerprint("  " + CHUNK_38.text.upper()) == original  # normalization-insensitive
    assert chunk_fingerprint(CHUNK_40.text) != original          # content-sensitive


# ---------------------------------------------------------------------------
# K. The missing-person-name policy still works
# ---------------------------------------------------------------------------

def test_K_unnamed_personnel_policy_survives_this_change():
    from pipeline.extractor import (
        AUTO_REVIEW_THRESHOLD,
        REVIEW_REASON_MISSING_NAME,
        _apply_unnamed_personnel_policy,
    )

    conf, needs_review, reason = _apply_unnamed_personnel_policy(None, 1.0, False)
    assert conf < AUTO_REVIEW_THRESHOLD
    assert needs_review is True
    assert reason == REVIEW_REASON_MISSING_NAME


def test_K2_missing_name_outranks_an_evidence_reason():
    """One review_reason column; the unidentifiable person is the worse problem."""
    from pipeline.extractor import REVIEW_REASON_MISSING_NAME, _merge_review_reason

    assert _merge_review_reason(REVIEW_REASON_MISSING_NAME,
                                REVIEW_REASON_NO_EVIDENCE) == REVIEW_REASON_MISSING_NAME
    assert _merge_review_reason(None, REVIEW_REASON_NO_EVIDENCE) == REVIEW_REASON_NO_EVIDENCE
    assert _merge_review_reason(None, None) is None


# ---------------------------------------------------------------------------
# L. Earlier personnel/vote fixes are not regressed
# ---------------------------------------------------------------------------

def test_L_name_verification_still_reads_the_whole_window():
    """
    The previous patch pointed _name_in_evidence at window.text instead of the
    500-char snippet.  That must stay: a name late in the window is real.
    """
    from pipeline.extractor import _name_in_evidence

    window_text = "filler. " * 200 + "the appointment of Ginetta Paige as Director"
    assert window_text.find("Ginetta Paige") > 500
    assert _name_in_evidence("Ginetta Paige", window_text) is True
    assert _name_in_evidence("Ginetta Paige", window_text[:500]) is False


def test_L2_position_is_not_amputated_when_a_name_is_given():
    """'Director of Student Life' must not become 'Director of Student'."""
    from pipeline.extractor import _split_name_from_position

    cleaned, extracted = _split_name_from_position("Director of Student Life")
    # The helper still splits when asked...
    assert extracted == "Life"
    # ...but extract_personnel only calls it when person_name is absent, so a
    # model-supplied name leaves the title intact.  Guard the call condition:
    import inspect
    from pipeline import extractor
    src = inspect.getsource(extractor.extract_personnel)
    assert "if not person_name:" in src
    assert "split_position, extracted_name = _split_name_from_position" in src


def test_L3_window_text_carries_stable_chunk_ids_not_positions():
    """The model must cite IDs that survive a window boundary change."""
    from pipeline.candidate_finder import CandidateWindow, ChunkRecord, ExtractionTarget

    recs = [
        ChunkRecord(chunk_id=f"{VID}_0038", chunk_index=38, text="alpha text",
                    start_time=1.0, end_time=2.0, token_count=2,
                    quality_score=0.9, source_type="vtt", score=3.0),
        ChunkRecord(chunk_id=f"{VID}_0039", chunk_index=39, text="beta text",
                    start_time=2.0, end_time=3.0, token_count=2,
                    quality_score=0.9, source_type="vtt", score=9.0),
    ]
    win = CandidateWindow(chunks=recs, target=ExtractionTarget.VOTES,
                          peak_score=9.0, window_score=12.0)

    assert f"[[CHUNK_ID: {VID}_0038]]" in win.text
    assert "[[START_TIME: 1.0]]" in win.text
    assert set(win.chunks_by_id) == {f"{VID}_0038", f"{VID}_0039"}


# ---------------------------------------------------------------------------
# Normalization is narrow on purpose
# ---------------------------------------------------------------------------

def test_normalization_folds_only_transcription_noise():
    assert normalize("The  board\napproved") == normalize("the board approved")
    assert normalize("don’t") == normalize("don't")           # smart quote
    assert normalize("2024–25") == normalize("2024-25")       # en dash
    # ...but never changes the words themselves
    assert normalize("approved the contract") != normalize("approved a contract")


@pytest.mark.parametrize("chunk_text,quote", [
    # The ellipsis is the trap: NFKC expands "…" to "...", so normalizing the
    # whole string at once shifts every later offset by +2 and returns quotes
    # that start mid-word.  Every real chunk after the first carries this marker.
    ("Hello. […] Next we have the introduction of employees.",
     "Next we have the introduction"),
    ("[…] Looks like we have one new hire, Melissa Lopez Castro.",
     "Looks like we have one new hire"),
    ("a" * 300 + " […] the board approved the settlement agreement today",
     "the board approved the settlement agreement"),
    ("The  board\napproved   the contract today", "board approved the contract"),
    ("He said “don’t do it now” and left the room", "don't do it now"),
    ("Budget for 2024–25 was approved by the board", "2024-25 was approved"),
])
def test_located_span_slices_the_original_text_exactly(chunk_text, quote):
    """Offsets must index the ORIGINAL chunk, not a normalized copy of it."""
    span = locate_quote(quote, chunk_text)
    assert span is not None
    sliced = chunk_text[span[0]:span[1]]
    assert normalize(sliced) == normalize(quote)
    # And it must not start mid-word: the character before the span is either
    # nothing or a non-alphanumeric boundary (space, quote, newline).
    before = chunk_text[span[0] - 1:span[0]] if span[0] else ""
    assert not before.isalnum()


def test_normalize_and_offset_map_cannot_diverge():
    """normalize() is defined via the mapping function — same string, always."""
    from pipeline.evidence import _norm_with_map

    for s in ("[…] Next up", "The  board\n\napproved", "don’t — really",
              "2024–25", "café", ""):
        assert normalize(s) == _norm_with_map(s)[0]


def test_partial_evidence_failure_still_requires_review():
    """
    Two good quotes and one that cannot be found is NOT a clean item.

    Regression: review was keyed off `ok` (did anything verify?), so an item
    with one unlocatable quote alongside valid ones kept needs_review=False
    while carrying a review_reason — flagged in the data, invisible in the
    review queue.  Found on the Houston run, on 2 financial items.
    """
    out = validate_item_evidence(
        [_ref(CHUNK_38.chunk_id,
              "the board approve the settlement and release agreement", ["motion_text"]),
         _ref(CHUNK_40.chunk_id, "a sentence that was never spoken here", ["passed"])],
        window_chunks=WINDOW, known_field_names=VOTE_FIELDS,
    )

    assert out.ok is True                 # something did verify
    assert len(out.verified) == 1
    assert out.needs_review is True       # ...but a human still has to look
    assert out.reason == REVIEW_REASON_QUOTE_NOT_FOUND


def test_fully_verified_item_does_not_require_review():
    out = validate_item_evidence(
        [_ref(CHUNK_38.chunk_id,
              "the board approve the settlement and release agreement", ["motion_text"])],
        window_chunks=WINDOW, known_field_names=VOTE_FIELDS,
    )
    assert out.ok is True
    assert out.needs_review is False
    assert out.reason is None


def test_supports_is_filtered_to_real_field_names():
    out = validate_item_evidence(
        [_ref(CHUNK_38.chunk_id,
              "the board approve the settlement and release agreement",
              ["motion_text", "invented_field", 42, None])],
        window_chunks=WINDOW, known_field_names=VOTE_FIELDS,
    )
    assert out.verified[0].supports == ["motion_text"]
