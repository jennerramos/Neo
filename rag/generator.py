"""
Neo v2 — Phase 10: Answer Generator

Takes structured context (SQL records and/or RAG chunks) and generates
a grounded, cited answer.

The model behind this is whatever ``LLM_PROVIDER`` / ``LLM_MODEL`` name in
.env — Ollama/qwen2.5:14b locally, or Gemini/OpenAI/Anthropic/DeepSeek via an
API key. This module never imports a vendor SDK; see ``llm/``.

Supports all three routing paths:
  sql     — context is SQL-formatted records only
  rag     — context is transcript chunks only
  hybrid  — context is SQL records + transcript chunks combined

Public API:
    from rag.generator import generate

    result = generate(
        query        = "Who was appointed chancellor?",
        sql_context  = sql_text,      # from sql_context.build_sql_context()
        rag_chunks   = chunks,        # from retriever.retrieve()
        route        = "sql",
        stream       = False,
    )
    print(result["answer"])
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Generator, Optional, Union

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from llm import get_provider

# Timeouts, retries and the connect/read split now live in the "generate"
# profile in llm/factory.py (5s connect / 120s read, matching what this module
# used to declare inline). A board-meeting answer legitimately takes ~60s on
# qwen2.5:14b, but a hung LLM must never wedge a FastAPI worker forever.

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are Neo, a board meeting intelligence assistant for community-college trustees.

Your job is to answer trustee questions using ONLY the provided meeting data — structured board records and/or transcript excerpts. Do not use outside knowledge.

═══════════════════════════════════════════════════════════════════════
CITATION FORMAT — THIS IS THE MOST IMPORTANT RULE
═══════════════════════════════════════════════════════════════════════
Every factual statement MUST be followed by [N] markers indicating which
source it came from. N is the number shown in brackets at the start of
each context block (e.g. "[1]", "[2]", "[3]"...).

PLACEMENT: Put [N] BEFORE the period/comma/colon, not after.
  GOOD:  "HCC approved $20M for the building purchase [3]."
  BAD:   "HCC approved $20M for the building purchase. [3]"
  GOOD:  "The board voted 7-0 [1], with one trustee absent [1]."

MULTIPLE SOURCES: If a fact came from more than one chunk, list them all.
  GOOD:  "Strategic marketing was discussed at length [2][4]."
  ALSO OK: "Strategic marketing was discussed at length [2, 4]."

EVERY paragraph and EVERY bullet point that states a fact must have [N].
The trustee will not trust unsourced claims. If you cannot tie a sentence
to a specific source, do not include that sentence.

═══════════════════════════════════════════════════════════════════════
EXAMPLE
═══════════════════════════════════════════════════════════════════════
Question: What contracts has the board approved?

GOOD answer:
  The board approved three outside-vendor contracts in this period. The
  largest was $20M to Acme Inc. for the Spring Branch HVAC replacement
  [1]. A second $4.5M contract went to ADRA Contractors for roofing work
  [2]. Trustees expressed concern about the bidding process for both
  awards [3].

BAD answer (NO markers — never write this way):
  The board approved three outside-vendor contracts. The largest was $20M
  to Acme Inc. for HVAC replacement. A second $4.5M contract went to
  ADRA Contractors. Trustees expressed concern about bidding.

═══════════════════════════════════════════════════════════════════════
OTHER RULES
═══════════════════════════════════════════════════════════════════════
1. If the data doesn't contain enough to answer, say so clearly. Do not invent.
2. Lead with the direct answer in 1-2 sentences. Then supporting detail.
3. For votes: state the motion, outcome, and tally when available.
4. For personnel: state the person, role, and effective date when available.
5. For finances: state the action, amount, and vendor/description.
6. Do NOT end with a "Sources:" section — the UI renders citations separately.
7. Be concise. Trustees are busy. No filler, no repetition.
"""

