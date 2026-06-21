# syntax=docker/dockerfile:1
#
# ytt — multi-stage build (plan: Deliverables / Dockerfile).
#
#  base     python:3.12-slim + ffmpeg (REQUIRED for yt-dlp bestaudio) + uv
#  builder  install locked deps (incl. dev) + the project, for testing
#  test     run `pytest -m "not integration"` — a red suite FAILS the build
#  runtime  runtime deps only, non-root, CMD ["ytt","serve"]
#
# The image is generic/portable — NO ardenone specifics are baked in; every
# deployment fact comes from YTT_* env (see docs/usage/self-hosting.md).
#
# TODO(marathon): pin the base image + uv image by digest at build time
# (plan: "Pinned digest"). Tags are used here because the digest can't be
# resolved offline during scaffolding.

FROM python:3.12-slim AS base
# ffmpeg is mandatory: yt-dlp's bestaudio path (Whisper fallback) shells out to it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.23 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:${PATH}"
WORKDIR /app

# --- builder: full dependency set (incl. dev) for the test stage ----------
FROM base AS builder
# Cache deps separately from source so code edits don't re-resolve everything.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project
COPY ytt ./ytt
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

# --- test: gate the build on the unit suite -------------------------------
FROM builder AS test
COPY tests ./tests
RUN uv run pytest -m "not integration" -q && touch /app/.pytest-passed

# --- runtime: lean image, runtime deps only, non-root ---------------------
FROM base AS runtime
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY ytt ./ytt
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Force the test stage into the build graph: a failed pytest aborts the build.
COPY --from=test /app/.pytest-passed /app/.pytest-passed

# Non-root user; owns the default cache + scratch mount points.
RUN useradd --create-home --uid 10001 ytt \
    && mkdir -p /cache /scratch \
    && chown -R ytt:ytt /app /cache /scratch
USER ytt

EXPOSE 8080
ENV YTT_CACHE_DIR=/cache \
    YTT_SCRATCH_DIR=/scratch
CMD ["ytt", "serve"]

# OCI labels link the published GHCR package back to the public repo.
LABEL org.opencontainers.image.title="ytt" \
      org.opencontainers.image.description="Remote MCP server for YouTube transcripts (captions + Whisper ASR)." \
      org.opencontainers.image.source="https://github.com/jedarden/ytt" \
      org.opencontainers.image.licenses="MIT"
