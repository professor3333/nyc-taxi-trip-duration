# MLflow tracking server + registry. The official image lacks a Postgres
# driver, so this is the smallest image that has one.
#
# Fully pinned (G11): the base by digest (the same as the service image) and
# every package, transitive ones included, from requirements.txt. Only the
# top-level packages used to be pinned; SQLAlchemy 2.1 then arrived as a
# transitive upgrade, switched postgresql:// to the psycopg v3 driver and the
# server stopped starting. mlflow and SQLAlchemy match uv.lock, so client
# and server agree. Refresh: see the header of requirements.txt.
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements.txt && pip check
EXPOSE 5000