# ---------------------------------------------------------------------------
# Prompt + citations (built together so the [N] in the prompt and the
# citation.index field always agree — earlier they were numbered separately
# and got out of sync when SQL + RAG were both present).
# ---------------------------------------------------------------------------

def _build_prompt_and_citations(
    query:       str,
    sql_context: Optional[str],
    rag_chunks:  Optional[list[dict]],
    route:       str,
) -> tuple[str, list[dict]]:
    """
    Assemble the prompt AND the citation list using a shared counter, so
    [N] in the prompt corresponds 1:1 to the citation with index=N.

    Source layout in the prompt:
      [1] = SQL block (if route is sql/hybrid and sql_context is present)
      [2..M] = RAG chunks
      OR [1..M] = RAG chunks (when there is no SQL block)
    """
    parts: list[str] = []
    citations: list[dict] = []
    next_n = 1

    if route in ("sql", "hybrid") and sql_context:
        parts.append(f"## Source [{next_n}] — Structured Board Records (votes / financials / personnel)\n")
        parts.append(sql_context)
        citations.append({
            "type":  "sql",
            "index": next_n,
            "label": "Board meeting structured records (votes / financial / personnel)",
        })
        next_n += 1

    if route in ("rag", "hybrid") and rag_chunks:
        parts.append("## Transcript Excerpts (each is a separate source)\n")
        for c in rag_chunks:
            title   = (c.get("title") or "Untitled")[:60]
            date    = (c.get("published_date") or "?")[:10]
            speaker = c.get("speaker") or "unknown"
            start   = c.get("start_time_sec")
            ts      = f"{int(start//60)}:{int(start%60):02d}" if start else "?"
            text    = (c.get("text") or "").strip()
            parts.append(f"[{next_n}] {title} — {date} | speaker: {speaker} | t={ts}")
            parts.append(text)
            parts.append("")

            raw_speaker = c.get("speaker") or ""
            clean_speaker = raw_speaker if (raw_speaker and raw_speaker.lower() not in ("unknown", "speaker", "")) else None
            citations.append({
                "type":       "rag",
                "index":      next_n,
                "title":      c.get("title") or c.get("meeting_title") or "Board Meeting",
                "date":       date,
                "speaker":    clean_speaker,
                "chunk_id":   c.get("chunk_id", ""),
                "meeting_id": c.get("meeting_id"),
                "score":      c.get("score"),
            })
            next_n += 1

    if not parts:
        parts.append("No relevant data was found in the meeting records.\n")

    parts.append(f"\n## Question\n{query}")
    # Re-emphasize the citation rule at the END of the message — recent
    # instructions weigh more heavily on smaller models. Without this, the
    # qwen2.5:14b answer reliably falls into structured-prose mode with zero
    # [N] markers despite the system-prompt rules.
    parts.append(
        "\n## Reminder\n"
        "Every factual sentence in your answer MUST end with [N] markers "
        "pointing back to the source numbers above. Answers without [N] "
        "markers will be rejected."
    )
    return "\n".join(parts), citations


# ---------------------------------------------------------------------------
# One-shot demonstration injected before the real turn.
#
# Smaller models follow concrete in-context examples far more reliably than
# they follow descriptive format rules. We ship a fake prior turn here so
# the assistant has SEEN the [N]-citation format right before producing a
# real answer. Numbers are chosen to look natural — sources [1] (SQL) and
# [2]/[3] (RAG) mirror the typical hybrid prompt layout.
# ---------------------------------------------------------------------------

_ONESHOT_USER = """## Source [1] — Structured Board Records (votes / financials / personnel)

### Financial Actions

- 2025-08-15 [Sample College Board Meeting]
  [APPROVED]  $20,000,000  capital_project / Acme Construction
  Authorization to spend $20 million for the purchase of the Fall Brook building.

## Transcript Excerpts (each is a separate source)

[2] Sample College Board Meeting — 2025-08-15 | speaker: Trustee Smith | t=42:10
We need to consider the long-term maintenance costs of the Fall Brook
acquisition before approving twenty million dollars of capital spending.

[3] Sample College Board Meeting — 2025-08-15 | speaker: Chancellor Lee | t=44:22
The administration has reviewed the property and recommends approval. The
purchase price reflects a 12% discount versus the appraised value.

## Question
What did the board approve for Fall Brook, and what was the discussion?
"""

