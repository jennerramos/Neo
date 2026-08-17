"""
Neo v2 — Phase 10: Query Router

Classifies an incoming trustee question into one of three execution paths:

  sql     — Answer primarily from structured extraction tables (votes,
             financial_actions, personnel_actions).  Fast, precise, grounded.
             Best for: specific factual lookups (who was hired, what was approved).

  rag     — Answer from narrative transcript chunks via hybrid Qdrant retrieval.
             Best for: rationale, concerns, discussion themes, context.

  hybrid  — Run SQL *and* RAG, merge both contexts for the generator.
             Best for: questions that need both the decision AND the discussion.

Routing is done in two passes:
  1. Fast keyword/pattern matching (no LLM, sub-millisecond).
  2. If ambiguous, a short Ollama prompt classifies the intent.

RouteDecision fields:
  route    : "sql" | "rag" | "hybrid"
  tables   : which SQL tables to query  (subset of votes/financial/personnel)
  intent   : short human-readable description for logging/debugging
  confident: True if pattern-matched, False if LLM-classified
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Optional, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from llm import get_provider

log = logging.getLogger(__name__)

# The router classifier is a 5-token call — it should never take more than a
# couple of seconds. It uses the "route" profile (2s connect / 5s read, no
# retries) rather than the generous "generate" profile, so a hung or throttled
# LLM can't wedge every /ask. Failure defaults to route="hybrid" (see
# _llm_route below), a safe fallback that just runs both SQL and RAG paths.


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------

class RouteDecision(TypedDict):
    route:     str          # "sql" | "rag" | "hybrid" | "compare" | "latest_meeting" | "none"
    tables:    list[str]    # e.g. ["votes", "financial_actions"]
    intent:    str
    confident: bool
    schools:   list[str]    # slugs mentioned in the query (used by compare + latest_meeting)


# ---------------------------------------------------------------------------
# Pattern banks (compiled once at import)
# ---------------------------------------------------------------------------

# ── Hard off-topic patterns — things that can NEVER appear in a college board meeting.
# Keep this list NARROW. When in doubt, let RAG try — if there's nothing in
# the transcripts the LLM will say so. Don't block queries the vector DB should answer.
_OFF_TOPIC_PATTERNS = re.compile(
    # Weather needs care rather than a blanket \bweather\b: boards genuinely
    # discuss inclement-weather closures and storm damage. These variants are
    # the "what is it like outside right now" sense. "weather in <place> today"
    # is spelled out because it slipped past `today('s)? weather` and reached
    # the LLM, which saw "Houston" -- also a school alias -- and chose hybrid.
    r"\b(weather forecast|today('s)? weather|temperature outside|"
    r"what('s| is) the weather|weather (in|for) [a-z ]{2,20}(today|tomorrow|right now)|"
    r"recipe for|how to cook|cooking (tip|hack)|"
    r"tell me a joke|write me a (poem|story|song)|generate (code|an? essay)|"
    r"(nfl|nba|mlb|nhl|fifa) (score|game|match|player|team)|"
    r"(football|baseball|basketball|soccer) (score|game|player|team|match)|"
    r"stock (price|ticker|market)|crypto(currency)?|bitcoin|ethereum|"
    r"celebrity (news|gossip)|who is (dating|married to)|"
    r"president of (the us|america|united states)|"
    r"who (is|was) (trump|biden|obama|harris|elon|musk)|"
    r"(hello|hi there|hey there|good morning|good evening|how are you|"
    r"what('s| is) up|thank(s| you)|bye|goodbye|see you))\b",
    re.I,
)

# ── School name → slug mapping ───────────────────────────────────────────────
#
# Each slug has a list of phrasings users actually type. Order matters within
# a slug ONLY for documentation — matching uses regex with `\b` word
# boundaries so short acronyms (hcc, lsc, ctc, epcc) don't false-match
# inside other words (e.g. "lscape", "ctcontract"). Add new colleges here
# as more schools are seeded — each new entry also wants an eval case.
_SCHOOL_ALIASES: dict[str, list[str]] = {
    "houston_city_college": [
        "houston community college",
        "houston city college",
        "hcc",
    ],
    "lone_star_college": [
        "lone star college",
        "lone star",
        "lsc",
    ],
    "el_paso_community_college": [
        "el paso community college",
        "el paso college",
        "el paso",
        "epcc",
    ],
    "central_texas_college": [
        "central texas college",
        "central texas",
        "ctc",
    ],
    "mt_san_antonio_college": [
        "mount san antonio college",
        "mt. san antonio college",
        "mt san antonio college",
        "mt. san antonio",
        "mt san antonio",
        "mtsac",
        "mt sac",
        "mt. sac",
    ],
    # The three below were seeded with the Panopto and Ravnur caption adapters
    # but never added here, so until 2026-08-16 a question naming any of them
    # extracted NO school. That is not only a comparison problem: sql, rag and
    # hybrid all use this to attach a school filter when the request omits
    # school_slug, so "what did Dallas College approve" was silently answered
    # across all eight colleges.
    "alamo_colleges": [
        "alamo colleges",
        "alamo college",
        "alamo",
    ],
    "dallas_college": [
        "dallas college",
        "dallas county community college",
        "dallas",
    ],
    "austin_community_college": [
        "austin community college",
        "austin cc",
        "acc",
        # Deliberately not bare "austin": it is the city, the state capital and
        # a common surname, and this corpus is full of Texas place names.
        # "Austin College" is also a different, private college in Sherman TX.
    ],
}

# Flatten to (compiled_pattern, slug) and sort by alias length DESC so
# multi-word names like "houston community college" are checked before
# the bare "hcc" — defensive even though dedup-by-slug already handles it.
SCHOOL_NAME_MAP: dict[str, str] = {
    alias: slug for slug, aliases in _SCHOOL_ALIASES.items() for alias in aliases
}

_SCHOOL_PATTERNS: list[tuple[re.Pattern, str]] = sorted(
    [
        (re.compile(rf"\b{re.escape(alias)}\b", re.I), slug)
        for alias, slug in SCHOOL_NAME_MAP.items()
    ],
    key=lambda pat_slug: len(pat_slug[0].pattern),
    reverse=True,
)


def _extract_schools(query: str) -> list[str]:
    """
    Return list of school slugs mentioned in the query.
    Returns [] if no school names detected (means all schools).

    Uses word-boundary regex so "ctc" doesn't match inside "contract" and
    "lsc" doesn't match inside "lscape" / "tlsc". Order is preserved by
    first-mention so "compare HCC and El Paso" yields [hcc, epcc] rather
    than alphabetical.
    """
    # Collect with the match offset, because _SCHOOL_PATTERNS is ordered by
    # alias LENGTH (so "houston community college" is tried before "hcc"), not
    # by where the name appears. Appending in that order made the docstring's
    # first-mention promise false -- "Dallas College and Lone Star College"
    # came back [lone_star, dallas] -- and answer.py takes schools[0] as the
    # fallback school filter, so the "primary" college was whichever happened
    # to have the longer alias.
    hits: dict[str, int] = {}
    for pattern, slug in _SCHOOL_PATTERNS:
        if slug in hits:
            continue
        m = pattern.search(query)
        if m:
            hits[slug] = m.start()
    return sorted(hits, key=lambda slug: hits[slug])


# ── Latest-meeting signals ────────────────────────────────────────────────────
# Matches "the last/latest/most recent/newest (board) meeting(s)". Accepts the
# plural "meetings" because users write casually ("summarize the last HCC
# meetings"); we still route to a single meeting in that case rather than
# fanning out across every tracked meeting.
#
# The count guard below short-circuits this when the user actually IS asking
# for multiple meetings ("the last 2 HCC meetings", "the last three").
_LATEST_MEETING_PATTERNS = re.compile(
    r"\b(last|latest|most recent|most-recent|newest|recent)"
    r"(\s+\w+){0,4}\s+(board\s+)?meetings?\b",
    re.I,
)

# When any of these appears inside the matched span, the user wants a SET of
# meetings, not the single most-recent one — fall through to rag/hybrid so
# retrieval spans multiple meetings.
_LATEST_COUNT_GUARD = re.compile(
    r"\b(\d+|two|three|four|five|six|seven|eight|nine|ten|"
    r"several|multiple|few|couple)\b",
    re.I,
)

# ── Comparison signals ────────────────────────────────────────────────────────
_COMPARE_PATTERNS = re.compile(
    # The bare imperative comes first because it was the gap: only "compared
    # to", "difference between" and "how does X compare" were covered, so
    # "Compare the bond programs at Alamo Colleges and Dallas College" -- the
    # most natural phrasing -- fell through to the LLM, which called it RAG.
    r"\b(compare[ds]?|comparison|"
    r"better than|worse than|vs\.?|versus|"
    r"difference between|"
    # Pluralised: "which colleges have ..." missed a singular-only pattern.
    r"which (college|school|institution)s? (is|are|has|have|does|do|did|was|were)|"
    r"(more|less|higher|lower|greater|fewer) than .{1,20} (college|school)|"
    r"across (schools|colleges|institutions|campuses))\b",
    re.I,
)

# ── SQL-first signals ────────────────────────────────────────────────────────
_SQL_VOTES = re.compile(
    r"\b(how did the board vote|what did (they|the board) vote|vote result|"
    r"motion (to|that)|was (it|the motion) approved|pass(ed)?|unanimously|"
    r"who voted|roll.?call|board action)\b",
    re.I,
)

_SQL_FINANCIAL = re.compile(
    r"\b(budget (item|amendment|adjustment|approved|modified|cut)|"
    r"financial (action|adjustment|approval)|appropriat|expenditure|"
    r"how much (was|did)|dollar|fund(ing|ed)?|allocat|contract (value|amount)|"
    r"what (was|were) approved? (budget|financ)|tuition (increase|decrease|chang|set))\b",
    re.I,
)

_SQL_PERSONNEL = re.compile(
    r"\b(who was (hired|appointed|promoted|terminated|reassigned|named|selected)|"
    r"new (chancellor|president|cfo|coo|vp|dean|director|provost|superintendent)|"
    r"personnel (action|change|decision)|executive (hire|appointment)|"
    r"who (got|received) the (position|role|job)|"
    r"employment (action|decision))\b",
    re.I,
)

# ── RAG-first signals ────────────────────────────────────────────────────────
_RAG_PATTERNS = re.compile(
    r"\b(what (concern|reason|rationale|argument|sentiment|opinion|view|position|"
    r"theme|issue|problem|challenge)|why did|why (was|were|is|are)|"
    r"how did (trustees|board members|the board) (feel|react|respond|discuss|describe)|"
    r"what (was|were) (said|discussed|raised|mentioned|brought up) about|"
    r"trustee (comment|remark|question|concern)|background|context|"
    r"general (discussion|sentiment|feeling))\b",
    re.I,
)

# ── Hybrid signals (need both structure + narrative) ─────────────────────────
_HYBRID_PATTERNS = re.compile(
    r"\b(what (action|step|decision)s? (were|was) taken (regarding|about|on|for)|"
    r"(construction|capital|bond|facility|facilities|building) (project|improvement|work|discussed|approved)|"
    r"(contract|vendor|contractor) (discussion|approval|vote|award)|"
    r"(outside|external) (vendor|contractor)|contracts? with (outside|external|vendor)|"
    r"tuition (and|or) fee|financial aid|student fee|"
    # Initiative / strategy questions — need both structured record + narrative context
    r"what (is|are|was|were) (hcc|the college|the board|they) (implement|launch|deploy|roll.?out|pilot|introduc|invest|pursu|develop|plan)|"
    r"what (initiative|program|project|effort|plan|strategy|pilot|rollout)s? (is|are|was|were)|"
    r"(ai|artificial intelligence|technology|tech|innovation|workforce|equity|strategic) (initiative|program|plan|investment|effort|project|pilot)|"
    r"what (new|recent) (program|initiative|effort|investment|technolog)|"
    r"what (are|is|were|was) (colleges?|schools?|institutions?|hcc|they) (doing|working on|talking about|discussing|implementing|planning|saying) (about|with|on|regarding)?|"
    r"(colleges?|schools?) (doing|working on|talking|discussing) (with|about|on) (ai|technology|tech|workforce|equity|innovation)|"
    r"(ai|artificial intelligence|robotics|technology) (program|degree|course|bachelor|certif|initiative|investment))\b",
    re.I,
)

# ── Initiative / strategy signal (for table inference) ───────────────────────
_INITIATIVE_PATTERNS = re.compile(
    r"\b(implement|launch|deploy|roll.?out|pilot|introduc|invest|pursu|develop|"
    r"initiative|program|effort|strategy|plan|"
    r"ai|artificial intelligence|technology|innovation|workforce|strategic|equity)\b",
    re.I,
)


# ---------------------------------------------------------------------------
# Table-hint extractors
# ---------------------------------------------------------------------------

def _infer_tables(query: str) -> list[str]:
    """Return which SQL tables are likely relevant for this query."""
    tables = []
    if _SQL_VOTES.search(query) or re.search(
        r"\bvote|motion|approv|pass|second|aye|nay\b", query, re.I
    ):
        tables.append("votes")
    if _SQL_FINANCIAL.search(query) or re.search(
        r"\bbudget|financ|fund|dollar|contract|vendor|expenditure|allocat|"
        r"construction|capital|bond|facilit|building\b", query, re.I
    ):
        tables.append("financial_actions")
    if _SQL_PERSONNEL.search(query) or re.search(
        r"\bhired?|appoint|promot|terminat|reassign|personnel|staff|executive\b", query, re.I
    ):
        tables.append("personnel_actions")
    if _INITIATIVE_PATTERNS.search(query):
        tables.append("initiatives")
    return tables or ["votes", "financial_actions", "personnel_actions"]


# ---------------------------------------------------------------------------
# Fast pattern-based router
# ---------------------------------------------------------------------------

def _pattern_route(query: str) -> RouteDecision | None:
    """
    Return a RouteDecision if patterns match confidently, else None.
    Priority: off-topic check first, then hybrid > sql > rag.
    """
    q = query.strip()

    # ── Off-topic: hard pattern match ────────────────────────────────────────
    if _OFF_TOPIC_PATTERNS.search(q):
        return RouteDecision(
            route="none",
            tables=[],
            intent="off-topic (hard pattern match)",
            confident=True,
            schools=[],
        )

    # ── No keyword guard here. ────────────────────────────────────────────────
    # Any topic that isn't hard-blocked above gets a chance through RAG.
    # If the vector DB has nothing relevant, the LLM will say so naturally.
    # Blocking queries before search defeats the purpose of having indexed transcripts.

    # ── Comparison check — cross-school queries ──────────────────────────────
    #
    # Naming two colleges in one question IS a comparison, whatever the
    # phrasing, so that counts on its own. This replaces a hardcoded pair list
    # -- (hcc|houston community) x (lone star|lsc|san jac) -- which could never
    # fire for the six other colleges we now track, and which would have needed
    # a new alternation for every pair on every school added. The same
    # HCC-centric leftover that P2-1 cleaned out of the off-topic message.
    #
    # answer.py's compare branch fans out per slug and falls back to all
    # schools on an empty list, so over-firing here degrades to a wider search
    # rather than a wrong answer.
    schools = _extract_schools(q)
    if _COMPARE_PATTERNS.search(q) or len(schools) >= 2:
        return RouteDecision(
            route="compare",
            tables=_infer_tables(q),
            intent="cross-school comparison",
            confident=True,
            schools=schools,
        )

    # ── Latest-meeting check — scope to ONE specific meeting ────────────────
    # Runs before sql/rag/hybrid so queries like "what votes happened in the
    # last HCC meeting" pin to that meeting rather than fanning out.
    #
    # Count-guard: phrases like "the last 2 HCC meetings" or "the last three"
    # mean the user wants a set, not one — fall through so retrieval spans
    # multiple meetings.
    lm_match = _LATEST_MEETING_PATTERNS.search(q)
    if lm_match and not _LATEST_COUNT_GUARD.search(lm_match.group(0)):
        return RouteDecision(
            route="latest_meeting",
            tables=_infer_tables(q),
            intent="summarize the most recent meeting",
            confident=True,
            schools=schools,
        )

    # ── Mentioned schools (used by sql/rag/hybrid below) ─────────────────────
    # Extracted once above for the comparison check; reused here. answer.py
    # uses decision["schools"][0] as a fallback when the request omits
    # school_slug.
    mentioned = schools

    # Hybrid check first — it overrides pure sql/rag
    if _HYBRID_PATTERNS.search(q):
        return RouteDecision(
            route="hybrid",
            tables=_infer_tables(q),
            intent="structured decision + narrative discussion",
            confident=True,
            schools=mentioned,
        )

    # Count sql vs rag signals
    sql_hits = (
        bool(_SQL_VOTES.search(q))
        + bool(_SQL_FINANCIAL.search(q))
        + bool(_SQL_PERSONNEL.search(q))
    )
    rag_hits = bool(_RAG_PATTERNS.search(q))

    if sql_hits > 0 and not rag_hits:
        return RouteDecision(
            route="sql",
            tables=_infer_tables(q),
            intent="factual structured lookup",
            confident=True,
            schools=mentioned,
        )

    if rag_hits and sql_hits == 0:
        return RouteDecision(
            route="rag",
            tables=[],
            intent="narrative / reasoning / discussion",
            confident=True,
            schools=mentioned,
        )

    if sql_hits > 0 and rag_hits:
        return RouteDecision(
            route="hybrid",
            tables=_infer_tables(q),
            intent="mixed factual + narrative",
            confident=True,
            schools=mentioned,
        )

    return None   # ambiguous — fall through to LLM classifier


# ---------------------------------------------------------------------------
# LLM fallback classifier (Ollama)
# ---------------------------------------------------------------------------

_CLASSIFY_PROMPT = """\
You are a query classifier for a college board meeting intelligence system.
The system has indexed transcripts and records from real board meetings covering topics like:
AI, technology, cloud, cybersecurity, workforce development, equity, budgets, contracts,
personnel, facilities, student programs, accreditation, strategic plans, and more.

