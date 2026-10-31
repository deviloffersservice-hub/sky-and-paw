# syntax=docker/dockerfile:1
# Optional generic image (hosting is Codespaces + Pages; Docker is not used for hosting).
#   docker build -t skypaw:dev .            (or: make docker)
#   docker run --rm -p 7860:7860 --env-file .env skypaw:dev
# The ML stack (requirements-ml.txt) is not included at T0; M3 adds it.

# --- Stage 1: build the frontend -------------------------------------------------------
# Base images pinned to exact tag + multi-arch index digest (resolved from the registry 2026-10-31).
FROM node:20.18.0-bookworm-slim@sha256:28fbbb764069c698ead61d6a739a7615e8f0e07a4b8fe1473ceca70c1c3d6aaa AS web
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
ARG VITE_BASE=/
RUN VITE_BASE="${VITE_BASE}" npm run build

# --- Stage 2: Python runtime ------------------------------------------------------------
FROM python:3.11.18-slim-bookworm@sha256:94a8fee35252d06ab75856f15f12c7bae67b10e8a6da93c7fded82b2610926cd AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=7860 \
    DATA_DIR=/data \
    SKYPAW_ENV=prod \
    SKYPAW_FRONTEND_DIST=/app/frontend/dist

WORKDIR /app

RUN groupadd --system --gid 10001 skypaw \
 && useradd --system --uid 10001 --gid skypaw --home-dir /app --shell /usr/sbin/nologin skypaw

# Dependencies first so code edits do not invalidate this layer.
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/pyproject.toml backend/pyproject.toml
COPY backend/skypaw backend/skypaw
RUN pip install --no-cache-dir --no-deps ./backend \
 && rm -rf backend/build backend/*.egg-info

COPY --from=web /src/frontend/dist /app/frontend/dist

RUN mkdir -p /data && chown skypaw:skypaw /data
VOLUME ["/data"]

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

USER skypaw
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-m", "skypaw.healthcheck"]

CMD ["sh", "-c", "exec uvicorn skypaw.app:create_app --factory --host 0.0.0.0 --port ${PORT:-7860}"]
