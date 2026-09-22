# MLflow tracking server + registry. The official image lacks a Postgres
# driver, so this is the smallest image that has one. Version pinned to match
# uv.lock's mlflow so client and server agree.
FROM python:3.12-slim
RUN pip install --no-cache-dir mlflow==3.16.1 psycopg2-binary==2.9.10 boto3==1.40.0
EXPOSE 5000
