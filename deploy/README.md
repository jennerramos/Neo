# Deploying Neo v2

Covers implementation-plan items **P1-1** (backend image), **P1-2** (frontend
image) and **P1-3** (Compose stack + Caddy TLS), plus the P0-2 auth decision
that folded into the Caddyfile. **P1-4** (provision + cutover) and **P1-6**
(backups) are now done and are recorded below rather than proposed; **P1-5**
(workstation → VPS data flow) is still outlined at the end and not yet built.

## The live pilot

Since **2026-08-21** the pilot serves `https://neoboardinsights.com` (and
`www.`) with a Let's Encrypt certificate. Before that it ran IP-only over plain
HTTP for four days.

| | |
|---|---|
| Host | OVH `15.204.248.52`, Ubuntu 24.04, 4 vCPU / 7.7 GB / 72 GB |
| SSH | `ubuntu@`, key `~/.ssh/neo_vps` — **not `root@`**, which is refused |
| Repo | `~/neo`, deployed with `--env-file deploy/.env.deploy` |
| DNS | Cloudflare, apex `A` + `www` `CNAME`, both **grey-cloud** |
| Serving LLM | `gemini` / `gemini-3.5-flash` |
| Reranker | `onnx` / `Xenova/ms-marco-MiniLM-L-6-v2`, `RERANKER_THREADS=4` |
| Pinned | `POSTGRES_IMAGE_TAG=16-alpine`, `QDRANT_IMAGE_TAG=v1.17.1` |

**The Cloudflare records must stay DNS-only (grey cloud).** Cloudflare's proxy
buffers `text/event-stream`, which would silently undo everything the Caddyfile
does to keep SSE a stream — the symptom is a whole answer landing at once after
~30 s, with nothing in the logs to explain it. The accepted cost is that the
origin IP is public and there is no CDN or DDoS layer in front of it.

Two corrections to figures quoted further down this file, both measured on the
real box under real traffic rather than in a container simulation:

- **The API is ~2.5 GB resident once it has served queries**, not the 1.42 GB
  measured idle-after-warm-up. The reranker arena grows. 4 GB would have been
  genuinely tight; the 8 GB-class box was the right call.
- **The simulated CPU benchmarks below ran about 2× optimistic.** Real
  end-to-end `/ask` on the torch reranker was 48–57 s per question, not the
  ~29 s the `--cpus`-limited runs predicted. Switching to onnx took it to
  **4.6–7.9 s**.

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

Prerequisites: a VPS with Docker installed, and DNS records already pointing at
it — Caddy provisions the certificate on first boot over an HTTP-01 challenge,
so the name must resolve **and port 80 must be open** before it can. Confirm
both before restarting Caddy; a premature attempt earns an ACME back-off you
then have to wait out.

`NEO_DOMAIN` takes Caddy site addresses, so more than one name is a
comma-separated list — each gets its own certificate and both serve the app:

```
NEO_DOMAIN="neoboardinsights.com, www.neoboardinsights.com"
```

Adding or changing the domain never needs an image rebuild. The frontend calls
a relative `/api` and so never learns its own hostname; `up -d caddy` is the
whole change.

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
curl https://neoboardinsights.com/api/health                # 200, no credentials
curl -u pilot:<pw> https://neoboardinsights.com/api/schools # 8 rows
curl https://neoboardinsights.com/api/schools               # 401
curl -sI http://neoboardinsights.com/ | head -1            # 308, redirect to https
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
  https://neoboardinsights.com/api/ask?stream=true
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

## The ONNX reranker (`RERANKER_BACKEND=onnx`)

Reranking was **28.2 s of that 29 s**. It is not *a* contributor to VPS latency;
it is the whole of it. `rag/retriever.py` can now score with fastembed's ONNX
cross-encoder instead of sentence-transformers, selected by `RERANKER_BACKEND`.

Rerank-only, 12 real queries × 20 candidates, 2 threads, agreement measured
against today's production ordering:

