"""Router pattern-layer tests.

Deliberately scoped to `_pattern_route`, the deterministic half of routing. It
needs no API, no LLM and no database, so it runs in CI in milliseconds and
never flakes — unlike tests/test_eval.py, whose LLM classifier varies run to
run by roughly one case.

The guard these protect is asymmetric by design. Blocking a query that the
transcripts could have answered is the worse failure, so the off-topic pattern
list is kept narrow and everything else is allowed through to retrieval. The
"must NOT be blocked" cases below are therefore the more important half.

Usage:
    uv run pytest tests/test_query_router.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag.query_router import _extract_schools, _pattern_route  # noqa: E402


# ── Off-topic: must be caught by pattern alone, without reaching the LLM ────
#
# Anything here that regressed to the LLM would still often route correctly,
# which is exactly why it needs a test: the failure is silent, costs a network
# round-trip, and shows up only as an occasional improvised answer.
OFF_TOPIC = [
    "What's the weather in Houston today?",
    "What is the weather in Dallas tomorrow?",
    "what's the weather like",
    "Give me today's weather",
    "What's the weather forecast?",
    "Tell me a joke",
    "Write me a poem about my cat.",
    "Can you write me a story?",
    "What's the recipe for banana bread?",
    "Who is dating that actress?",
    "What's the bitcoin price?",
    "Did you see the NFL game?",
    "Good morning!",
    "thanks",
]


@pytest.mark.parametrize("query", OFF_TOPIC)
def test_off_topic_is_blocked_by_pattern(query: str) -> None:
    decision = _pattern_route(query)
    assert decision is not None, f"pattern layer let {query!r} through to the LLM"
    assert decision["route"] == "none", f"{query!r} routed to {decision['route']!r}"


# ── On-topic: must NOT be blocked ───────────────────────────────────────────
#
# Weather is the sharp edge. Boards really do discuss closures and storm
# damage, so the patterns match "what is it like outside right now" phrasings
# and not the word itself. If a blanket \bweather\b ever creeps in, these fail.
ON_TOPIC = [
    "What did the board decide about inclement weather closures?",
    "How much did the college spend on weather damage repairs?",
    "What was discussed about severe weather preparedness on campus?",
    "Which campuses closed due to winter weather?",
    "How much did HCC approve for the fiscal year 2025-26 operating budget?",
    "What contracts with outside vendors has the board approved?",
    "Who was appointed chancellor?",
    "What did trustees say about the AI education program?",
    "Summarize the last board meeting.",
    "Compare the bond programs at Alamo Colleges and Dallas College.",
]


@pytest.mark.parametrize("query", ON_TOPIC)
def test_on_topic_is_not_blocked(query: str) -> None:
    decision = _pattern_route(query)
    # None is a fine outcome here: it means "no confident pattern, ask the
    # LLM". The only unacceptable result is being hard-blocked as off-topic.
    if decision is not None:
        assert decision["route"] != "none", (
            f"{query!r} was hard-blocked as off-topic; the transcripts may well "
            f"answer it"
        )


# ── Comparison routing ─────────────────────────────────────────────────────
#
# Two failure modes lived here. The pattern list covered "compared to" and
# "how does X compare" but not the bare imperative, and its cross-school clause
# was a hardcoded (hcc|houston community) x (lone star|lsc|san jac) pair list
# that could never fire for the six other colleges. Both are covered below;
# the second is why the "no HCC in sight" cases matter.
COMPARE = [
    "Compare the bond programs at Alamo Colleges and Dallas College.",
    "Compare Austin Community College and Mt. SAC.",
    "How does HCC's operating budget compare to El Paso's?",
    "What's the difference between Alamo Colleges and Central Texas College?",
    "Which colleges have invested in workforce development programs?",
    "How do enrollment trends vary across colleges?",
    # No comparison word at all — two colleges named is the signal.
    "What did Dallas College and Lone Star College approve for facilities?",
]


@pytest.mark.parametrize("query", COMPARE)
def test_comparison_queries_route_to_compare(query: str) -> None:
    decision = _pattern_route(query)
    assert decision is not None, f"pattern layer let {query!r} through to the LLM"
    assert decision["route"] == "compare", f"{query!r} routed to {decision['route']!r}"


SINGLE_SCHOOL = [
    "How much did HCC approve for the operating budget?",
    "What happened at Mt. SAC's last board meeting?",
    "What did trustees say about the Dallas College innovation center?",
]


@pytest.mark.parametrize("query", SINGLE_SCHOOL)
def test_single_school_questions_are_not_comparisons(query: str) -> None:
    """One college named is not a comparison, however the sentence is phrased.

    Guards the >= 2 rule from being loosened to >= 1, which would send every
    ordinary question down the cross-school fan-out.
    """
    decision = _pattern_route(query)
    if decision is not None:
        assert decision["route"] != "compare", (
            f"{query!r} routed to compare with one college named"
        )


# ── School extraction ──────────────────────────────────────────────────────
#
# Alamo, Dallas and Austin CC were seeded with the Panopto/Ravnur adapters but
# never added to _SCHOOL_ALIASES, so questions naming them extracted no school
# and were answered across all eight colleges. Every seeded slug is asserted
# here so the next college added fails this test rather than degrading quietly.
ALL_SLUGS = {
    "houston_city_college":     "How much did HCC approve for the budget?",
    "lone_star_college":        "What did Lone Star College decide?",
    "el_paso_community_college": "Summarize the last El Paso meeting.",
    "central_texas_college":    "What did Central Texas College approve?",
    "mt_san_antonio_college":   "What happened at Mt. SAC's last meeting?",
    "alamo_colleges":           "What did Alamo Colleges approve?",
    "dallas_college":           "What did Dallas College approve?",
    "austin_community_college": "What did Austin Community College approve?",
}


@pytest.mark.parametrize("slug,query", sorted(ALL_SLUGS.items()))
def test_every_seeded_college_is_recognised(slug: str, query: str) -> None:
    assert _extract_schools(query) == [slug]


def test_schools_come_back_in_first_mention_order() -> None:
    """answer.py uses schools[0] as the fallback filter, so order is meaning.

    _SCHOOL_PATTERNS is sorted by alias length, which is not query order.
    """
    assert _extract_schools("Dallas College and Lone Star College") == [
        "dallas_college", "lone_star_college",
    ]
    assert _extract_schools("Compare HCC and El Paso") == [
        "houston_city_college", "el_paso_community_college",
    ]


# ── Cases the pattern layer cannot decide ──────────────────────────────────
#
# General-knowledge trivia has no useful surface pattern — you cannot enumerate
# it — so it is the LLM classifier's job, steered by the NONE definition in
# _CLASSIFY_PROMPT. Asserted here as documentation of the split, so that a
# future reader does not "fix" it by bloating the regex.
LLM_S_JOB = [
    "What is the capital of France?",
    "Who wrote Hamlet?",
    "What is 17 times 23?",
]


@pytest.mark.parametrize("query", LLM_S_JOB)
def test_general_knowledge_falls_through_to_the_llm(query: str) -> None:
    assert _pattern_route(query) is None
