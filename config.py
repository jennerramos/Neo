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
# LLM — Ollama (local, for extraction)
# ---------------------------------------------------------------------------

OLLAMA_HOST: str  = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")

# ---------------------------------------------------------------------------
# LLM — Claude API (customer-facing RAG)
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY: str   = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL: str        = os.getenv("CLAUDE_MODEL", "claude-opus-4-6")

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
