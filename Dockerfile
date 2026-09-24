# syntax=docker/dockerfile:1.7
# One image for local Compose and AWS Lambda (container). Runtime deps only,
# installed from uv.lock; the model artefacts are baked in at build time.
#
#   docker build -t tripduration:dev .                       # local models/
#   docker build --build-arg GIT_SHA=$(git rev-parse HEAD) …  # CI stamps the sha
#
# Lambda: the AWS Lambda Web Adapter extension forwards Lambda events to the
# uvicorn server on 8080, so the same CMD works in both environments.
#
# The `train` target is the canonical TRAINING environment: the same pinned
# base (so the same glibc/libm), the same uv.lock, linux/amd64 like Lambda.
# Features such as np.hypot differ in their last bit between libms, so a
# model is trained and evaluated where it will serve. scripts/train_env.sh
# builds and runs it; see docs/reproducibility.md.
#   docker build --platform linux/amd64 --target train -t tripduration-train .

# Pinned by digest, not tag: a moved tag would change libm under both the
# serving and the training image. Python 3.12.14, Debian 13 (glibc 2.41).
ARG PYTHON_IMAGE=python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
ARG UV_VERSION=0.12.5
ARG LAMBDA_ADAPTER_VERSION=0.9.1
# Where the model artefacts come from: the working tree (dev) or the directory
# scripts/fetch_champion.py fills from the DVC remote (deploy).
ARG MODELS_SRC=models
ARG REFERENCE_SRC=data/reference

# --- builder: resolve and install the locked runtime environment ---------------------
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv
FROM public.ecr.aws/awsguru/aws-lambda-adapter:${LAMBDA_ADAPTER_VERSION} AS lwa
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# --frozen: exactly uv.lock or fail. --no-dev/--no-group train: no pytest, dvc, mlflow.
RUN uv sync --frozen --no-dev --no-group train

# --- train: canonical training/evaluation environment -------------------------------
# Runtime + train + dev groups from the same uv.lock. The project itself is not
# installed: the repo is mounted at /work and imported from /work/src, so the
# code under test is the checkout, never a copy baked into the image.
FROM ${PYTHON_IMAGE} AS train
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --group train --no-install-project \
    && chmod -R a+rwX /app/.venv   # runs as the host uid; `uv run` adds the project
ENV PATH="/app/.venv/bin:$PATH" \
    VIRTUAL_ENV=/app/.venv \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONPATH=/work/src \
    PYTHONUNBUFFERED=1
WORKDIR /work

# --- runtime -------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
ARG MODELS_SRC
ARG REFERENCE_SRC
ARG GIT_SHA=unknown
ARG MODEL_VERSION=unknown
# The release this image is (release.json, ADR-0014). deploy.yml reads the
# label to prove an existing image is the release before reusing it.
ARG RELEASE_ID=none
LABEL org.opencontainers.image.source="https://github.com/professor3333/nyc-taxi-trip-duration" \
      git.sha="${GIT_SHA}" model.version="${MODEL_VERSION}" release.id="${RELEASE_ID}"

COPY --from=lwa /lambda-adapter /opt/extensions/lambda-adapter

RUN useradd --system --uid 10001 --create-home app
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app src ./src
COPY --chown=app:app params.yaml ./params.yaml
COPY --chown=app:app configs/holidays.csv ./configs/holidays.csv
COPY --chown=app:app ${REFERENCE_SRC}/zone_centroids.csv ./data/reference/zone_centroids.csv
COPY --chown=app:app ${MODELS_SRC}/model.pkl ${MODELS_SRC}/fallback_table.parquet ${MODELS_SRC}/model_meta.json ./models/
# release.json (ADR-0014) exists only in deploy builds (scripts/release_manifest.py);
# the [n] glob makes it optional, so a dev build from models/ still works.
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
