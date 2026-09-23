# syntax=docker/dockerfile:1.7
# One image for local Compose and AWS Lambda (container). Runtime deps only,
# installed from uv.lock; the model artefacts are baked in at build time.
#
#   docker build -t tripduration:dev .                       # local models/
#   docker build --build-arg GIT_SHA=$(git rev-parse HEAD) …  # CI stamps the sha
#
# Lambda: the AWS Lambda Web Adapter extension forwards Lambda events to the
# uvicorn server on 8080, so the same CMD works in both environments.

# Base images pinned by digest (multi-arch index), not by tag: a tag like
# python:3.12-slim moves weekly, and a rebuild from the same commit would
# then ship a different OS layer. The digests are part of the release
# (release.json hashes this file). Refresh deliberately:
#   docker buildx imagetools inspect python:3.12-slim --format '{{json .Manifest.Digest}}'
ARG PYTHON_IMAGE=python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1
ARG LAMBDA_ADAPTER_IMAGE=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1@sha256:46d6625e68cbbdd2efab4a20245977664513f13ffef47915b000d431adcea0b4
# Where the model artefacts come from: the working tree (dev) or the directory
# scripts/fetch_champion.py fills from the DVC remote (deploy).
ARG MODELS_SRC=models
ARG REFERENCE_SRC=data/reference
# The holidays file is reference data like the centroids: a deploy takes the
# champion's packaged copy (build/champion/reference/holidays.csv), not the
# working tree's.
ARG HOLIDAYS_SRC=configs/holidays.csv

# --- builder: resolve and install the locked runtime environment ---------------------
FROM ${UV_IMAGE} AS uv
FROM ${LAMBDA_ADAPTER_IMAGE} AS lwa
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# --frozen: exactly uv.lock or fail. --no-dev/--no-group train: no pytest, dvc, mlflow.
RUN uv sync --frozen --no-dev --no-group train

# --- runtime -------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
ARG MODELS_SRC
ARG REFERENCE_SRC
ARG HOLIDAYS_SRC
ARG GIT_SHA=unknown
ARG MODEL_VERSION=unknown
LABEL org.opencontainers.image.source="https://github.com/professor3333/nyc-taxi-trip-duration" \
      git.sha="${GIT_SHA}" model.version="${MODEL_VERSION}"

COPY --from=lwa /lambda-adapter /opt/extensions/lambda-adapter

RUN useradd --system --uid 10001 --create-home app
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app src ./src
COPY --chown=app:app params.yaml ./params.yaml
COPY --chown=app:app ${HOLIDAYS_SRC} ./configs/holidays.csv
COPY --chown=app:app ${REFERENCE_SRC}/zone_centroids.csv ./data/reference/zone_centroids.csv
COPY --chown=app:app ${MODELS_SRC}/model.pkl ${MODELS_SRC}/fallback_table.parquet ${MODELS_SRC}/model_meta.json ./models/
# release.json (deploy builds only; the [n] glob makes it optional for dev
# builds) names every component of this release - see scripts/release_manifest.py.
COPY --chown=app:app ${MODELS_SRC}/champion.json ${MODELS_SRC}/release.jso[n] ./models/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    MODEL_DIR=/app/models \
    REFERENCE_DIR=/app/data/reference \
    HOLIDAYS_PATH=/app/configs/holidays.csv \
    PARAMS_PATH=/app/params.yaml \
    PORT=8080 \
    GIT_SHA=${GIT_SHA} \
    AWS_LWA_PORT=8080 \
    AWS_LWA_READINESS_CHECK_PATH=/health/live \
    OMP_NUM_THREADS=2
USER app
EXPOSE 8080
CMD ["uvicorn", "tripduration.api.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
