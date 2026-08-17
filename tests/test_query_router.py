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

from rag.query_router import _pattern_route  # noqa: E402


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
