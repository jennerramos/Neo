# Neo v2 — Making the LLM Provider Interchangeable

**Status:** **Implemented** 2026-08-13 (steps 1–4 + Gemini + error mapping).
See §13 for what shipped and where reality differed from this design.
**Date:** 2026-08-09
**Built from commit:** `cd74efc`
**Scope:** Serving path only (`/ask` generation + query routing). The offline
extraction pipeline stays on Ollama in phase 1 — see §3 and §10.

---

## 0. TL;DR

Neo v2 currently generates every trustee answer with `qwen2.5:14b` through a local
Ollama install. The goal is to make the provider selectable by configuration
(`LLM_PROVIDER=ollama|openai|anthropic|deepseek`) without touching the RAG
pipeline, `/ask`, retrieval, citations, or the frontend.

**The coupling is far shallower than the file count suggests.** Exactly **two files**
on the serving path talk to Ollama, totaling **~70 lines**:

- `rag/generator.py` — client construction + the chat call + response parsing
- `rag/query_router.py` — a second client for the ambiguous-query classifier

Everything else is already provider-neutral: `POST /ask`, Qdrant hybrid search,
BM25, the BGE reranker, SQL context assembly, the citation numbering, the SSE
frame taxonomy, and the entire `frontend/` tree.

**Minimum viable change:** rewrite two functions, delete two client constructors,
add a neutral config block, and add an additive `llm/` package. Seven existing
files touched (two substantively), eight new files, zero frontend changes.

**Verified working baseline:** `ollama list` on the dev box confirms
`qwen2.5:14b` (9.0 GB) and `nomic-embed-text:latest` are installed locally.

---

## 1. Current coupling inventory

### Tier 1 — serving path, genuinely Ollama-specific (MUST change)

#### `rag/generator.py:39-61` — `_get_client()`

```python
import httpx
from ollama import Client as _OllamaClient

_OLLAMA_CONNECT_TIMEOUT = 5.0
_OLLAMA_READ_TIMEOUT    = 120.0

_client: Optional[_OllamaClient] = None

def _get_client() -> _OllamaClient:
    """Module-singleton Ollama client with explicit timeouts."""
    global _client
    if _client is None:
        _client = _OllamaClient(
            host=config.OLLAMA_HOST,
            timeout=httpx.Timeout(connect=..., read=..., write=..., pool=...),
        )
    return _client
```

**Why coupled:** hard import of the `ollama` SDK; `host=` is an Ollama-shaped
kwarg (cloud SDKs use `base_url=` plus `api_key=`); there is no auth concept at
all.

#### `rag/generator.py:265-284` — the call + response parsing inside `_call_ollama()`

```python
    if stream:
        def _gen():
            for chunk in client.chat(
                model=model, messages=messages, stream=True,
                options={"temperature": 0.1, "num_predict": 1024},
            ):
                token = chunk.get("message", {}).get("content", "")
                if token:
                    yield token
        return _gen()

    resp = client.chat(
        model=model, messages=messages, stream=False,
        options={"temperature": 0.1, "num_predict": 1024},
    )
    return resp["message"]["content"]
```

**Why coupled — four separate ways:**

1. `options={...}` — Ollama's parameter envelope. OpenAI and Anthropic take flat kwargs.
2. `num_predict` — Ollama's name for max output tokens. OpenAI: `max_tokens` /
   `max_completion_tokens`. Anthropic: `max_tokens`, and it is **required**.
3. `resp["message"]["content"]` — Ollama's dict shape. OpenAI:
   `resp.choices[0].message.content`. Anthropic: `resp.content[0].text`.
4. `chunk["message"]["content"]` — Ollama's stream frame. OpenAI:
   `chunk.choices[0].delta.content`. Anthropic: typed events
   (`content_block_delta.delta.text`).

#### `rag/generator.py:258-263` — the message array (partially coupled)

```python
    messages = [
        {"role": "system",    "content": _SYSTEM_PROMPT},
        {"role": "user",      "content": _ONESHOT_USER},
        {"role": "assistant", "content": _ONESHOT_ASSISTANT},
        {"role": "user",      "content": prompt},
    ]
```

**Why coupled:** the `role: "system"` entry. Ollama and OpenAI accept
system-as-a-message; **Anthropic does not** — the system prompt is a top-level
`system=` parameter, and a `{"role": "system"}` entry is an API error. This
single line is the biggest shape difference across providers and drives the
interface design in §4.

#### `rag/query_router.py:41-61, 435-444` — second client + classifier call

```python
from ollama import Client as _OllamaClient
_ROUTER_CONNECT_TIMEOUT = 2.0
_ROUTER_READ_TIMEOUT    = 5.0
...
        resp = client.chat(
            model=config.OLLAMA_MODEL,
            messages=[{"role": "user", "content": _CLASSIFY_PROMPT.format(query=query)}],
            options={"temperature": 0, "num_predict": 5},
        )
        label = resp["message"]["content"].strip().upper()
```

**Why coupled:** same SDK, same `options`/`num_predict`, same dict-shaped
response. Note the deliberately *different* timeout budget (2 s / 5 s versus
5 s / 120 s) — any abstraction must preserve two distinct timeout profiles
rather than collapsing them into one.

### Tier 2 — model-name leakage (SHOULD change)

| Location | Code | Problem |
|---|---|---|
| `api/services/ask_service.py:64` | `model=result.get("model", "qwen2.5:14b")` | Hardcoded string literal as fallback |
| `api/services/ask_service.py:190` | `model=result.get("model", "qwen2.5:14b")` | Same literal, streaming path |
| `rag/answer.py:138` | `"model": model or config.OLLAMA_MODEL` | `route="none"` short-circuit reports Ollama without calling any LLM |
| `rag/answer.py:173` | `"model": model or config.OLLAMA_MODEL` | latest-meeting-empty short-circuit, same |
| `rag/answer.py:434` | `parser.add_argument("--model", default=config.OLLAMA_MODEL)` | CLI default |

These do not break provider switching, but under `LLM_PROVIDER=openai` the UI
footer (`frontend/src/components/ask/AskBox.tsx:381`) and
`data/query_log.jsonl` would report `qwen2.5:14b` for answers GPT actually
produced — corrupting the only quality-audit trail the project has.

### Tier 3 — config and environment

- **`config.py:76-81`** — `OLLAMA_HOST` / `OLLAMA_MODEL`. Provider-named, no key,
  no neutral abstraction.
