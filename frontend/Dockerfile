# syntax=docker/dockerfile:1
#
# Neo v2 — Next.js frontend image (implementation plan P1-2).
#
# Two env vars decide how this talks to FastAPI, and they are not symmetric
# (see src/lib/api.ts):
#
#   • Browser  — BASE is the relative "/api". Caddy routes /api/* straight to
#                the api container, so calls stay same-origin and never touch
#                CORS. NEXT_PUBLIC_API_URL is therefore deliberately NOT baked;
#                setting it would hard-code an absolute backend URL into the
#                client bundle and require a rebuild to change.
#   • SSR/RSC  — /meetings and /insights render on the server and need an
#                absolute URL. NEO_API_TARGET stays runtime-configurable and is
#                set to http://api:8000 by docker-compose, which keeps SSR
#                traffic inside the Docker network (no TLS hop, no auth prompt).

ARG NODE_VERSION=20

# ═══════════════════════════════════════════════════════════════════════════
# Stage 1 — dependencies
# ═══════════════════════════════════════════════════════════════════════════
FROM node:${NODE_VERSION}-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

# ═══════════════════════════════════════════════════════════════════════════
# Stage 2 — build
# ═══════════════════════════════════════════════════════════════════════════
FROM node:${NODE_VERSION}-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

ENV NEXT_TELEMETRY_DISABLED=1

# The repo has no public/ today. Creating it keeps the COPY in the runtime
# stage valid, and means adding static assets later needs no Dockerfile edit.
RUN mkdir -p public && npm run build

# ═══════════════════════════════════════════════════════════════════════════
# Stage 3 — runtime
# ═══════════════════════════════════════════════════════════════════════════
FROM node:${NODE_VERSION}-alpine AS runtime
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN addgroup -g 1001 -S nodejs && adduser -S nextjs -u 1001

COPY --from=builder --chown=nextjs:nodejs /app/public         ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
# standalone traces server code only; the client bundle is copied alongside it.
COPY --from=builder --chown=nextjs:nodejs /app/.next/static    ./.next/static

USER nextjs

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD node -e "require('http').get('http://127.0.0.1:3000/',r=>process.exit(r.statusCode<500?0:1)).on('error',()=>process.exit(1))"

CMD ["node", "server.js"]
