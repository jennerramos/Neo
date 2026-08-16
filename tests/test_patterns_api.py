"""
Pattern signals over the API.

pattern_signals held the only cross-college claims Neo makes and had no
endpoint at all — it was reachable solely by counting rows in
scripts/status_check.py.  These tests pin the parts of the new /patterns
layer that can be checked without a live database: the traceability
derivation, the route ordering that decides whether /patterns/summary is a
path or a bad integer, and the response contracts that carry a signal's
caveats alongside its claim.

Query behaviour against real rows is covered by the endpoints themselves;
this repo has no API/database fixture to hang integration tests on.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.db.queries.patterns import TRACEABLE_TYPES, _row_to_dict
from api.routers.patterns import router
from api.schemas.patterns import (
    PatternDetail, PatternEvidence, PatternRow, PatternsStats,
)


def _row(**over):
    """Minimal stand-in for a pattern_signals row from SQLAlchemy."""
    base = {
        "signal_id": 1,
        "signal_type": "recurring_initiative",
        "category": "dual_enrollment",
        "description": "…",
        "school_count": 7,
        "meeting_count": 35,
        "first_observed_date": None,
        "last_observed_date": None,
        "confidence": 0.97,
        "needs_review": False,
        "extractor_version": "v2.6",
        "supporting_initiative_ids": [1, 2, 3],
    }
    base.update(over)
    return SimpleNamespace(_mapping=base, **base)


# ---------------------------------------------------------------------------
# Traceability
# ---------------------------------------------------------------------------

def test_initiative_signal_with_ids_is_traceable():
    d = _row_to_dict(_row())
    assert d["traceable"] is True
    assert d["supporting_count"] == 3


def test_supporting_ids_do_not_leak_into_the_row():
    """The id array is an implementation detail; the row exposes a count."""
    d = _row_to_dict(_row())
    assert "supporting_initiative_ids" not in d


@pytest.mark.parametrize("signal_type", ["budget_trend", "personnel_trend"])
def test_aggregate_signals_are_not_traceable(signal_type):
    """These aggregate other tables and record no supporting ids."""
    d = _row_to_dict(_row(signal_type=signal_type, supporting_initiative_ids=[]))
    assert d["traceable"] is False
    assert d["supporting_count"] == 0


def test_initiative_signal_without_ids_is_not_traceable():
    """A traceable TYPE with an empty array still cannot be expanded — the
    claim is about the rows on hand, not the category they belong to."""
    d = _row_to_dict(_row(supporting_initiative_ids=[]))
    assert d["traceable"] is False


def test_null_id_array_is_treated_as_empty():
    d = _row_to_dict(_row(supporting_initiative_ids=None))
    assert d["supporting_count"] == 0
    assert d["traceable"] is False


def test_only_initiative_signals_are_declared_traceable():
    assert TRACEABLE_TYPES == {"recurring_initiative"}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _paths():
    return [r.path for r in router.routes]


def test_summary_is_registered_before_the_id_route():
    """Reversed, FastAPI would try to parse "summary" as an int and 422."""
    paths = _paths()
    assert paths.index("/patterns/summary") < paths.index("/patterns/{signal_id}")


def test_expected_routes_exist():
    assert set(_paths()) == {
        "/patterns",
        "/patterns/summary",
        "/patterns/{signal_id}",
        "/patterns/{signal_id}/evidence",
    }


# ---------------------------------------------------------------------------
# Response contracts
# ---------------------------------------------------------------------------

def test_row_defaults_are_conservative():
    """An unknown signal defaults to needing review and being untraceable —
    the safe direction for anything shown to trustees."""
    row = PatternRow(
        signal_id=1, signal_type="recurring_initiative", category="x",
        description="y", school_count=2, meeting_count=2,
    )
    assert row.needs_review is True
    assert row.traceable is False
    assert row.supporting_count == 0


def test_detail_extends_row_so_list_and_detail_cannot_drift():
    assert issubclass(PatternDetail, PatternRow)


def test_detail_lists_default_empty_with_no_note():
    d = PatternDetail(
        signal_id=1, signal_type="budget_trend", category="bond",
        description="y", school_count=3, meeting_count=4,
    )
    assert d.schools == []
    assert d.supporting_initiatives == []
    assert d.evidence == []
    assert d.trace_note is None


def test_evidence_requires_a_chunk():
    """A PatternEvidence row without a chunk id is not evidence — every
    quotation in this table was located inside a named chunk."""
    with pytest.raises(Exception):
        PatternEvidence(initiative_id=1, text="a quote")


def test_evidence_defaults_to_verified():
    ev = PatternEvidence(initiative_id=1, chunk_id="vid_0001", text="a quote")
    assert ev.verified is True
    assert ev.supports == []


def test_stats_shape():
    s = PatternsStats(
        total=27, trustee_ready=26, needs_review=1, by_type=[],
        categories=25, max_school_count=8, extractor_versions=["v2.6"],
    )
    assert s.total == s.trustee_ready + s.needs_review