- **`config.py:83-88`** — `ANTHROPIC_API_KEY` / `CLAUDE_MODEL="claude-opus-4-6"`.
  Dead: nothing in the tree imports `anthropic`. That default model ID is also
  stale; current Anthropic models are the Claude 5 family (`claude-opus-5`,
  `claude-sonnet-5`).
- **`.env.example:22-28`** — documents both pairs, including the unused Claude one.
- **`pyproject.toml:51-54, 64-67`** — `extraction = ["ollama>=0.2"]` (which `rag/`
  also depends on, so the label is wrong) and `llm = ["anthropic>=0.28"]`
  (installed, unused).

### Tier 4 — offline pipeline (out of scope for phase 1)

`pipeline/extractor.py:203-267` and `pipeline/initiative_extractor.py:90-135`
use a **third** client style — raw `requests.post` to `/api/chat` — with
`"format": "json"` (Ollama's structured-output flag) and `num_predict: 2048`.
`check_ollama()` probes `GET /api/tags`.

`format=json` is the hardest thing in the codebase to abstract: OpenAI needs
`response_format={"type": "json_object"}`, and **Anthropic has no JSON mode at
all** (you use tool-calling or assistant prefill). These are offline batch jobs
where local inference is free, so they stay on Ollama.

### Tier 5 — stale, not actually coupled

- `pipeline/indexer.py:423` prints `"✅ Ollama embed ready"`, but
  `_get_dense_model()` (`:129-145`) loads fastembed ONNX. Cosmetic only.
- `scripts/preflight_check.py:4-13` probes `/api/tags`.

---

## 2. Dependency trace — who knows Ollama exists

```
User ─▶ frontend/AskBox.tsx ──────────────────────── NEUTRAL (displays result.model as opaque string)
  ▼
POST /ask
  ▼
api/routers/ask.py:21 ask() ─────────────────────── NEUTRAL
  ▼
api/schemas/ask.py:28 AskResponse ───────────────── NEUTRAL (model: str, free-form)
  ▼
api/services/ask_service.py:83/124 ──────────────── LEAKS ("qwen2.5:14b" literal x2, docstring)
  ▼
rag/answer.py:48 ask() ──────────────────────────── LEAKS (config.OLLAMA_MODEL x3, docstrings)
  │
  ├─▶ rag/query_router.py:466 route_query()
  │      └─ _llm_route():435 ──────────────────────  ★ COUPLED  (ollama.Client, options, dict parse)
  │
  ├─▶ rag/sql_context.py:272 build_sql_context() ── NEUTRAL (pure SQL)
  ├─▶ rag/retriever.py:286 retrieve() ───────────── NEUTRAL (Qdrant + fastembed + BGE, zero LLM)
  │
  └─▶ rag/generator.py:291 generate()
         ├─ _build_prompt_and_citations():126 ───── NEUTRAL (pure string/dict assembly)
         ├─ _SYSTEM_PROMPT:67 / _ONESHOT_*:209 ──── NEUTRAL text, but TUNED for a 14B model
         └─ _call_ollama():243 ──────────────────── ★ COUPLED  (client, message shape, options, parse)
  ▼
observability/query_log.py:100 ──────────────────── NEUTRAL (records whatever `model` string it is given)
```

**Only two components genuinely know:** `rag/generator.py` and
`rag/query_router.py`. Two more merely *mention* it via string literals and
defaults (`api/services/ask_service.py`, `rag/answer.py`).

---

## 3. Change matrix

| File | Verdict | Exactly what changes | Why |
|---|---|---|---|
| `rag/generator.py` | **MUST** | Delete `_get_client()` (39-61); rewrite `_call_ollama()` (243-284) as `_call_llm()` that splits `system` from `messages` and delegates to a provider. Keep `_SYSTEM_PROMPT`, `_ONESHOT_*`, `_build_prompt_and_citations`, and `generate()`'s signature | This is where the SDK, the `options` envelope, and response parsing live |
| `rag/query_router.py` | **MUST** | Delete `_get_router_client()` (47-61); rewrite the `_llm_route()` call (438-444) to use a short-timeout provider handle. Keep `_CLASSIFY_PROMPT`, `_pattern_route`, and the fallback-to-hybrid behavior | Second SDK call site, second timeout profile |
| `config.py` | **MUST** | **Add** `LLM_PROVIDER/MODEL/API_KEY/BASE_URL/TEMPERATURE/MAX_TOKENS/*_TIMEOUT`, each defaulting back to current Ollama values. **Keep** `OLLAMA_*` for the pipeline | Neutral names are the switch; back-compat defaults mean no `.env` change today |
| `api/services/ask_service.py` | **SHOULD** | Replace the `"qwen2.5:14b"` literals at `:64` and `:190` with `config.LLM_MODEL`. Later: map typed LLM errors to 503/429 | Wrong model in logs and UI otherwise; cloud providers add 429/5xx that `handle_ask` does not handle at all today |
| `rag/answer.py` | **SHOULD** | `config.OLLAMA_MODEL` → `config.LLM_MODEL` at `:138, :173, :434`; refresh docstrings at `:8, :74` | Three one-line edits, no control-flow change |
| `.env.example` | **SHOULD** | Document the `LLM_*` block; mark `OLLAMA_*` as pipeline-only; drop or annotate the dead `CLAUDE_MODEL` | Documentation, not behavior |
| `pyproject.toml` | **SHOULD** | Move `ollama` out of `extraction` into a real `llm` extra; add `openai` and keep `anthropic` as optional extras | `rag/` already depends on `ollama` while being labeled a Phase-6 extra |
| `scripts/preflight_check.py` | **SHOULD** | Branch on `LLM_PROVIDER`; for cloud, report **key present / absent only**, never the value | Preflight is meaningless if it probes Ollama while you are on OpenAI |
| `api/routers/ask.py` | **UNCHANGED** | — | Knows nothing but `AskRequest` → service |
| `api/schemas/ask.py` | **UNCHANGED** | — | `model: str` is already opaque |
| `api/main.py` | **UNCHANGED** | — | Warms retrieval models only; never touched the LLM |
| `rag/retriever.py` | **UNCHANGED** | — | fastembed ONNX + Qdrant + BGE. Zero LLM calls |
| `rag/sql_context.py` | **UNCHANGED** | — | Pure SQL + string formatting |
| `rag/meeting_lookup.py` | **UNCHANGED** | — | Pure SQL |
| `observability/query_log.py` | **UNCHANGED** | — | `getattr(response, "model")` — provider-agnostic by construction |
| `frontend/**` | **UNCHANGED** | — | SSE frame taxonomy is provider-independent; `model` is a display string |
| `pipeline/extractor.py`, `pipeline/initiative_extractor.py` | **UNCHANGED (phase 1)** | Optionally later | `format=json` is the least portable feature; offline batch stays free and local |
| `pipeline/indexer.py:423` | **UNCHANGED** | Stale print, cosmetic | Not on the LLM path |

### Components that stay provider-independent

| Component | Answer |
|---|---|
| `POST /ask` | **Yes, completely.** Router and schemas never see a provider |
| Retrieval | **Yes.** No LLM anywhere in `retriever.py` |
| Qdrant / vector search | **Yes.** Storage layer, orthogonal |
| BM25 | **Yes.** fastembed sparse, local |
| Reranking | **Yes.** BGE cross-encoder via sentence-transformers, local |
| Context construction | **Yes.** `_build_prompt_and_citations` is pure assembly — but it must start emitting `(system, messages)` instead of the message array being assembled inside the Ollama function |
| Citations | **Yes.** The `next_n` shared counter is pure Python. Caveat: citation *compliance* is behavioral, not structural — see §6 |
| Frontend | **Yes, zero changes** |

---

## 4. Proposed abstraction

A `generate(prompt, context, options) -> response` interface was considered and
**is not recommended**, for a reason grounded in the existing code: Neo's prompt
is not one string. `_call_ollama` builds a **four-message array** containing a
synthetic one-shot exchange (`_ONESHOT_USER` / `_ONESHOT_ASSISTANT`,
`rag/generator.py:209-236`) that exists specifically because `qwen2.5:14b`
dropped citations without it. Flattening to a single `prompt` string would
either destroy that few-shot or force every provider to re-derive it.

### Recommended interface

```python
# llm/base.py
from dataclasses import dataclass
from typing import Iterator, Protocol

@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    provider: str
    prompt_tokens: int | None = None       # normalized across providers
    completion_tokens: int | None = None

class LLMProvider(Protocol):
    provider: str          # "ollama" | "openai" | "anthropic" | "deepseek"
    model: str

    def complete(self, *, system: str, messages: list[dict],
                 temperature: float, max_tokens: int,
                 json_mode: bool = False) -> LLMResult: ...

    def stream(self, *, system: str, messages: list[dict],
               temperature: float, max_tokens: int) -> Iterator[str]: ...
```

Two design decisions carry the whole thing:

1. **`system` is a separate parameter, not a message.** Ollama and OpenAI
   providers prepend it internally as `{"role": "system"}`; Anthropic passes it
   as `system=`. Without this split, every call site would need an
   `if provider == "anthropic"` branch — precisely what the abstraction exists
   to prevent.
2. **`stream()` returns a plain `Iterator[str]` of text deltas.** This matches
   what `api/services/ask_service.py:115-121` (`_next_or_sentinel`) already
   consumes, so the SSE layer needs **zero** changes.

### Error taxonomy

```python
# llm/errors.py
class LLMError(Exception): ...
class LLMUnavailable(LLMError): ...   # connection refused, Ollama down, 5xx
class LLMTimeout(LLMError): ...
class LLMRateLimited(LLMError): ...   # 429 — cloud-only, new failure mode
class LLMAuthError(LLMError): ...     # 401/403 — cloud-only
```

**Why this matters:** today `rag/generator.py` has *no* error handling, and
`handle_ask` (`api/services/ask_service.py:83-105`) has **no try/except** — a
dead Ollama produces a raw 500. Survivable when the LLM is on localhost.
With a metered API you gain 429s and 401s, and `/ask` should return 503/429
with a useful message rather than a stack trace.

### Factory

```python
# llm/factory.py
@lru_cache(maxsize=None)
def get_provider(profile: str = "generate") -> LLMProvider:
    """profile: 'generate' (long read budget) | 'route' (fast-fail classifier)"""
```

`lru_cache` reproduces today's module-singleton behavior, so clients are still
built once per process, and the two timeout profiles (5 s/120 s for generation,
2 s/5 s for routing) are preserved.

### Package layout — all new files, nothing moved

```
llm/
  __init__.py        # re-export get_provider, LLMResult, errors
  base.py            # Protocol + LLMResult
  errors.py          # exception taxonomy
  factory.py         # provider selection from config
  providers/
    ollama.py
    openai.py
    anthropic.py
    deepseek.py      # ~10 lines: subclass OpenAIProvider, different base_url
```

---

## 5. Configuration

### `config.py` — neutral vars only, with back-compat fallbacks

```python
# LLM — provider-neutral (serving path: /ask generation + query routing)
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "ollama")
LLM_MODEL:    str = os.getenv("LLM_MODEL", OLLAMA_MODEL)      # falls back to qwen2.5:14b
LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "")             # "" = provider default
LLM_API_KEY:  str = os.getenv("LLM_API_KEY", "")              # never a literal default

LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.1"))
LLM_MAX_TOKENS:  int   = int(os.getenv("LLM_MAX_TOKENS", "1024"))
LLM_CONNECT_TIMEOUT: float = float(os.getenv("LLM_CONNECT_TIMEOUT", "5"))
LLM_READ_TIMEOUT:    float = float(os.getenv("LLM_READ_TIMEOUT", "120"))
LLM_ROUTER_READ_TIMEOUT: float = float(os.getenv("LLM_ROUTER_READ_TIMEOUT", "5"))

# OLLAMA_HOST / OLLAMA_MODEL above stay — the offline pipeline keeps using them.
```

The fallback chain (`LLM_MODEL` → `OLLAMA_MODEL` → `"qwen2.5:14b"`) is what
makes this a **zero-`.env`-change** upgrade for the current box.

### Environment (`.env`, gitignored) — the actual switch

```bash
# Local (today's behavior — nothing to set, these are the defaults)
LLM_PROVIDER=ollama
LLM_MODEL=qwen2.5:14b
LLM_BASE_URL=http://localhost:11434

# OpenAI
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=<set in .env, never committed>

# Anthropic
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-5
LLM_API_KEY=<set in .env, never committed>

# DeepSeek
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat
LLM_API_KEY=<set in .env, never committed>
LLM_BASE_URL=https://api.deepseek.com   # or let the provider default it
```

### Provider modules — everything provider-shaped

Default base URLs, header names (`Authorization: Bearer` versus `x-api-key` plus
`anthropic-version`), parameter renaming (`max_tokens` ↔ `num_predict`), response
unwrapping, error mapping into the taxonomy, retry policy, and streaming-event
decoding.

### Secret-handling rules this design must follow

- `LLM_API_KEY` uses `os.getenv(..., "")` — **never** a literal default, matching
  how `config.py:44` already treats `YOUTUBE_API_KEY`.
- `.env` stays gitignored; `.env.example` carries **placeholder text only**, as it
  does today at `:6, :20, :27`.
- `scripts/preflight_check.py` prints `key: present/absent` — never the value,
  never a prefix.
- `observability/query_log.py` logs `model` only (`:100`); it never touches keys.
  Keep it that way.
- Never include the key in `LLMAuthError` messages — sanitize before raising.

---

## 6. Provider differences the layer must absorb

| Concern | Ollama | OpenAI | Anthropic Claude | DeepSeek |
|---|---|---|---|---|
| **Auth** | none | `Authorization: Bearer` | `x-api-key` + `anthropic-version` header | `Authorization: Bearer` |
| **Base URL** | `http://localhost:11434` | `https://api.openai.com/v1` | `https://api.anthropic.com` | `https://api.deepseek.com` |
| **Request** | `client.chat(model, messages, options={})` | `chat.completions.create(model, messages, ...)` | `messages.create(model, system=, messages=, max_tokens=)` | OpenAI-shaped |
| **Response** | `resp["message"]["content"]` | `resp.choices[0].message.content` | `resp.content[0].text` | OpenAI-shaped |
| **System msg** | role in `messages` | role in `messages` | **top-level `system=` param** | role in `messages` |
| **Temperature** | `options.temperature` | `temperature` | `temperature` (0–1 range) | ignored by `deepseek-reasoner` |
| **Max tokens** | `options.num_predict` | `max_tokens` / `max_completion_tokens` | `max_tokens` — **required** | `max_tokens` |
| **Timeouts** | caller supplies `httpx.Timeout` | SDK `timeout=` | SDK `timeout=` | SDK `timeout=` |
| **Retries** | **none** | SDK auto-retries by default | SDK auto-retries by default | SDK default |
| **Streaming** | dict chunks `chunk["message"]["content"]` | `chunk.choices[0].delta.content` | typed events / `stream.text_stream` inside a context manager | OpenAI-shaped |
| **Errors** | `httpx.ConnectError`, `ResponseError` | `APIStatusError`, `RateLimitError` | `APIStatusError`, `RateLimitError`, `OverloadedError` | OpenAI-shaped |
| **Rate limits** | none (you own the GPU) | RPM/TPM tiers, 429 | RPM/ITPM/OTPM, 429 | 429 |
| **Context limit** | `num_ctx` — see warning below | model-dependent, large | large | large |
| **Token usage** | `prompt_eval_count` / `eval_count` | `usage.prompt_tokens` / `completion_tokens` | `usage.input_tokens` / `output_tokens` | OpenAI-shaped |
| **Model names** | `qwen2.5:14b` (tag syntax) | `gpt-4o-mini` etc. | `claude-sonnet-5` etc. | `deepseek-chat`, `deepseek-reasoner` |
| **JSON mode** | `format="json"` | `response_format={"type": "json_object"}` | **no JSON mode** — tools or prefill | `response_format` |

**All sixteen rows belong inside `llm/providers/*.py`.** None should ever be
visible to `rag/answer.py`, `api/services/ask_service.py`, or the frontend.

### Three differences needing explicit design attention

**(a) Anthropic streaming is a context manager.** The recommended API is
`with client.messages.stream(...) as s: for text in s.text_stream:`. The SSE
layer pulls one token per `run_in_threadpool` call and may abandon the iterator
if the browser disconnects. The Anthropic provider's `stream()` must be a
generator function that **owns** the `with` block and handles `GeneratorExit`
cleanly, or HTTP connections leak. This is the trickiest implementation detail
in the migration.

**(b) No cancellation exists today.** `handle_ask_stream`
(`api/services/ask_service.py:162-179`) drains to completion regardless of
client disconnect. Free locally; **billable** on a cloud provider. Worth adding
a disconnect check as a follow-up — noted, not required for phase 1.

**(c) `num_ctx` is not set anywhere.** `rag/generator.py:271, 282` pass only
`temperature` and `num_predict`. Ollama applies a default context window unless
the model's Modelfile overrides it — and `ollama show qwen2.5:14b --parameters`
on the dev box returned **empty**, meaning no override is set. The prompt is
system (~50 lines) + one-shot (~30 lines) + SQL block (up to 100 rows) + 8
chunks, which may be **silently truncating** today. This is a hypothesis worth
testing independently of this migration, and a good argument for normalizing an
explicit context budget in the provider layer.

### The one thing the abstraction cannot fully hide

`_SYSTEM_PROMPT`, `_ONESHOT_USER/_ASSISTANT`, and the trailing `## Reminder`
(`rag/generator.py:190-195`) are **tuned for a 14B local model**. The code
comment says so explicitly:

> *"Without this, the qwen2.5:14b answer reliably falls into structured-prose
> mode with zero [N] markers despite the system-prompt rules."*

GPT-4o and Claude will likely cite correctly without the one-shot, making it
roughly 400 wasted input tokens per request. The `n_markers` field in
`observability/query_log.py:106` is exactly the instrument to measure this per
provider. Structurally clean; behaviorally you will want per-provider prompt
tuning later. Do not solve it in phase 1.

---

## 7. Preserving existing behavior

With `LLM_PROVIDER=ollama`, the factory returns `OllamaProvider`, which uses the
same `ollama.Client`, the same host, the same `httpx.Timeout(5, 120)`, the same
four-message array, and the same
`options={"temperature": 0.1, "num_predict": 1024}`.

**The byte-for-byte identical request reaches Ollama.** Only the Python call
stack differs.

Because `LLM_MODEL` defaults to `OLLAMA_MODEL` and `LLM_PROVIDER` defaults to
`"ollama"`, the existing `.env` needs **no edit at all** — the system behaves
exactly as it does today until `LLM_PROVIDER` is deliberately set.

### Is `change config → restart → new provider` realistic?

**Yes**, with two honest caveats:

1. **Restart is required.** `_get_client()` is a process-lifetime singleton
   (`rag/generator.py:45`) and `config.py` reads env once at import (`:19`).
   No hot reload. This matches the stated workflow.
2. **The provider's SDK must be installed.** Switching to `LLM_PROVIDER=openai`
   without `openai` in the venv fails at import. Handle it as a clear startup
   error naming the missing extra — never a silent fallback to another provider.

---

## 8. Architecture before and after

### A. Current

```
api/routers/ask.py ──▶ ask_service ──▶ rag/answer.ask()
                                          ├─▶ query_router  ──[ollama.Client]──▶ Ollama ▶ Qwen
                                          ├─▶ sql_context     (neutral)
                                          ├─▶ retriever       (neutral, local models)
                                          └─▶ generator
                                                ├─ prompt+citations (neutral)
                                                └─ _call_ollama ──[ollama.Client]──▶ Ollama ▶ Qwen
```

Two SDK-bound call sites, each with its own client, timeouts, and response parsing.

### B. Proposed

```
api/routers/ask.py ──▶ ask_service ──▶ rag/answer.ask()      ← all unchanged in behavior
                                          ├─▶ query_router ──┐
                                          ├─▶ sql_context    │  (neutral)
                                          ├─▶ retriever      │  (neutral)
                                          └─▶ generator ─────┤
                                                             ▼
                                                  llm.get_provider(profile)
                                                             │
                                    ┌────────────┬───────────┼────────────┬─────────────┐
                                    ▼            ▼           ▼            ▼             ▼
                              OllamaProvider  OpenAI    Anthropic    DeepSeek     (future)
                                    │            │           │            │
                                 Ollama      api.openai   api.anthropic  api.deepseek
                                    ▼
                                  Qwen
```

### C. Files to modify — 7 (two substantive, five one-liners or docs)

`rag/generator.py` · `rag/query_router.py` · `config.py` ·
`api/services/ask_service.py` · `rag/answer.py` · `.env.example` ·
`pyproject.toml`  *(plus `scripts/preflight_check.py`, optional)*

### D. Files that remain unchanged

`api/routers/ask.py` · `api/schemas/ask.py` · `api/main.py` · `rag/retriever.py` ·
`rag/sql_context.py` · `rag/meeting_lookup.py` · `observability/query_log.py` ·
`pipeline/**` · `tests/test_eval.py` · `eval/eval_set.jsonl` ·
**the entire `frontend/` tree**

### E. New files — 8, all additive

`llm/__init__.py` · `llm/base.py` · `llm/errors.py` · `llm/factory.py` ·
`llm/providers/{ollama,openai,anthropic,deepseek}.py`

---

## 9. Example code — design illustration only

> These snippets are illustrative. They are **not** written into the project.

### Ollama — wraps today's exact behavior

```python
# llm/providers/ollama.py
import httpx
from ollama import Client

class OllamaProvider:
    provider = "ollama"

    def __init__(self, model, base_url, connect_timeout, read_timeout):
        self.model = model
        self._c = Client(host=base_url, timeout=httpx.Timeout(
            connect=connect_timeout, read=read_timeout,
            write=read_timeout, pool=read_timeout))

    def _msgs(self, system, messages):
        return [{"role": "system", "content": system}, *messages]

    def complete(self, *, system, messages, temperature, max_tokens, json_mode=False):
        kw = {"format": "json"} if json_mode else {}
        r = self._c.chat(model=self.model, messages=self._msgs(system, messages),
                         stream=False,
                         options={"temperature": temperature, "num_predict": max_tokens},
                         **kw)
        return LLMResult(text=r["message"]["content"], model=self.model,
                         provider=self.provider,
                         prompt_tokens=r.get("prompt_eval_count"),
                         completion_tokens=r.get("eval_count"))

    def stream(self, *, system, messages, temperature, max_tokens):
        for chunk in self._c.chat(model=self.model, messages=self._msgs(system, messages),
                                  stream=True,
                                  options={"temperature": temperature,
                                           "num_predict": max_tokens}):
            if tok := chunk.get("message", {}).get("content", ""):
                yield tok
```

### OpenAI

```python
# llm/providers/openai.py
from openai import OpenAI

class OpenAIProvider:
    provider = "openai"
    DEFAULT_BASE_URL = None          # SDK default

    def __init__(self, model, api_key, base_url=None, timeout=120.0):
        self.model = model
        self._c = OpenAI(api_key=api_key,
                         base_url=base_url or self.DEFAULT_BASE_URL,
                         timeout=timeout)

    def _msgs(self, system, messages):
        return [{"role": "system", "content": system}, *messages]

    def complete(self, *, system, messages, temperature, max_tokens, json_mode=False):
        kw = {"response_format": {"type": "json_object"}} if json_mode else {}
        r = self._c.chat.completions.create(
            model=self.model, messages=self._msgs(system, messages),
            temperature=temperature, max_tokens=max_tokens, **kw)
        u = r.usage
        return LLMResult(text=r.choices[0].message.content, model=self.model,
                         provider=self.provider,
                         prompt_tokens=u.prompt_tokens if u else None,
                         completion_tokens=u.completion_tokens if u else None)

    def stream(self, *, system, messages, temperature, max_tokens):
        for chunk in self._c.chat.completions.create(
                model=self.model, messages=self._msgs(system, messages),
                temperature=temperature, max_tokens=max_tokens, stream=True):
            if chunk.choices and (tok := chunk.choices[0].delta.content):
                yield tok
```

### DeepSeek — OpenAI-wire-compatible, so nearly free

```python
# llm/providers/deepseek.py
class DeepSeekProvider(OpenAIProvider):
    provider = "deepseek"
    DEFAULT_BASE_URL = "https://api.deepseek.com"
    # Note: deepseek-reasoner ignores `temperature` and emits a separate
    # `reasoning_content` field — strip it here so callers never see it.
```

### Anthropic — note `system=` is not a message

```python
# llm/providers/anthropic.py
from anthropic import Anthropic

class AnthropicProvider:
    provider = "anthropic"

    def __init__(self, model, api_key, base_url=None, timeout=120.0):
        self.model = model
        self._c = Anthropic(api_key=api_key, base_url=base_url, timeout=timeout)

    def complete(self, *, system, messages, temperature, max_tokens, json_mode=False):
        r = self._c.messages.create(
            model=self.model, system=system, messages=messages,   # ← system split out
            temperature=temperature, max_tokens=max_tokens)       # ← required, not optional
        return LLMResult(text=r.content[0].text, model=self.model,
                         provider=self.provider,
                         prompt_tokens=r.usage.input_tokens,
                         completion_tokens=r.usage.output_tokens)

    def stream(self, *, system, messages, temperature, max_tokens):
        # The generator OWNS the context manager so the HTTP stream closes
        # even if the SSE consumer abandons iteration mid-answer.
        with self._c.messages.stream(
                model=self.model, system=system, messages=messages,
                temperature=temperature, max_tokens=max_tokens) as s:
            yield from s.text_stream
```

### The resulting call site in `rag/generator.py` — the whole point

```python
def _call_llm(prompt: str, model: str | None, stream: bool):
    provider = get_provider("generate")
    messages = [
        {"role": "user",      "content": _ONESHOT_USER},
        {"role": "assistant", "content": _ONESHOT_ASSISTANT},
        {"role": "user",      "content": prompt},
    ]
    kw = dict(system=_SYSTEM_PROMPT, messages=messages,
              temperature=config.LLM_TEMPERATURE,
              max_tokens=config.LLM_MAX_TOKENS)
    return provider.stream(**kw) if stream else provider.complete(**kw).text
```

`generate()`'s signature, its return dict, and every caller in `rag/answer.py`
stay exactly as they are.

---

## 10. Migration plan

The eval harness is the safety net throughout: `tests/test_eval.py` hits `/ask`
against `eval/eval_set.jsonl`, and `scripts/audit_query_log.py` reads the
`n_markers` citation signal. **Baseline is 8/9** — record it before starting.

### Step 0 — Baseline (no code changes)

- **Do:** `uv run --no-sync python tests/test_eval.py`; save the score and a
  `data/query_log.jsonl` snapshot.
- **Why:** every later step is judged against this number.
- **Rollback:** n/a.

### Step 1 — Add the `llm/` package, wire nothing

- **Files:** 8 new under `llm/`; `config.py` gains the `LLM_*` block with Ollama
  fallbacks.
- **Why:** additive only; the running app cannot regress because nothing imports
  it yet.
- **Test:** `uv run python -c "from llm import get_provider; print(get_provider('generate').model)"`
  → `qwen2.5:14b`. Re-run eval: must still be 8/9.
- **Rollback:** delete `llm/`, revert the config block. One commit.

### Step 2 — Point `rag/generator.py` at the provider *(the real change)*

- **Files:** `rag/generator.py` only. Delete `_get_client()`; replace the
  `_call_ollama` body. Keep `_SYSTEM_PROMPT`, the one-shot,
  `_build_prompt_and_citations`, and `generate()`'s signature untouched.
- **Why:** removes the SDK from the generation path.
- **Test:** eval must return **exactly 8/9** — same cases passing, same cases
  failing. Then `POST /ask?stream=true` in the browser and confirm tokens flow
  and citation cards render. Diff the `n_markers` distribution against the
  Step-0 snapshot: it should be statistically unchanged, since the wire request
  is identical.
- **Rollback:** `git revert` one commit. `LLM_PROVIDER` is still `ollama`, so
  nothing external changed.

### Step 3 — Point `rag/query_router.py` at the provider

- **Files:** `rag/query_router.py`. Use `get_provider("route")` with the 2 s/5 s
  profile; keep the `except → "HYBRID"` fallback verbatim.
- **Why:** last SDK import on the serving path.
- **Test:** run `python rag/query_router.py` (built-in 12-query CLI at
  `:492-514`) and diff routes against a pre-change run. Then eval again.
- **Rollback:** revert one commit.

> **At this point Neo is provider-agnostic and still 100 % Ollama.**
> This is a safe stopping point.

### Step 4 — Clean the model-name leaks

- **Files:** `api/services/ask_service.py:64,190`; `rag/answer.py:138,173,434`;
  `.env.example`; `pyproject.toml`.
- **Why:** so `query_log.jsonl` and the UI footer report the truth after a switch.
- **Test:** eval; confirm the `model` field in fresh log lines.
- **Rollback:** revert one commit.

### Step 5 — Add OpenAI + DeepSeek

- **Files:** `llm/providers/openai.py`, `llm/providers/deepseek.py`,
  `pyproject.toml` extra. **No RAG files touched.**
- **Why:** first real second provider; DeepSeek rides the same wire format for
  roughly ten extra lines.
- **Test:** set `LLM_PROVIDER=openai` plus the key in `.env`, restart, re-run
  eval. Expect a *different* score — cloud models may cite better or worse.
  Compare `n_markers`. Then flip back to `ollama` and confirm a return to 8/9.
- **Rollback:** `LLM_PROVIDER=ollama` in `.env` and restart. **No code revert
  needed** — this is the payoff.

### Step 6 — Add Anthropic

- **Files:** `llm/providers/anthropic.py` only.
- **Why:** exercises the `system=` split and context-manager streaming — the two
  cases the interface was designed for.
- **Test:** eval under `LLM_PROVIDER=anthropic`; specifically test a **mid-stream
  browser disconnect** and confirm the HTTP stream closes.
- **Rollback:** env flip.

### Step 7 (optional, later) — Hardening

Typed errors mapped to HTTP codes in `ask_service`; `prompt_tokens` /
`completion_tokens` into `query_log`; client-disconnect cancellation; then, only
if desired, migrate `pipeline/extractor.py` (the `format=json` problem).

---

## 11. Direct answers

**1. How tightly coupled is Neo v2 to Ollama today?**
**Loosely — better than the file count suggests.** Two serving-path files
(`rag/generator.py`, `rag/query_router.py`), ~70 lines total, plus two hardcoded
`"qwen2.5:14b"` literals in `ask_service.py` and three `config.OLLAMA_MODEL`
references in `answer.py`. The pipeline extractors are separately coupled but
are offline batch jobs outside this scope. The existing layering already did
most of the work.

**2. What is the minimum code that needs to change?**
Two functions: `rag/generator.py:243-284` (`_call_ollama`) and
`rag/query_router.py:435-459` (`_llm_route`), plus deleting their two client
constructors. Add a `config.py` block. Everything else is one-line hygiene.

**3. Can `/ask` remain mostly unchanged?**
**Entirely unchanged.** `api/routers/ask.py` and `api/schemas/ask.py` need zero
edits. `ask_service.py` needs two string literals corrected — cosmetic, not
structural. The SSE frame taxonomy (`meta` / `token` / `done` / `error`) is
already provider-neutral.

**4. Can the RAG/retrieval pipeline remain unchanged?**
**Yes, completely.** `retriever.py`, `sql_context.py`, and `meeting_lookup.py`
make zero LLM calls — retrieval runs on fastembed ONNX, Qdrant RRF, and a local
BGE cross-encoder. `rag/answer.py` changes only three one-line constants and
some docstrings; its control flow is untouched.

**5. Can Ollama/Qwen continue working?**
**Yes, as the default.** With `LLM_PROVIDER` defaulting to `"ollama"` and
`LLM_MODEL` falling back to `OLLAMA_MODEL`, the current `.env` needs no edit and
the wire request to Ollama is byte-identical.

**6. How to switch to OpenAI?** Set `LLM_PROVIDER=openai`,
`LLM_MODEL=gpt-4o-mini`, `LLM_API_KEY=…` in `.env`; restart. One-time: install
the `openai` extra.

**7. How to switch to Claude?** Set `LLM_PROVIDER=anthropic`,
`LLM_MODEL=claude-sonnet-5`, `LLM_API_KEY=…`; restart. `anthropic>=0.28` is
already in `pyproject.toml:66` — installed and currently unused.

**8. How to switch to DeepSeek?** Set `LLM_PROVIDER=deepseek`,
`LLM_MODEL=deepseek-chat`, `LLM_API_KEY=…`; restart. Reuses the OpenAI SDK with
a different base URL.

**9. Code changes or configuration only?**
**Configuration only — after the one-time migration**, provided the target
provider's module exists. Adding a *new* provider later means one new file in
`llm/providers/`; switching among existing ones is env plus restart. No hot
reload: `config.py` reads env at import and clients are process-lifetime
singletons.

**10. Any provider-specific features preventing a clean abstraction?**
Four, none blocking:

- **`format=json`** (`pipeline/extractor.py:211`,
  `pipeline/initiative_extractor.py:93`) — Ollama's structured-output flag.
  OpenAI has an analogue; **Anthropic has none**. Confined to the offline
  pipeline, which stays on Ollama.
- **Anthropic's `system=` parameter** — a genuine API-shape divergence, solved
  by splitting `system` from `messages` in the interface.
- **Anthropic's context-manager streaming** — solved by having the provider's
  generator own the `with` block; needs care around `GeneratorExit`.
- **The one-shot + `## Reminder` prompt** (`rag/generator.py:209-236, 190-195`)
  — explicitly tuned for `qwen2.5:14b`'s citation weakness. Behavioral, not
  structural: it will not break other providers, just waste tokens on models
  that do not need it. The `n_markers` audit field is the right instrument for
  tuning this per provider later.

Nothing in the current code blocks the design. There is no tool-calling, no
logprobs, no Ollama `keep_alive`, and no embeddings on the serving LLM path.

---

## 12. Adjacent issues found during analysis

Neither blocks this migration; both get easier once the provider layer exists.

1. **Missing `num_ctx`** — see §6(c). Possible silent prompt truncation on the
   current Ollama setup. Worth testing independently.
2. **No error handling in `handle_ask`** — `api/services/ask_service.py:83-105`
   has no `try/except`, so a dead LLM yields a bare 500. This gets worse with
   cloud 429s and 401s. Addressed by §4's error taxonomy in Step 7.
3. **Dead Claude config** — `config.py:83-88` and `pyproject.toml:64-67` define
   `ANTHROPIC_API_KEY` / `CLAUDE_MODEL` and install the `anthropic` SDK, but
   nothing imports it. Prior reviews already flagged this
   (`specs/neo_v2_rag_review.md:42`, `specs/neo_v2_architecture_review.md:39`).
   This migration finally makes that config meaningful.
4. **Stale `README.md:22`** — lists Ollama/`qwen2.5:14b` as "local LLM
   (extraction)" only, understating its role in `/ask`.

---

## 13. Implementation record (2026-08-13)

Shipped: steps 1–4 of §10, plus a **Gemini** provider (not in the original
design) and the step-7 error mapping, pulled forward because the first live
Gemini run turned a 429 into a raw 500.

### Verification

| Check | Result |
|---|---|
| `tests/test_eval.py`, `LLM_PROVIDER=ollama` | **8/9 — baseline held.** Same 8 passing, same `ask-009` failing |
| `rag/query_router.py` CLI, `LLM_PROVIDER=gemini` | 12/12 routed; LLM-classified rows returned real labels, not the hybrid fallback |
| `rag/answer.ask()`, gemini, sql route | Answer + citation `[1]` + `model: gemini-3.7-flash` |
| `rag/answer.ask(stream=True)`, gemini | First token 5.5 s, citations intact |
| `tests/test_eval.py`, `LLM_PROVIDER=gemini` | **Blocked — free-tier quota, see below.** Not a code failure |

### Where reality differed from the design

**(a) Gemini was added, and it is the DeepSeek trick.** Google ships an
OpenAI-wire-compatible endpoint at
`https://generativelanguage.googleapis.com/v1beta/openai/`, so
`GeminiProvider` subclasses `OpenAIProvider` with a different base URL and no
`google-genai` dependency. §5's provider table generalizes: **gemini, openai
and deepseek all ride one SDK and one streaming decoder.**

**(b) `openai>=3` runs on `httpx2`, the rest of the tree on `httpx` 0.28.**
A hand-built `httpx.Timeout` is the wrong type for that SDK. Both cloud
providers therefore use the SDK's own re-export (`openai.Timeout`,
`anthropic.Timeout`) instead of importing httpx directly. §9's illustrative
snippets are wrong on this point.