Classify the question into exactly one category:

  SQL    – Asks for specific facts from structured records: who was hired/appointed/voted,
           what was approved, vote counts, contract amounts, budget figures, personnel actions.
  RAG    – Asks about topics, discussions, rationale, themes, plans, or anything that
           requires searching meeting transcripts. Use RAG for ANY topic that could appear
           in a college board meeting — technology, AI, cloud, innovation, programs, etc.
  HYBRID – Needs BOTH structured records AND transcript search for a complete answer.
  NONE   – ONLY for things that absolutely cannot appear in any board meeting:
           weather, sports scores, celebrity gossip, cooking recipes, personal greetings.
           Do NOT use NONE for education or technology topics — search the transcripts first.

Question: {query}

Respond with exactly one word: SQL, RAG, HYBRID, or NONE."""

# Keep this prompt SHORT. Measured 2026-08-16 against gemini-3.5-flash: an
# expanded NONE definition (1881 chars vs 1273) ran 12.8-15.1s against this
# one's 7.5-10.7s, and classified nothing better — both label "capital of
# France", "who wrote Hamlet" and "17 times 23" as NONE, despite the list above
# naming none of them. Since a classifier that overruns LLM_ROUTER_READ_TIMEOUT
# falls back to "hybrid", extra prompt length converts directly into wrong
# routes. Adding an example here is not free.


# The classifier answers with one word. The old Ollama call relied on a hard
# 5-token cap to truncate anything longer; that cap is unsafe on a cloud
# reasoning model, where thinking tokens are spent from the same budget and a
# tight cap yields an EMPTY reply (which would silently route everything to
# hybrid). So: give it a little headroom and parse the label out of whatever
# comes back.
_ROUTER_MAX_TOKENS = config.LLM_ROUTER_MAX_TOKENS
_LABEL_RE = re.compile(r"\b(SQL|RAG|HYBRID|NONE)\b")


def _parse_label(text: str) -> str:
    """Pull the route label out of the classifier reply. '' -> hybrid fallback."""
    m = _LABEL_RE.search((text or "").upper())
    return m.group(1) if m else "HYBRID"


def _llm_route(query: str) -> RouteDecision:
    """Use the configured LLM to classify ambiguous queries.

    The classifier prompt carries its own instructions, so there is no system
    prompt here — hence ``system=""``.
    """
    try:
        result = get_provider("route").complete(
            system="",
            messages=[{"role": "user", "content": _CLASSIFY_PROMPT.format(query=query)}],
            temperature=0,
            max_tokens=_ROUTER_MAX_TOKENS,
        )
        label = _parse_label(result.text)
    except Exception as exc:  # noqa: BLE001
        # Safe default on timeout / connection / config failure. This fallback
        # is deliberately silent to the caller, but it must NOT be silent to
        # us: on a local Ollama it fired ~never, while a cloud provider can
        # trip the 5s router budget and degrade routing quality with no other
        # symptom. Without this line the only evidence is a route that looks
        # like a model opinion but was actually a network failure.
        log.warning(
            "router classifier failed, defaulting to hybrid: %s: %s",
            type(exc).__name__, exc,
        )
        label = "HYBRID"

    route_map = {"SQL": "sql", "RAG": "rag", "HYBRID": "hybrid", "NONE": "none"}
    route = route_map.get(label, "hybrid")

    # Always extract schools — answer.py uses them as a fallback school filter
    # for sql/rag/hybrid routes when the request omits an explicit school_slug.
    return RouteDecision(
        route=route,
        tables=_infer_tables(query) if route in ("sql", "hybrid") else [],
        intent=f"llm-classified as {route}",
        confident=False,
        schools=_extract_schools(query),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def route_query(query: str) -> RouteDecision:
    """
    Classify a trustee question and return a RouteDecision.

    Tries fast pattern matching first; falls back to Ollama if ambiguous.

    Examples:
        route_query("Who was appointed chancellor?")
        → RouteDecision(route='sql', tables=['personnel_actions'], ...)

        route_query("What concerns did trustees raise about the budget?")
        → RouteDecision(route='rag', tables=[], ...)

        route_query("What actions were taken regarding student fees?")
        → RouteDecision(route='hybrid', tables=['financial_actions', 'votes'], ...)
    """
    decision = _pattern_route(query)
    if decision is not None:
        return decision
    return _llm_route(query)


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        "Who was hired, appointed, or promoted at the executive level?",
        "What budget items did the board approve?",
        "How did the board vote on the tuition increase?",
        "What vendor contracts were approved?",
        "What concerns were raised about the budget cuts?",
        "What rationale did trustees give for the vote?",
        "What themes came up around capital projects?",
        "What actions were taken regarding student tuition and fees?",
        "What construction projects were discussed or approved?",
        "Did the board vote on any contracts with outside vendors?",
        "Who is the new chancellor?",
        "Why did the board delay the facilities vote?",
    ]

    print("Query Router — routing decisions\n" + "=" * 60)
    for q in tests:
        d = route_query(q)
        flag = "✓" if d["confident"] else "~"
        print(f"[{flag}] {d['route'].upper():<7} | {q[:60]}")
        if d["tables"]:
            print(f"         tables: {d['tables']}")
