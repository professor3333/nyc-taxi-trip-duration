# data/

DVC-tracked. Git holds only the `*.dvc` pointer files here; the bytes live in
DVC's remote. After cloning:

    make setup            # installs dvc via the `train` dependency group
    uv run dvc pull       # fetches every file the current commit points at
    make verify-raw       # recomputes md5s and compares with reports/ingest/

## Layout

| Path | What | Source of truth |
|---|---|---|
| `raw/yellow/YYYY-MM.parquet` | **Immutable copy** of TLC's monthly file, byte-for-byte as served | `reports/ingest/yellow-YYYY-MM.json` (`source_md5`, `etag`, `bytes`, `rows`, source schema) |
| `reference/taxi_zone_lookup.csv` | TLC zone lookup, byte-for-byte | `reports/ingest/zones.json` |
| `validated/`, `processed/` | pipeline outputs (not yet built) | `dvc.lock` |

Raw files are **not** normalised: column names and dtypes are exactly what TLC
published. The rename/cast to the canonical schema in `configs/schema_raw.yaml`
happens in the first pipeline stage. Consequently the md5 in a `.dvc` pointer,
the `source_md5` in the ingest report, and the object key in the DVC remote are
the same value, and any snapshot can be verified against TLC's file directly.

## Size

One yellow month is ~60–65 MB (3.5–4M rows). Three months ≈ 190 MB; the
six-month training window of ADR-0003 ≈ 400 MB.

## Fetching new months

    make ingest MONTH="2024-11 2024-12"
    uv run dvc add data/raw/yellow/2024-11.parquet data/raw/yellow/2024-12.parquet
    uv run dvc push

Ingest is idempotent: a month whose ETag and size are unchanged at TLC is
skipped without downloading. A changed month is a TLC republish; the report
records the md5 it replaced and `dvc add` produces a one-file diff.

## Remote

Configured per machine in `.dvc/config.local` (git-ignored). Currently a local
directory for development; the S3 remote is added when the AWS account and
budget alert exist (ADR-0004). `dvc pull` from S3 will need AWS credentials
with read access to the bucket.