**(c) The router's 5-token cap is unsafe on reasoning models.** §1 preserved
`num_predict: 5`. On Gemini 2.5/3.x thinking tokens come out of the same
budget, so a 5-token cap returns *empty* — and `route_map.get(label,
"hybrid")` would silently route every ambiguous query to hybrid, doubling
retrieval work with no visible error. Fixed three ways:
`LLM_ROUTER_MAX_TOKENS=16`; `_parse_label()` regex-searches for the label
instead of exact-matching; and the `"route"` profile defaults
`LLM_ROUTER_REASONING_EFFORT=none` so the classifier never pays for thinking.

**(d) Same trap on the generation path, handled differently.** `complete()`
and `stream()` raise a named error when the provider returns no text with
`finish_reason == "length"`, rather than handing a trustee a blank answer.
§6(c)'s `num_ctx` concern has an output-side twin.

**(e) `get_provider()` takes an optional `model` override.** Needed to keep
`rag/answer.py --model` meaningful — it is now the way to A/B a stronger model
against the configured one without touching `.env`. The provider (and so key
and endpoint) still always comes from `LLM_PROVIDER`.

**(f) Config gained `LLM_REASONING_EFFORT`, `LLM_ROUTER_REASONING_EFFORT`,
`LLM_ROUTER_MAX_TOKENS`, `LLM_ROUTER_CONNECT_TIMEOUT`** beyond §5's block.
Dead `ANTHROPIC_API_KEY` / `CLAUDE_MODEL` deleted (§12.3) — nothing imported
them.

