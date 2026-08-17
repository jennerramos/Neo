"""
Neo v2 — Central Configuration

All settings live here. Pipeline code imports from this module only —
no raw os.getenv() calls scattered across the codebase.

Usage:
    from config import cfg

    engine = create_engine(cfg.DATABASE_URL)
    model = WhisperModel(cfg.WHISPER_MODEL, device=cfg.DEVICE)
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (works regardless of where the script is called from)
load_dotenv(Path(__file__).resolve().parent / ".env")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent

DATA_DIR       = PROJECT_ROOT / "data"
RAW_DIR        = DATA_DIR / "raw"        # downloaded VTT files, per school slug
PROCESSED_DIR  = DATA_DIR / "processed"  # meeting.txt, meeting.json, chunks.jsonl
AUDIO_DIR      = DATA_DIR / "audio"      # temp audio (auto-deleted after ASR)
EXPORTS_DIR    = DATA_DIR / "exports"    # PDF / Word exports for trustees

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DATABASE_URL: str = os.environ["DATABASE_URL"]
SQL_ECHO: bool = os.getenv("SQL_ECHO", "false").lower() == "true"

# ---------------------------------------------------------------------------
# YouTube / Google
# ---------------------------------------------------------------------------

YOUTUBE_API_KEY: str = os.getenv("YOUTUBE_API_KEY", "")

# Browser to pull cookies from for yt-dlp (helps access age-restricted or
# authenticated content and avoids bot detection).
# Options: "firefox" | "chrome" | "chromium" | "edge" | "" (disabled)
YT_DLP_COOKIES_BROWSER: str = os.getenv("YT_DLP_COOKIES_BROWSER", "firefox")

# Comma-separated proxy URLs for IP rotation in caption_downloader.
# On IpBlocked the downloader cycles to the next proxy automatically.
#
# Webshare rotating residential (single URL, auto-rotates every request):
#   PROXY_LIST=http://user:pass@p.webshare.io:80
#
# Multiple proxies (round-robin):
#   PROXY_LIST=http://1.2.3.4:8080,http://5.6.7.8:3128
#
# Tor (free, keep Tor Browser open):
#   PROXY_LIST=socks5h://127.0.0.1:9150
#
PROXY_LIST: list = [p.strip() for p in os.getenv("PROXY_LIST", "").split(",") if p.strip()]

# ---------------------------------------------------------------------------
# ASR — WhisperX
# ---------------------------------------------------------------------------

WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "large-v3")
DEVICE: str         = os.getenv("DEVICE", "cuda")           # 'cuda' | 'cpu'
COMPUTE_TYPE: str   = os.getenv("COMPUTE_TYPE", "float16")  # 'float16' | 'int8'

# Diarization (pyannote.audio)
HUGGINGFACE_TOKEN: str = os.getenv("HUGGINGFACE_TOKEN", "")

# ---------------------------------------------------------------------------
# LLM — Ollama (local; offline extraction pipeline)
# ---------------------------------------------------------------------------
#
# Kept as the local-Ollama defaults that BOTH neutral namespaces below fall
# back to: LLM_* (serving) and PIPELINE_LLM_* (extraction). Setting these two
# alone still gives you today's fully-local behavior on every path.
#
OLLAMA_HOST: str  = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")

# ---------------------------------------------------------------------------
# LLM — provider-neutral (serving path: /ask generation + query routing)
# ---------------------------------------------------------------------------
#
# Switching providers is env + restart, no code change:
#
#   LLM_PROVIDER=ollama                        # local, default
#   LLM_PROVIDER=gemini    LLM_MODEL=gemini-2.5-flash   LLM_API_KEY=...
#   LLM_PROVIDER=openai    LLM_MODEL=gpt-4o-mini        LLM_API_KEY=...
#   LLM_PROVIDER=anthropic LLM_MODEL=claude-sonnet-5    LLM_API_KEY=...
#   LLM_PROVIDER=deepseek  LLM_MODEL=deepseek-chat      LLM_API_KEY=...
#
# Every default below falls back to today's Ollama behavior, so an existing
# .env with no LLM_* lines keeps working byte-identically.
#
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
LLM_MODEL:    str = os.getenv("LLM_MODEL", OLLAMA_MODEL)
LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "")   # "" = provider's own default
LLM_API_KEY:  str = os.getenv("LLM_API_KEY", "")    # never a literal default

LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.1"))
LLM_MAX_TOKENS:  int   = int(os.getenv("LLM_MAX_TOKENS", "1024"))

# Gemini/OpenAI reasoning models spend thinking tokens out of the SAME
# max_tokens budget as the visible answer, so a cap tuned for qwen2.5:14b can
# be consumed before the first answer token. "" = don't send the parameter.
#   gemini: none | low | medium | high
LLM_REASONING_EFFORT: str = os.getenv("LLM_REASONING_EFFORT", "").strip().lower()

# The query-router classifier is a one-word call. Letting a reasoning model
# think there burns the budget (and the money) for no gain, so it defaults to
# "none" independently of the generation setting above.
LLM_ROUTER_REASONING_EFFORT: str = os.getenv(
    "LLM_ROUTER_REASONING_EFFORT", "none"
).strip().lower()
LLM_ROUTER_MAX_TOKENS: int = int(os.getenv("LLM_ROUTER_MAX_TOKENS", "16"))

# Two timeout profiles. Generation can legitimately take ~60s; the router
# classifier is a one-word call that should fail fast so a hung LLM can't wedge
# every /ask.
#
# "Fast" is relative to the provider, though, and the old 2s/5s router budget
# was measured against Ollama on localhost. A cloud provider answers the same
# one-word question in 4.0-5.2s, which straddled the 5s read budget and made
# routing a coin toss: on a timeout `_llm_route` falls back to "hybrid", so
# off-topic questions never reached NONE and specific lookups never reached
# SQL. It read like model nondeterminism and was a stopwatch.
#
# Provider latency also moves hour to hour: the same call measured 4.0-5.2s
# early on 2026-08-16 and 7.5-10.7s later the same day. 20s covers both regimes
# with margin. It costs nothing in the normal case — a classifier that answers
# in 4s returns in 4s — and stays an order of magnitude below generation's
# budget. Drop it back to ~5s only if you are serving from a local Ollama.
LLM_CONNECT_TIMEOUT: float = float(os.getenv("LLM_CONNECT_TIMEOUT", "5"))
LLM_READ_TIMEOUT:    float = float(os.getenv("LLM_READ_TIMEOUT", "120"))
LLM_ROUTER_CONNECT_TIMEOUT: float = float(os.getenv("LLM_ROUTER_CONNECT_TIMEOUT", "5"))
LLM_ROUTER_READ_TIMEOUT:    float = float(os.getenv("LLM_ROUTER_READ_TIMEOUT", "20"))

# ---------------------------------------------------------------------------
# LLM — extraction pipeline (offline batch: extractor + initiative_extractor)
# ---------------------------------------------------------------------------
#
# A SEPARATE namespace from LLM_* above, and it deliberately does NOT inherit
# from it. Unset PIPELINE_LLM_* falls back to OLLAMA_HOST/OLLAMA_MODEL — never
# to LLM_* — so a .env that points /ask at a paid provider does not silently
# move extraction there too. That matters here more than on the serving path:
# /ask is one call per question, extraction is N candidate windows x 4
# extraction types per meeting across the whole corpus.
#
#   PIPELINE_LLM_PROVIDER=ollama                                  # default
#   PIPELINE_LLM_PROVIDER=gemini PIPELINE_LLM_MODEL=gemini-3.5-flash
#   PIPELINE_LLM_PROVIDER=openai PIPELINE_LLM_MODEL=gpt-4o-mini
#
# anthropic is NOT usable here: it has no JSON mode and llm/providers/anthropic
# raises LLMConfigError rather than silently returning prose.
#
PIPELINE_LLM_PROVIDER: str = os.getenv("PIPELINE_LLM_PROVIDER", "ollama").strip().lower()
PIPELINE_LLM_MODEL:    str = os.getenv("PIPELINE_LLM_MODEL", OLLAMA_MODEL)
PIPELINE_LLM_BASE_URL: str = os.getenv("PIPELINE_LLM_BASE_URL", "")
PIPELINE_LLM_API_KEY:  str = os.getenv("PIPELINE_LLM_API_KEY", "")

# Extraction runs at temperature 0 — structured JSON, no sampling wanted. This
# is NOT LLM_TEMPERATURE's 0.1; both extractors sent 0.0 pre-refactor.
PIPELINE_LLM_TEMPERATURE: float = float(os.getenv("PIPELINE_LLM_TEMPERATURE", "0"))

# 2048 = the num_predict both extractors sent pre-refactor. Truncation is
# nastier here than on the serving path: a cut-off answer is a visibly short
# sentence, but a cut-off JSON array fails to parse, yields zero rows, and
# reads downstream as "this meeting had no votes". pipeline/llm_json.py logs a
# loud TRUNCATED warning when a provider reports hitting this cap.
PIPELINE_LLM_MAX_TOKENS: int = int(os.getenv("PIPELINE_LLM_MAX_TOKENS", "2048"))

# Reasoning models spend thinking tokens out of MAX_TOKENS before emitting the
# first JSON character, so effort defaults off — same reason the query router
# pins it to "none".
PIPELINE_LLM_REASONING_EFFORT: str = os.getenv(
    "PIPELINE_LLM_REASONING_EFFORT", "none"
).strip().lower()

# Read timeout covers the slower of the two callers (initiative_extractor sent
# 150s, extractor 120s). Retries default higher than the serving path's 2:
# extraction fans out enough calls to hit cloud rate limits that a single
# /ask never sees.
PIPELINE_LLM_CONNECT_TIMEOUT: float = float(os.getenv("PIPELINE_LLM_CONNECT_TIMEOUT", "5"))
PIPELINE_LLM_READ_TIMEOUT:    float = float(os.getenv("PIPELINE_LLM_READ_TIMEOUT", "150"))
PIPELINE_LLM_MAX_RETRIES:     int   = int(os.getenv("PIPELINE_LLM_MAX_RETRIES", "5"))

# ---------------------------------------------------------------------------
# Embeddings — nomic-embed-text
# ---------------------------------------------------------------------------

EMBED_MODEL: str = os.getenv("EMBED_MODEL", "nomic-embed-text")
EMBED_DIM: int   = int(os.getenv("EMBED_DIM", "768"))

# ---------------------------------------------------------------------------
# Vector DB — Qdrant
# ---------------------------------------------------------------------------

QDRANT_HOST: str       = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT: int       = int(os.getenv("QDRANT_PORT", "6333"))
QDRANT_COLLECTION: str = os.getenv("QDRANT_COLLECTION", "neo_v2_chunks")

# Embedded / local mode — set this path to skip the Docker server entirely.
# qdrant-client stores the full index in this directory (no server needed).
# Leave empty ("") to use the remote server at QDRANT_HOST:QDRANT_PORT.
#
# Example .env entry:
#   QDRANT_LOCAL_PATH=./data/qdrant_local
#
QDRANT_LOCAL_PATH: str = os.getenv("QDRANT_LOCAL_PATH", "")

# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

RETRIEVAL_TOP_K: int  = int(os.getenv("RETRIEVAL_TOP_K", "20"))   # candidates from Qdrant
RERANK_TOP_K: int     = int(os.getenv("RERANK_TOP_K", "8"))        # passed to LLM after rerank
RERANKER_MODEL: str   = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")

# Which cross-encoder runtime to score with:
#   torch  sentence-transformers CrossEncoder. The default, and what every
#          indexed answer to date was reranked with.
#   onnx   fastembed TextCrossEncoder. Measured 5-32x faster on CPU, which
#          matters a great deal on a small VPS and not at all on the GPU
#          workstation. Note fastembed does NOT offer bge-reranker-v2-m3, so
#          switching backend also means switching model — set RERANKER_MODEL
#          to one of TextCrossEncoder.list_supported_models().
RERANKER_BACKEND: str = os.getenv("RERANKER_BACKEND", "torch").strip().lower()

# Thread cap for the ONNX runtime. Left unset it inherits fastembed's default,
# which reads the *host* core count and oversubscribes a cpu-limited container.
RERANKER_THREADS: int | None = (
    int(os.environ["RERANKER_THREADS"]) if os.getenv("RERANKER_THREADS") else None
)

# ---------------------------------------------------------------------------
# Pipeline thresholds (Quality Gate)
# ---------------------------------------------------------------------------

MIN_WORD_COUNT: int           = int(os.getenv("MIN_WORD_COUNT", "500"))
MIN_QUALITY_SCORE: float      = float(os.getenv("MIN_QUALITY_SCORE", "0.6"))
MIN_CHUNK_QUALITY_SCORE: float = float(os.getenv("MIN_CHUNK_QUALITY_SCORE", "0.6"))

# ---------------------------------------------------------------------------
# API — FastAPI
# ---------------------------------------------------------------------------

API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
API_PORT: int = int(os.getenv("API_PORT", "8000"))

# Browser origins allowed to call the API directly (CORS `Access-Control-Allow-Origin`).
# Comma-separated; falls back to the local dev hosts when unset so `uv run` on a
# workstation keeps working with no .env entry.
#
# Deployment note: behind the Caddy/Next.js reverse proxy the browser only ever
# hits a same-origin `/api/*` URL, so CORS is not exercised at all. This matters
# for anyone pointing a browser at the API on another hostname (a second
# frontend deploy, a preview build, local frontend against the VPS API).
#
#   NEO_CORS_ORIGINS=https://neo.example.com,https://staging.neo.example.com
#
_DEV_CORS_ORIGINS = [
    "http://localhost:3000",   # Next.js dev
    "http://localhost:3001",
    "http://127.0.0.1:3000",
]
CORS_ORIGINS: list = [
    o.strip().rstrip("/")
    for o in os.getenv("NEO_CORS_ORIGINS", "").split(",")
    if o.strip()
] or _DEV_CORS_ORIGINS

# Credentialed requests (cookies / Authorization) cannot be combined with a
# wildcard origin — the browser rejects `Access-Control-Allow-Origin: *` when
# credentials are sent. Downgrade automatically instead of shipping a config
# that silently fails every preflight.
CORS_ALLOW_CREDENTIALS: bool = "*" not in CORS_ORIGINS

# ---------------------------------------------------------------------------
# UI — Streamlit
# ---------------------------------------------------------------------------

UI_PORT: int = int(os.getenv("UI_PORT", "8501"))

# ---------------------------------------------------------------------------
# Observability — OpenTelemetry / Arize Phoenix
# ---------------------------------------------------------------------------

PHOENIX_HOST: str = os.getenv("PHOENIX_HOST", "localhost")
PHOENIX_PORT: int = int(os.getenv("PHOENIX_PORT", "6006"))


# ---------------------------------------------------------------------------
# Convenience: ensure data directories exist at import time
# ---------------------------------------------------------------------------

def ensure_dirs() -> None:
    """Create all data subdirectories if they don't exist."""
    for d in [RAW_DIR, PROCESSED_DIR, AUDIO_DIR, EXPORTS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


ensure_dirs()