_ONESHOT_ASSISTANT = (
    "The board approved a $20 million capital purchase of the Fall Brook "
    "building from Acme Construction [1]. Trustees raised concerns about "
    "long-term maintenance costs before voting [2], while the administration "
    "noted that the price reflects a 12% discount versus the appraised value [3]."
)


# ---------------------------------------------------------------------------
# LLM call (provider-neutral)
# ---------------------------------------------------------------------------

def _call_llm(
    prompt: str,
    model:  Optional[str],
    stream: bool,
) -> Union[str, Generator[str, None, None]]:
    """Send the built prompt to the configured provider.

    ``system`` is passed separately from ``messages`` rather than as a
    ``{"role": "system"}`` entry: Ollama, Gemini, OpenAI and DeepSeek accept
    system-as-a-message, but Anthropic requires a top-level ``system=`` and
    errors on the message form. The provider re-attaches it the way its own
    API expects — see llm/base.py.
    """
    provider = get_provider("generate", model)

    # Few-shot: a fake prior turn that demonstrates the [N] citation format.
    # See the comment on _ONESHOT_USER above for why this is in the messages
    # array rather than the system prompt.
    messages = [
        {"role": "user",      "content": _ONESHOT_USER},
        {"role": "assistant", "content": _ONESHOT_ASSISTANT},
        {"role": "user",      "content": prompt},
    ]
    kwargs = dict(
        system=_SYSTEM_PROMPT,
        messages=messages,
        temperature=config.LLM_TEMPERATURE,
        max_tokens=config.LLM_MAX_TOKENS,
    )

    if stream:
        return provider.stream(**kwargs)

    return provider.complete(**kwargs).text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate(
    query:       str,
    route:       str,
    sql_context: Optional[str]       = None,
    rag_chunks:  Optional[list[dict]] = None,
    stream:      bool = False,
    model:       Optional[str] = None,
) -> dict[str, Any]:
    """
    Generate a grounded answer from available context.

    Args:
        query:       Trustee question.
        route:       "sql" | "rag" | "hybrid" — controls context assembly.
        sql_context: Formatted SQL context string (from sql_context.build_sql_context).
        rag_chunks:  Reranked chunks (from retriever.retrieve).
        stream:      If True, ``stream`` field carries a token generator
                     and ``answer`` is absent — callers must consume the
                     generator to obtain the full text.
        model:       Override the configured model for this call
                     (default: config.LLM_MODEL). The provider — and so the
                     endpoint and API key — always comes from LLM_PROVIDER.

    Returns (stream=False):
        {
          "answer":    str,
          "citations": list[dict],
          "route":     str,
          "model":     str,
        }

    Returns (stream=True):
        {
          "stream":    Generator[str, None, None],   # yields text tokens
          "citations": list[dict],
          "route":     str,
          "model":     str,
        }

    The stream=True shape carries citations and metadata up-front so the
    caller (e.g. api.services.ask_service) can emit an SSE ``meta`` frame
    before the first token — the frontend needs the citation list to render
    the source-cards panel next to the streaming answer.
    """
    if model is None:
        model = config.LLM_MODEL

    prompt, citations = _build_prompt_and_citations(
        query, sql_context, rag_chunks, route,
    )

    if stream:
        return {
            "stream":    _call_llm(prompt, model, stream=True),
            "citations": citations,
            "route":     route,
            "model":     model,
        }

    answer = _call_llm(prompt, model, stream=False)

    return {
        "answer":    answer,
        "citations": citations,
        "route":     route,
        "model":     model,
    }