### Blocker for a Gemini pilot: free-tier quota

Live 429 body from `gemini-3.7-flash`:

```
quotaId:    GenerateRequestsPerDayPerProjectPerModel-FreeTier
quotaValue: 20
metric:     generativelanguage.googleapis.com/generate_content_free_tier_requests
```

**20 requests per day, per model.** Each `/ask` can spend two (router
classification + generation), so the free tier is roughly **10 trustee
questions per day** — it cannot support a pilot. `gemini-3.1-pro-preview`
returns 429 immediately on this key, so the "escalate to Pro" path also needs
a paid tier.

Also note `gemini-2.5-flash` now 404s for new keys ("no longer available to
new users"), which is why §5's example model IDs are already stale. Take live
IDs from `client.models.list()`.

### Model selection bake-off (2026-08-13, paid tier)

Same 9 eval cases, one API process per model, `LLM_MAX_TOKENS=2048`,
`EVAL_TIMEOUT_S=180`. **Chosen: `gemini-3.5-flash`.**

| Model | Score | Wall | Infra errors | Verdict |
|---|---|---|---|---|
| `gemini-3.5-flash` | **8/9** | **130 s** | none | **Chosen.** Fastest, cleanest, matches the Ollama baseline count |
| `gemini-3.6-flash` | 6/9 | 171 s | none | 3 route mismatches — classifies `rag` questions as `hybrid` |
| `gemini-3.7-flash` | 2/9 | 973 s | 5×503 + 2 timeouts | Capacity-starved: "this model is currently experiencing high demand". Not a quality result |

Newest ≠ best here. 3.7 never got to show quality — Google is rationing it, and
`/ask` cannot sit on a model that refuses service. Revisit when capacity settles.

**Gemini beats Ollama on `ask-009`** (the bond-refunding case qwen2.5:14b has
never passed) and is the only provider to do so.

**The score is not stable at ±1 case.** Two consecutive `gemini-3.5-flash` runs
both scored 8/9 but failed *different* cases (`ask-005`, then `ask-003`). Cloud
output varies run to run even at `temperature=0.1`. Treat a single eval run as
a noisy sample: a 1-case delta is not a regression, and 8/9 should be read as
"8–9 of 9". Ollama's 8/9 was stable — the same case failed every time.

### Thinking tokens were eating the answer (fixed) — 8/9 to **9/9**

Reported as "answers are too short". `Tell about HCC` came back 374 chars,
cut off mid-sentence at "HCC partnered". Measured on the real RAG prompt:

| `LLM_REASONING_EFFORT` | `LLM_MAX_TOKENS` | finish | visible tokens | chars |
|---|---|---|---|---|
| (default) | 2048 | **length** | **82** | 423 |
| none | 2048 | stop | 508 | 2529 |
| low | 4096 | stop | 410 | 1994 |
| (default) | 8192 | stop | 442 | 2072 |
| **none** | **8192** | stop | **568** | **2786** |

**Only 82 of 2048 tokens reached the answer** — thinking spent ~96% of the
budget before the first visible token. This is §6's `max_tokens` warning
biting in its partial form: not a blank answer (which `_empty_answer_error`
already catches) but a *plausible-looking* one that stops mid-word.

Settled on `LLM_REASONING_EFFORT=none` + `LLM_MAX_TOKENS=8192`. Turning
thinking off wins on every axis at once: longest answers, lowest token spend,
lowest latency. Neo's generation step is grounded synthesis over
already-retrieved context — the reasoning was being spent re-deriving what
retrieval had already established. Same finding as the router, where
`effort=low|medium` measurably *misrouted* queries that `none` got right.

**This took the eval from 8/9 to 9/9 — the first clean sweep on this set,
better than the qwen2.5:14b baseline has ever scored.** The failures were
never model quality; truncated answers simply could not contain the strings
`must_contain_any` was looking for. Any eval run before this fix understated
Gemini's real quality.

`max_tokens` is a ceiling, not a target — raising it does not pad short
answers. Measured after the change: off-topic refusal 438 chars / 1.0 s,
simple SQL lookup 355 chars / 1.2 s, broad "tell me everything" 3611 chars /
11.2 s. `Tell about HCC` went 374 → 1288 chars with 8 citations, and got
*faster* (8.5 s → 3.7 s) because the budget stopped going to thinking.

`llm/providers/openai.py` now logs a TRUNCATED warning whenever
`finish_reason == "length"` with visible text. A cut-off answer is worse than
an error — it looks finished, and the trustee has no way to tell it from the
model's real conclusion.

### Silent router degradation (fixed)

`_llm_route`'s `except Exception -> "HYBRID"` had no log line. On localhost
Ollama it fired ~never; against a cloud provider on a 5 s budget with zero
retries, a trip degrades routing with **no symptom other than a route that
looks like a model opinion.** `rag/query_router.py` now logs a warning there.
That log is what proved the second `ask-005` result was genuine model
non-determinism (0 fallbacks, 0 LLM errors, 9×HTTP 200) rather than a
swallowed network failure — the two are indistinguishable without it.

### Latency

`gemini-3.7-flash` took ~21 s non-streaming and 5.5 s to first token on a SQL
route — slower than expected because thinking is on by default for generation.
`tests/test_eval.py` uses a 60 s client timeout and hit it on the longer RAG
cases. If Gemini becomes the pilot provider, either set
`LLM_REASONING_EFFORT=low|none` or raise the eval timeout.
