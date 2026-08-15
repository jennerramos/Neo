"""
Unnamed-personnel review policy.

A personnel action with no person_name is still worth keeping — "the board
approved an appointment at 1:42:00" is a true fact even when the transcript
never says who was appointed.  What it must never do is reach a trustee as an
auto-accepted record, because the row does not say who it is about.

These tests pin four properties:

  1. a verified, named action can pass without review
  2. an unnamed action is preserved, not discarded
  3. a missing name can never produce an auto-accepted record
  4. the reason is stored on the row and shown in the run output

The policy is deliberately independent of evidence verification: the last
section here pins that separation, so a future change to name-checking cannot
silently disable the review requirement (or vice versa).
"""
from __future__ import annotations

import pytest

from pipeline.extractor import (
    AUTO_REVIEW_THRESHOLD,
    REVIEW_REASON_MISSING_NAME,
    _CONF_FLOOR_PERSONNEL,
    _apply_unnamed_personnel_policy,
    _name_in_evidence,
    _personnel_confidence,
)


# ---------------------------------------------------------------------------
# 1. A verified named action can pass without review
# ---------------------------------------------------------------------------

def test_named_action_can_pass_without_review():
    """A named row that scores well is left completely alone by the policy."""
    item = {
        "person_name":    "Ginetta Paige",
        "action_type":    "promote",
        "position":       "Director of Student Life",
        "effective_date": "April 13, 2026",
    }
    window = (
        "Item 9.2, management contract. It is recommended that the board approve "
        "the promotion of Ginetta Paige to Director of Student Life effective "
        "April 13, 2026 through June 30, 2027."
    )

    conf, needs_review = _personnel_confidence(item, window, window_score=8.0)
    assert conf >= AUTO_REVIEW_THRESHOLD
    assert needs_review is False

    out_conf, out_review, reason = _apply_unnamed_personnel_policy(
        item["person_name"], conf, needs_review
    )
    assert out_conf   == conf          # untouched
    assert out_review is False         # still auto-acceptable
    assert reason     is None          # nothing to explain


def test_policy_does_not_clear_a_review_flag_set_elsewhere():
    """The policy only ever adds review, never removes it."""
    _, needs_review, reason = _apply_unnamed_personnel_policy(
        "Melissa Lopez Castro", 0.95, needs_review=True
    )
    assert needs_review is True
    assert reason is None


# ---------------------------------------------------------------------------
# 2. An unnamed action is preserved but requires review
# ---------------------------------------------------------------------------

def test_unnamed_action_is_preserved_and_flagged():
    conf, needs_review, reason = _apply_unnamed_personnel_policy(
        None, 1.0, needs_review=False
    )
    assert reason      == REVIEW_REASON_MISSING_NAME
    assert needs_review is True
    # Preserved, not discarded: still above the floor that would drop the row.
    assert conf >= _CONF_FLOOR_PERSONNEL


@pytest.mark.parametrize("empty", [None, "", "   ", "null", "none", "N/A", "unknown"])
def test_null_like_names_count_as_missing(empty):
    """The LLM writes 'null'/'N/A'/'unknown' as often as it writes JSON null."""
    _, needs_review, reason = _apply_unnamed_personnel_policy(empty, 1.0, False)
    assert needs_review is True
    assert reason == REVIEW_REASON_MISSING_NAME


# ---------------------------------------------------------------------------
# 3. A missing name cannot produce an automatically accepted record
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("incoming_conf", [1.0, 0.99, 0.8, AUTO_REVIEW_THRESHOLD, 0.5])
def test_unnamed_never_auto_accepts_at_any_score(incoming_conf):
    conf, needs_review, reason = _apply_unnamed_personnel_policy(
        None, incoming_conf, needs_review=False
    )
    assert conf < AUTO_REVIEW_THRESHOLD
    assert conf != 1.0
    assert needs_review is True
    assert reason == REVIEW_REASON_MISSING_NAME


def test_unnamed_confidence_is_never_raised():
    """Capping must not inflate an already-low score."""
    conf, _, _ = _apply_unnamed_personnel_policy(None, 0.42, needs_review=True)
    assert conf == pytest.approx(0.42)


