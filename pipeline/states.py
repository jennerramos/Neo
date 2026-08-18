"""
Single source of truth for the pipeline state machine.

Before this module the eligibility lists for each phase were copy-pasted as
literal lists at three call sites (extractor / quality_gate / indexer), the
allowed-statuses set lived in ``scripts/reset_status.py``, and the recovery
mapping lived in ``scripts/recover_failed.py``. Adding or renaming a status
required synchronized edits across files with no compile-time check.

Now every phase imports its eligible-input tuple from here. Adding a new
status (or e.g. extending the recovery list) becomes a one-file change.

Conventions:
- Status names are plain strings, matching the ``meetings.status`` text
  column used everywhere. Not an enum — sticking with strings keeps the
  literal grep-ability the rest of the codebase relies on.
- Eligibility tuples come in two shapes per phase:
    ``INPUTS[phase]``           — default: only fresh, never-processed meetings
    ``RECHECK_INPUTS[phase]``   — when the phase's --recheck/--reextract/--reindex
                                  flag is set, these are appended.
- Failed-state recovery is described by ``RECOVERY_TARGETS`` (used by
  ``scripts/recover_failed.py``).

See refactor_candidates.md #3.
"""
from __future__ import annotations
from typing import Final


# ---------------------------------------------------------------------------
# Status constants — every value the ``meetings.status`` column can take.
# ---------------------------------------------------------------------------

# Forward-progress states (success path: discovered → ... → indexed)
DISCOVERED:    Final = "discovered"
CAPTIONED:     Final = "captioned"
NEEDS_ASR:     Final = "needs_asr"
DOWNLOADING:   Final = "downloading"      # asr_processor in-flight
TRANSCRIBING:  Final = "transcribing"     # asr_processor in-flight
TRANSCRIBED:   Final = "transcribed"
PROCESSED:     Final = "processed"
EXTRACTED:     Final = "extracted"
APPROVED:      Final = "approved"
INDEXED:       Final = "indexed"

# Re-runnable terminal state — quality_gate can pick these up via --recheck.
REJECTED:      Final = "rejected"

# Deliberately excluded from the pilot corpus for being older than
# config.MEETING_YEAR_CUTOFF. Terminal and intentional: it appears in no
# INPUTS or RECHECK_INPUTS tuple, so no phase — including a --reprocess or
# --recheck run — will pick these up again. Distinct from REJECTED, which
# means "we tried and the content was unusable"; these were never in scope.
ARCHIVED_OLD:  Final = "archived_old"

# Terminal-failure states (no automatic recovery — see scripts/recover_failed.py).
ASR_FAILED:        Final = "asr_failed"
PROCESSING_FAILED: Final = "processing_failed"
FAILED:            Final = "failed"        # generic — any phase

# Truly terminal — platform has no captions AND no audio download path, so
# neither caption retry nor ASR can help. Not in RECOVERY_TARGETS by design;
# included in TERMINAL_FAILURES so dashboards can group it with the others.
CAPTION_UNAVAILABLE: Final = "caption_unavailable"

# Legacy (kept for backwards compatibility with old rows).
PENDING:       Final = "pending"


ALL_STATUSES: Final[frozenset[str]] = frozenset({
    DISCOVERED, CAPTIONED, NEEDS_ASR, DOWNLOADING, TRANSCRIBING, TRANSCRIBED,
    PROCESSED, EXTRACTED, APPROVED, INDEXED,
    REJECTED, ASR_FAILED, PROCESSING_FAILED, FAILED, PENDING,
    CAPTION_UNAVAILABLE, ARCHIVED_OLD,
})

TERMINAL_FAILURES: Final[frozenset[str]] = frozenset({
    ASR_FAILED, PROCESSING_FAILED, REJECTED, CAPTION_UNAVAILABLE,
})


# ---------------------------------------------------------------------------
# Phase eligibility — what each pipeline phase will pick up by default and
# what extra statuses get included when its rerun flag is set.
#
# The diamond between EXTRACTED/APPROVED is intentional: quality_gate and the
# extractor are order-independent. Both orderings are valid:
#   packager → quality_gate → extractor → indexer  (status='extracted' at end)
#   packager → extractor    → quality_gate → indexer  (status='approved' at end)
# The indexer accepts either.
# ---------------------------------------------------------------------------

INPUTS: Final[dict[str, tuple[str, ...]]] = {
    "caption_downloader":   (DISCOVERED,),
    "asr_processor":        (NEEDS_ASR,),
    "data_packager":        (TRANSCRIBED, CAPTIONED),
    "quality_gate":         (PROCESSED, EXTRACTED),
    "extractor":            (PROCESSED, APPROVED),
    "indexer":              (APPROVED, EXTRACTED),
    "initiative_extractor": (EXTRACTED, INDEXED),
}

# Extra statuses included when the phase's rerun flag (--recheck / --reprocess /
# --reextract / --reindex) is set. Empty tuple means "no rerun mode".
RECHECK_INPUTS: Final[dict[str, tuple[str, ...]]] = {
    "caption_downloader":   (),
    "asr_processor":        (),
    "data_packager":        (PROCESSED, REJECTED),
    "quality_gate":         (REJECTED,),
    "extractor":            (EXTRACTED, INDEXED),
    "indexer":              (INDEXED,),
    "initiative_extractor": (),
}


def eligible_inputs(phase: str, *, rerun: bool = False) -> tuple[str, ...]:
    """Return the tuple of statuses ``phase`` will pick up.

    Pass ``rerun=True`` when the phase's rerun flag is active (e.g.
    ``quality_gate --recheck`` or ``extractor --reextract``).
    """
    base  = INPUTS[phase]
    extra = RECHECK_INPUTS[phase] if rerun else ()
    return base + extra


# ---------------------------------------------------------------------------
# Failed-state recovery — used by scripts/recover_failed.py.
#
# ``to`` is the status to flip a failed meeting back to. ``by_source_type``
# allows routing per source_type (data_packager accepts both 'captioned' and
# 'transcribed', so we route by what the meeting's source_type was).
# ---------------------------------------------------------------------------

RECOVERY_TARGETS: Final[dict[str, dict]] = {
    ASR_FAILED: {
        "to": NEEDS_ASR,
        "by_source_type": None,
    },
    PROCESSING_FAILED: {
        "to": TRANSCRIBED,
        "by_source_type": {
            "whisperx_asr":    TRANSCRIBED,
            "transcribed":     TRANSCRIBED,
            "youtube_caption": CAPTIONED,
        },
    },
    REJECTED: {
        "to": PROCESSED,
        "by_source_type": None,
    },
}
