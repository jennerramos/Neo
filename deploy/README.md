# Deploying Neo v2

Covers implementation-plan items **P1-1** (backend image), **P1-2** (frontend
image) and **P1-3** (Compose stack + Caddy TLS), plus the P0-2 auth decision
that folded into the Caddyfile. P1-4 (provision + cutover), P1-5 (workstation →
VPS data flow) and P1-6 (backups) are outlined at the end but not yet built.

## Shape of the thing

```
              :443
internet ──▶ caddy ──┬── /api/health ─▶ api:8000/health      (public)
                     ├── /api/*      ─▶ api:8000/*           (basic auth)
                     └── *           ─▶ web:3000             (basic auth)

                     web ──(SSR only)──▶ api:8000            (in-network)
                     api ──▶ postgres:5432
                         └─▶ qdrant:6333
                         └─▶ LLM provider over HTTPS         (outbound)
```

Postgres and Qdrant are not published to the host. Nothing but Caddy binds a
public port.

**The API holds no generation model.** Since the `llm/` provider layer landed,
`/ask` generation is an outbound HTTPS call, so the only resident weights are
retrieval: fastembed dense + sparse (ONNX) and the BGE reranker (torch CPU),
~1.4 GB. That is what makes a 4 GB box viable and why the backend runs
`--workers 1`.

## Files

| Path | What it is |
|---|---|
| `Dockerfile` | Backend image. CPU-only torch, retrieval models baked in. |
| `deploy/requirements-api.txt` | Serving deps — pyproject core + `llm` extra, minus the workstation stack. |
| `frontend/Dockerfile` | Next.js standalone image. |
| `docker-compose.yml` | The five services. |
| `deploy/Caddyfile` | TLS, routing, Basic auth, SSE-safe proxying. |
| `deploy/.env.deploy.example` | Template for the one env file this stack reads. |

## First deploy

Prerequisites: a VPS with Docker installed, and `neo.<domain>` A/AAAA records
already pointing at it — Caddy provisions the certificate on first boot and
needs DNS to resolve before it can.

```bash
git clone <repo> neo && cd neo

cp deploy/.env.deploy.example deploy/.env.deploy
chmod 600 deploy/.env.deploy
# Fill in: NEO_DOMAIN, NEO_ACME_EMAIL, POSTGRES_PASSWORD, LLM_API_KEY,
#          NEO_BASIC_AUTH_USER, NEO_BASIC_AUTH_HASH

docker run --rm caddy:2-alpine caddy hash-password --plaintext 'the-pilot-password'
# paste the output into NEO_BASIC_AUTH_HASH

docker compose --env-file deploy/.env.deploy up -d --build
```

**Always pass `--env-file deploy/.env.deploy`.** Without it Compose falls back
to `./.env`, which in this repo is the *workstation* config — localhost
database, CUDA paths, pipeline keys. It would interpolate quietly and wrongly.

Then, still per P1-4:

```bash
# schema
docker compose --env-file deploy/.env.deploy exec api alembic upgrade head

# the 8 colleges
docker compose --env-file deploy/.env.deploy exec api python database/seed.py

# smoke
curl https://neo.<domain>/api/health                  # 200, no credentials
curl -u pilot:<pw> https://neo.<domain>/api/schools   # 8 rows
curl https://neo.<domain>/api/schools                 # 401
```

Data comes over separately: `pg_dump` from the workstation restored into the
`postgres` service, and a Qdrant snapshot restored into `qdrant`. Pin
`POSTGRES_IMAGE_TAG` and `QDRANT_IMAGE_TAG` to the workstation's versions
**before** doing either — neither format crosses major versions cleanly.

## Verifying streaming actually streams

The single most breakable thing in this stack is SSE, because three layers can
each buffer it into a non-stream. Two defences are already in place — Caddy's
`flush_interval -1`, and an `encode` allow-list that never compresses
`text/event-stream` — but verify on the real host, not just locally:

```bash
curl -N -u pilot:<pw> \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d '{"query":"What did the board vote on most recently?"}' \
  https://neo.<domain>/api/ask?stream=true
```

Frames should arrive progressively: one `meta`, many `token`, one `done`. If
the whole answer lands at once after 30 s, something re-buffered it.

## Measured on a local build (2026-08-16)

| | |
|---|---|
| `neo-api` image | 2.35 GB (torch CPU + baked retrieval weights) |
| `neo-web` image | ~150 MB runtime layer |
| API resident memory, idle after warm-up | **1.42 GB** — matches the plan's F-20 estimate, and is why it's `--workers 1` |
| API cold boot to `Application startup complete` | ~7 s, weights loaded from the image, no download |

