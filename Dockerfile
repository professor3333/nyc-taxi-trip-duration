# syntax=docker/dockerfile:1.7
# One image for local Compose and AWS Lambda (container). Runtime deps only,
# installed from uv.lock; the model artefacts are baked in at build time.
#
#   docker build -t tripduration:dev .                       # local models/
#   docker build --build-arg GIT_SHA=$(git rev-parse HEAD) …  # CI stamps the sha
#
# Lambda: the AWS Lambda Web Adapter extension forwards Lambda events to the
# uvicorn server on 8080, so the same CMD works in both environments.

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.12.5
ARG LAMBDA_ADAPTER_VERSION=0.9.1
# Where the model artefacts come from: the working tree (dev) or the directory
# scripts/fetch_champion.py fills from the DVC remote (deploy).
ARG MODELS_SRC=models
ARG REFERENCE_SRC=data/reference

# --- builder: resolve and install the locked runtime environment ---------------------
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv
FROM public.ecr.aws/awsguru/aws-lambda-adapter:${LAMBDA_ADAPTER_VERSION} AS lwa
FROM python:${PYTHON_VERSION}-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# --frozen: exactly uv.lock or fail. --no-dev/--no-group train: no pytest, dvc, mlflow.
RUN uv sync --frozen --no-dev --no-group train

# --- runtime -------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime
ARG MODELS_SRC
ARG REFERENCE_SRC
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
COPY --chown=app:app configs/holidays.csv ./configs/holidays.csv
COPY --chown=app:app ${REFERENCE_SRC}/zone_centroids.csv ./data/reference/zone_centroids.csv
COPY --chown=app:app ${MODELS_SRC}/model.pkl ${MODELS_SRC}/fallback_table.parquet ${MODELS_SRC}/model_meta.json ./models/
COPY --chown=app:app ${MODELS_SRC}/champion.json ./models/champion.json

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    MODEL_DIR=/app/models \
    REFERENCE_DIR=/app/data/reference \
    HOLIDAYS_PATH=/app/configs/holidays.csv \
    PARAMS_PATH=/app/params.yaml \
    PORT=8080 \
    GIT_SHA=${GIT_SHA} \
    AWS_LWA_PORT=8080 \
    AWS_LWA_READINESS_CHECK_PATH=/health \
    OMP_NUM_THREADS=2
USER app
EXPOSE 8080
CMD ["uvicorn", "tripduration.api.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