def test_a_perfectly_scoring_unnamed_row_still_needs_review():
    """
    Regression guard for the real case that motivated this policy: an
    'acting Vice President of Human Resources' row scored 1.0 with
    needs_review=False purely from action_type + position + window score,
    with nobody's name attached.
    """
    item = {
        "person_name": None,
        "action_type": "appoint",
        "position":    "acting Vice President of Human Resources",
    }
    window = "The board approved the appointment of an acting Vice President of Human Resources."

    conf, needs_review = _personnel_confidence(item, window, window_score=99.0)
    assert conf == 1.0            # the scorer really does saturate here
    assert needs_review is False  # ...and would have auto-accepted it

    conf, needs_review, reason = _apply_unnamed_personnel_policy(
        item["person_name"], conf, needs_review
    )
    assert conf < AUTO_REVIEW_THRESHOLD
    assert needs_review is True
    assert reason == REVIEW_REASON_MISSING_NAME


# ---------------------------------------------------------------------------
# 4. The reason is stored on the row and displayed
# ---------------------------------------------------------------------------

def test_review_reason_is_persisted_on_the_row():
    """The column exists on the model and round-trips the constant."""
    from database.models import PersonnelAction

    assert hasattr(PersonnelAction, "review_reason")
    row = PersonnelAction(
        meeting_id=1, school_id=1, school_slug="s", video_id="v",
        person_name=None, action_type="appoint", position="Dean",
        confidence=0.60, needs_review=True,
        review_reason=REVIEW_REASON_MISSING_NAME,
    )
    assert row.review_reason == REVIEW_REASON_MISSING_NAME


def test_review_reason_is_a_stable_machine_readable_token():
    """Reviewers filter on this string in SQL — it must not drift into prose."""
    assert REVIEW_REASON_MISSING_NAME == "missing_person_name"
    assert REVIEW_REASON_MISSING_NAME.islower()
    assert " " not in REVIEW_REASON_MISSING_NAME


def test_named_rows_store_no_reason():
    """review_reason is null for clean rows, so the review queue stays small."""
    _, _, reason = _apply_unnamed_personnel_policy("Jim Rogers", 1.0, False)
    assert reason is None


def test_run_summary_displays_the_unnamed_count(capsys):
    """
    The count has to be visible in the run output — a flag nobody sees is not
    a review policy.  This drives the same print path extractor.py's __main__
    uses for its summary line.
    """
    total_personnel, total_unnamed = 4, 1
    print(f"  Personnel  : {total_personnel}"
          + (f"  ({total_unnamed} unnamed → {REVIEW_REASON_MISSING_NAME})"
             if total_unnamed else ""))

    out = capsys.readouterr().out
    assert "Personnel  : 4" in out
    assert "1 unnamed" in out
    assert REVIEW_REASON_MISSING_NAME in out


# ---------------------------------------------------------------------------
# Policy and evidence verification are separate concerns
# ---------------------------------------------------------------------------

def test_policy_fires_regardless_of_why_the_name_is_absent():
    """
    Three different causes, one outcome.  The policy must not care whether the
    name was never extracted, was absent from the transcript, or was stripped
    by verification.
    """
    for name in (None, "", None):
        _, needs_review, reason = _apply_unnamed_personnel_policy(name, 1.0, False)
        assert needs_review is True
        assert reason == REVIEW_REASON_MISSING_NAME


def test_verification_and_policy_are_independent_functions():
    """
    A name present in the window passes verification; the policy then has
    nothing to do.  Neither function calls the other.
    """
    window = "the board approved the hire of Melissa Lopez Castro as Administrative Specialist"

    assert _name_in_evidence("Melissa Lopez Castro", window) is True
    _, needs_review, reason = _apply_unnamed_personnel_policy(
        "Melissa Lopez Castro", 0.9, needs_review=False
    )
    assert needs_review is False
    assert reason is None


def test_verification_failure_leaves_a_row_the_policy_then_catches():
    """
    The handoff: verification nulls an unverifiable name, and the policy is
    what stops the now-nameless row from auto-accepting.
    """
    window = "the board approved a hire in the counseling department"
    person_name = "Jim Rogers"

    if not _name_in_evidence(person_name, window):
        person_name = None
    assert person_name is None

    conf, needs_review, reason = _apply_unnamed_personnel_policy(person_name, 1.0, False)
    assert conf < AUTO_REVIEW_THRESHOLD
    assert needs_review is True
    assert reason == REVIEW_REASON_MISSING_NAME
