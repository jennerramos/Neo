# syntax=docker/dockerfile:1
#
# Neo v2 — FastAPI serving image (implementation plan P1-1).
#
# CPU-only by design. Answer generation is an outbound HTTPS call through the
# llm/ provider layer (LLM_PROVIDER=gemini today), so no model weights for
# generation live here. What does run locally is retrieval:
#
#   • fastembed dense  (nomic-ai/nomic-embed-text-v1.5)  — ONNX, no torch
#   • fastembed sparse (Qdrant/bm25)                     — ONNX, no torch
#   • BGE cross-encoder reranker                         — torch, CPU
#
# That is ~1.4 GB resident per worker, which is why the process runs with
# -w 1 on a 4 GB VPS. Bump to 2 workers only on 8 GB+.

ARG PYTHON_VERSION=3.12

# ═══════════════════════════════════════════════════════════════════════════
# Stage 1 — builder: resolve and install everything into a venv
# ═══════════════════════════════════════════════════════════════════════════
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Torch goes in first, from the CPU wheel index.
#
# pyproject.toml's [tool.uv.sources] pins torch to the CUDA 12.8 channel — the
# right call on the workstation, where WhisperX and the GPU reranker need it,
# and the wrong one here: the CUDA build is ~3 GB of driver shims that a VPS
# with no GPU will never load. Installing the CPU wheel up front means the
# resolver on the next line sees torch as already satisfied (sentence-
# transformers only asks for `torch>=1.11`) and never reaches for the big one.
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.2"

COPY deploy/requirements-api.txt /tmp/requirements-api.txt
RUN pip install -r /tmp/requirements-api.txt

# ═══════════════════════════════════════════════════════════════════════════
# Stage 2 — runtime
# ═══════════════════════════════════════════════════════════════════════════
FROM python:${PYTHON_VERSION}-slim AS runtime

# Reranker identity is a build arg *and* an env var so the weights baked below
# are guaranteed to be the ones config.py asks for at boot.
#
# The ONNX backend is 5-32x faster on CPU, which decides whether a small VPS is
# usable (see the benchmark table in deploy/README.md). fastembed does not
# publish bge-reranker-v2-m3, so switching backend also switches model:
#
#   docker build \
#     --build-arg RERANKER_BACKEND=onnx \
#     --build-arg RERANKER_MODEL=Xenova/ms-marco-MiniLM-L-6-v2 .
ARG RERANKER_BACKEND=torch
ARG RERANKER_MODEL=BAAI/bge-reranker-v2-m3

# HF_HOME covers sentence-transformers; FASTEMBED_CACHE_PATH is where fastembed
# unpacks its ONNX models. Both point under /home/neo so the bake below lands in
# a directory the runtime user owns.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RERANKER_MODEL=${RERANKER_MODEL} \
    RERANKER_BACKEND=${RERANKER_BACKEND} \
    HF_HOME=/home/neo/.cache/huggingface \
    FASTEMBED_CACHE_PATH=/home/neo/.cache/fastembed

RUN useradd --create-home --uid 10001 neo

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# observability/query_log.py appends to data/query_log.jsonl relative to cwd.
# Creating it owned by neo also fixes ownership on the named volume compose
# mounts here — Docker seeds a fresh volume from the image path, permissions
# included.
RUN mkdir -p /app/data && chown neo:neo /app/data

USER neo

# Bake the retrieval models into the image rather than downloading them on
# first boot. Same bytes either way, but baking buys three things: deterministic
# start-up time, restarts that don't re-download, and a build that fails loudly
# on a bad model name instead of a container that limps with a cold /ask.
#
# This sits BEFORE the source COPY on purpose. It is the most expensive layer in
# the image (~1.2 GB of downloads), and putting it after the source would make
# every one-line code edit re-download the lot.
#
# Model names come from env rather than config.py: importing config does
# `os.environ["DATABASE_URL"]`, which would KeyError during a build.
RUN python - <<'PY'
import os
from fastembed import TextEmbedding, SparseTextEmbedding

TextEmbedding(model_name="nomic-ai/nomic-embed-text-v1.5")
SparseTextEmbedding(model_name="Qdrant/bm25")

backend = os.environ["RERANKER_BACKEND"]
model   = os.environ["RERANKER_MODEL"]
if backend == "onnx":
    from fastembed.rerank.cross_encoder import TextCrossEncoder
    TextCrossEncoder(model_name=model)
else:
    from sentence_transformers import CrossEncoder
    CrossEncoder(model, max_length=512, device="cpu")
print("baked: dense + sparse + %s reranker (%s)" % (backend, model))
PY

# Source last: it is the layer that changes on every commit, and everything
# above it is expensive and stable.
# Only the serving path plus what `alembic upgrade head` and `database/seed.py`
# need on first deploy (P1-4). pipeline/ is deliberately absent — nothing under
# api/ or rag/ imports it, and it drags in the whole ASR/extraction stack.
COPY --chown=neo:neo config.py alembic.ini ./
COPY --chown=neo:neo api/           ./api/
COPY --chown=neo:neo rag/           ./rag/
COPY --chown=neo:neo llm/           ./llm/
COPY --chown=neo:neo database/      ./database/
COPY --chown=neo:neo observability/ ./observability/

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# One worker (see header). Gunicorn earns its place even at -w 1: it is the
# watchdog that kills and respawns a wedged worker. Docker's healthcheck alone
# would only mark the container unhealthy, not restart it.
#
# --timeout 180 must stay above LLM_READ_TIMEOUT (120 s default) or a worker
# gets reaped mid-generation.
CMD ["gunicorn", "api.main:app", \
     "--worker-class", "uvicorn_worker.UvicornWorker", \
     "--workers", "1", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "180", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