The startup warm-up still makes one metadata call to the Hugging Face hub
(you'll see an "unauthenticated requests to the HF Hub" warning in the logs).
It is cache-backed — the weights are already in the image, so a slow or
unreachable HF only costs the timeout, it does not re-download.

## Rehearsing locally before the VPS

The local run is the same stack, same images, same Caddy config — only the site
address changes (`NEO_DOMAIN=http://localhost` turns off TLS/ACME). It doubles
as a dry run of the P1-4 cutover, because loading the data is the same work.

```bash
# 1. Local env file (gitignored via **/.env.local). It sets NEO_ENV_FILE to
#    itself, so it never collides with the VPS's deploy/.env.deploy.
#    Quote the bcrypt hash: compose interpolates $-signs in an --env-file.
docker compose --env-file deploy/.env.local up -d --build

# 2. Real Postgres data (native Windows instance -> container)
PGPASSWORD=… pg_dump -h 127.0.0.1 -U postgres -d neo_v2 \
    --no-owner --no-privileges -f neo_v2.sql
docker compose --env-file deploy/.env.local exec -T postgres \
    psql -U neo -d neo_v2 < neo_v2.sql

# 3. Real Qdrant data — copy the dev volume rather than sharing it, so the
#    stack can never corrupt your working index
docker compose --env-file deploy/.env.local stop qdrant
docker run --rm -v qdrant_storage:/from:ro -v neo_qdrant_data:/to alpine \
    sh -c "rm -rf /to/*; cp -a /from/. /to/"
docker compose --env-file deploy/.env.local start qdrant
```

Then browse `http://localhost` and run the SSE check below against it.

## Latency: CPU reranking is the pilot's real constraint

Measured 2026-08-16, one `retrieve()` (hybrid + BGE cross-encoder), steady
state, against the real 12,464-point collection. Docker `--cpus` limits
simulate the VPS tiers:

| Box | `RETRIEVAL_TOP_K` | Threads | retrieve + rerank |
|---|---|---|---|
| Workstation (24 core) | 20 | 12 | **8 s** |
| 2 vCPU (CX22) | 20 | 12 (unset — thrashes) | **45 s** |
| 2 vCPU (CX22) | 20 | 2 (matched) | **29 s** |
| 2 vCPU (CX22) | 10 | 2 | **15 s** |
| 4 vCPU (CX32) | 20 | 4 | **20 s** |
| 4 vCPU (CX32) | 10 | 4 | **10 s** |

Add ~20 s of provider time-to-first-token on top of every row (measured against
gemini-3.5-flash with 8 chunks of context).

The plan's F-20 sizing note reasoned about **memory** and got it right — 1.42 GB
per worker. Nobody had measured **CPU throughput**, and that, not RAM, is what
decides whether the pilot feels usable. Two conclusions:

1. **Always set `OMP_NUM_THREADS`/`MKL_NUM_THREADS` to the vCPU count.** Torch
   reads the host's CPU count and ignores the container limit. Free 35% win.
2. **`RETRIEVAL_TOP_K=10` halves rerank time** — but it is a retrieval-quality
   change, so it must be validated against the eval set before it ships. That
   is a direct argument for doing **P2-0** (expand eval to ≥25 cases) *before*
   go-live rather than in the first pilot week.

## Building on the VPS

A 2 vCPU / 4 GB box can build both images, but not quickly, and the Next.js
build is the memory-hungry half. If the build OOMs, add swap
(`fallocate -l 2G /swapfile`) or build elsewhere and push to a registry.

The backend build downloads ~1.2 GB of model weights (baked in on purpose —
see the Dockerfile header) plus the CPU torch wheel. Budget ~10 minutes on a
first, cold build.

## Operations

```bash
alias dc='docker compose --env-file deploy/.env.deploy'

dc ps
dc logs -f api
dc logs -f caddy            # cert issuance problems show up here
dc up -d --build api        # redeploy just the backend
dc exec api python -c "import config; print(config.LLM_PROVIDER, config.LLM_MODEL)"
```

Switching LLM provider is an env edit plus `dc up -d api` — no rebuild, since
the `openai`, `anthropic` and `ollama` SDKs all ship in the image and
`llm/factory.py` imports them lazily.

## Still to do

- **P1-5 — workstation → VPS data flow.** WireGuard, then uncomment the
  `10.8.0.1:` port bindings on `postgres` and `qdrant` in `docker-compose.yml`
  so the tunnel reaches them and the public interface does not. The pipeline
  stays on the workstation: it needs the GPU, and `PIPELINE_LLM_*` is a
  separate namespace from the serving `LLM_*` on purpose.
- **P1-6 — nightly backup.** `pg_dump | gzip | rclone` to B2/R2, 14 daily + 4
  weekly, plus one restore drill before the pilot opens. The Qdrant collection
  is rebuildable from `chunks.jsonl`, so Postgres is the thing that must not be
  lost — along with the `caddy_data` volume, which holds the ACME account.
- **P3-14 — UptimeRobot** on `https://neo.<domain>/api/health`, which is
  unauthenticated precisely so this works.