| model | median/query | speedup | top-8 overlap | Spearman |
|---|---|---|---|---|
| torch `bge-reranker-v2-m3` (default) | 28.2 s | 1.0× | 100% | 1.00 |
| onnx `ms-marco-MiniLM-L-6-v2` | **0.88 s** | **32×** | 67% | 0.60 |
| onnx `jina-reranker-v1-turbo-en` | 1.16 s | 24× | 54% | 0.49 |
| onnx `ms-marco-MiniLM-L-12-v2` | 1.72 s | 16× | 59% | 0.53 |
| onnx `bge-reranker-base` | 4.95 s | 5.7× | 69% | 0.70 |
| onnx `jina-reranker-v2-base-multilingual` | 6.52 s | 4.3× | 69% | 0.66 |

End-to-end `retrieve()` on a simulated 2-vCPU box: **29.04 s → 1.49 s**.
Image size: **2.35 GB → 1.07 GB** (the v2-m3 weights are ~2 GB; MiniLM is 80 MB).

fastembed does not publish `bge-reranker-v2-m3`, so this is a model change, not
just a runtime change — the ordering genuinely differs. Build it in:

```bash
docker build -t neo-api \
  --build-arg RERANKER_BACKEND=onnx \
  --build-arg RERANKER_MODEL=Xenova/ms-marco-MiniLM-L-6-v2 .
```

Set `RERANKER_THREADS` to the vCPU count; fastembed otherwise reads the host's
core count and oversubscribes a cpu-limited container.

### What the evidence does and does not support

**The 9-case eval set cannot judge this change.** Run twice against an
*unchanged* container it scored 5/9 then 6/9, flipping on ask-002 — the router
LLM is nondeterministic, and that noise is as large as the entire effect being
measured. Any "onnx scored lower" reading from it is measuring dice.

So retrieval was probed directly instead — same stage-1 candidates, each
reranker picks its top 8, does any chunk contain the case's expected keywords:

| backend | evidence surfaced |
|---|---|
| torch `bge-reranker-v2-m3` | 8/8 |
| onnx `ms-marco-MiniLM-L-6-v2` | 8/8 |
| onnx `bge-reranker-base` | 7/8 |

MiniLM-L-6 holds up despite reordering heavily — 67% overlap is a different
ordering, not a worse one. But 8 cases with every backend at ceiling is weak
evidence, and it cannot detect a subtle quality loss. **P2-0 (expand to ≥25
cases with `expected_chunk_ids` and precision@8) is the prerequisite for
calling this decision settled**, and is now also the prerequisite for judging
`RETRIEVAL_TOP_K=10`.

### Settled — P2-0 landed, and onnx shipped

The eval set is now 33 grounded cases with `expected_chunk_ids`, which is
enough signal to judge a reranker instead of measuring router dice. Against it
the onnx backend scored **33/33 with chunk recall unchanged**, so the reordering
really is a different ordering and not a worse one. It went live on the VPS on
2026-08-17 and took `/ask` from 48–57 s to **4.6–7.9 s** per question.

`RETRIEVAL_TOP_K=10` never had to be decided: with onnx doing the reranking,
`20` is affordable, so the VPS keeps the better recall and the question is moot.

**The repo default is still `torch`, and that is deliberate** — the workstation
has a GPU where none of this matters, and the indexed corpus was built with the
v2-m3 ordering. The backend is chosen per-deployment in the env file, and only
the VPS sets `onnx`. Because the weights are baked in at build time, changing it
means a rebuild: `docker-compose.yml` sources the build args from the same env
file for exactly this reason, so the image and the runtime cannot drift apart.

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
- **P1-6 — offsite copy of the nightly backup.** The nightly itself is *done*
  and running: `neo-backup.timer` at 03:30 UTC writes `neo_v2` and the
  `caddy_data` volume to `/var/backups/neo/{daily,weekly}` with 14+4 retention,
  and a restore drill passed on 2026-08-17. What is missing is that the
  destination is the VPS's own disk, so it survives a bad deploy but not a lost
  box. Enabling offsite is one uncommented `rclone` block marked `OFFSITE` in
  `/usr/local/bin/neo-backup.sh`. Deferred while the workstation is still a full
  mirror of the VPS — that argument expires the moment P1-5 lands, or once the
  pilot query logs in the `api_data` volume become the only record of usage.
  Note the `caddy_data` volume now holds live certificate private keys, not just
  the ACME account, so its tarball is `chmod 600` in the script.
- **P3-14 — UptimeRobot** on `https://neoboardinsights.com/api/health`, which is
  unauthenticated precisely so this works.
