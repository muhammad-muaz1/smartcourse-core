# Build from the repo root, which holds the workspace lockfile and libs/:
#   docker build -f services/analytics/Dockerfile -t smartcourse-analytics .
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so a code-only change doesn't reinstall them. uv needs every
# workspace member's pyproject.toml to resolve the lockfile, not just this one's.
COPY pyproject.toml uv.lock ./
COPY libs/platform-core/pyproject.toml libs/platform-core/
COPY libs/contracts/pyproject.toml libs/contracts/
COPY services/identity/pyproject.toml services/identity/
COPY services/course/pyproject.toml services/course/
COPY services/learning/pyproject.toml services/learning/
COPY services/notification/pyproject.toml services/notification/
COPY services/analytics/pyproject.toml services/analytics/
COPY gateway/pyproject.toml gateway/
COPY libs/platform-core/src libs/platform-core/src
COPY libs/contracts/src libs/contracts/src
RUN uv sync --frozen --no-dev --package smartcourse-analytics

COPY services/analytics /app/services/analytics

ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
ENV SMARTCOURSE_APP__GIT_SHA=$GIT_SHA \
    SMARTCOURSE_APP__BUILD_TIME=$BUILD_TIME

WORKDIR /app/services/analytics
EXPOSE 8005
# Never --reload in an image. Proxy headers are trusted only from FORWARDED_ALLOW_IPS.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8005"]
