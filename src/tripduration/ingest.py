"""Ingest stage: fetch TLC monthly files byte-for-byte and record provenance.

``data/raw/`` holds **immutable copies**: exactly the bytes CloudFront served,
so a snapshot can be verified against TLC's own file forever, independently of
later changes to our code. Ingest never rewrites those bytes. It does inspect
the file's schema against ``configs/schema_raw.yaml`` and refuses to store a
file with an unknown or missing-required column, so drift is caught on the day
it appears, in a reviewed config change — never absorbed silently. The
rename/cast to the canonical schema (``normalise``) is applied downstream by
the first pipeline stage, not here.

Idempotent: a HEAD request compares ``ETag``/``Content-Length`` with the last
report; an unchanged month is skipped without downloading. A changed month is
a TLC republish, downloaded and recorded with the md5 it replaced.

A month TLC has not published yet (HTTP 404) is a distinct, expected condition
(``MonthNotPublishedError``); the CLI turns it into a clean exit 0. Transient
failures (5xx, 429, network errors, short reads) are retried with backoff;
anything else propagates.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.csv as pcsv
import pyarrow.parquet as pq
import yaml

log = logging.getLogger(__name__)

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
TIMEOUT_S = 60
RETRIES = 3
BACKOFF_S = 2.0

_ARROW_TYPES: dict[str, pa.DataType] = {
    "int64": pa.int64(),
    "float64": pa.float64(),
    "string": pa.string(),
    "timestamp[us]": pa.timestamp("us"),
}

# Anything that behaves like urllib.request.urlopen: takes a URL or Request and
# returns a context manager whose value has .read(n) and .headers. Injected so
# tests never touch the network and can simulate 404s, 5xx and short reads.
Opener = Callable[[str | urllib.request.Request], Any]
Sleeper = Callable[[float], None]

_default_opener: Opener = functools.partial(urllib.request.urlopen, timeout=TIMEOUT_S)


# TLC's CloudFront distribution sits in front of S3 without s3:ListBucket, so
# a key that does not exist comes back as 403, not 404 — verified against
# 2026-08, 2099-01 and a nonsense path, all 403, while 2025-03 is 200. There
# is no authentication on these URLs, so a 403 cannot mean "not allowed"; it
# means "not there". Treating only 404 as unpublished made the weekly check
# fail every Monday until a month appeared.
NOT_PUBLISHED_CODES = frozenset({403, 404})


class MonthNotPublishedError(Exception):
    """TLC has not published this month yet (403 or 404 from CloudFront)."""


class SchemaDriftError(ValueError):
    """The file's columns do not match configs/schema_raw.yaml."""


class IncompleteDownloadError(OSError):
    """Bytes received differ from the Content-Length the server declared."""


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    dtype: pa.DataType
    variants: tuple[str, ...]
    optional: bool


@dataclass(frozen=True)
class RawSchema:
    columns: tuple[ColumnSpec, ...]
    trip_url_template: str
    zone_lookup_url: str
    zone_lookup_columns: tuple[str, ...]

    def variant_map(self) -> dict[str, str]:
        """Every accepted spelling -> canonical name (canonical maps to itself)."""
        out: dict[str, str] = {}
        for col in self.columns:
            out[col.name] = col.name
            for v in col.variants:
                out[v] = col.name
        return out

    def arrow_schema(self) -> pa.Schema:
        return pa.schema([pa.field(c.name, c.dtype) for c in self.columns])


@dataclass(frozen=True)
class RemoteInfo:
    etag: str | None
    content_length: int | None


def load_schema(path: Path) -> RawSchema:
    with path.open() as fh:
        raw = yaml.safe_load(fh)
    cols = tuple(
        ColumnSpec(
            name=name,
            dtype=_ARROW_TYPES[spec["dtype"]],
            variants=tuple(spec.get("variants", ())),
            optional=bool(spec.get("optional", False)),
        )
        for name, spec in raw["columns"].items()
    )
    src = raw["source"]
    return RawSchema(
        columns=cols,
        trip_url_template=src["trip_url_template"],
        zone_lookup_url=src["zone_lookup_url"],
        zone_lookup_columns=tuple(src["zone_lookup_columns"]),
    )


# --- HTTP -------------------------------------------------------------------


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in NOT_PUBLISHED_CODES:
            return False
        return exc.code == 429 or exc.code >= 500
    return isinstance(
        exc,
        urllib.error.URLError
        | http.client.IncompleteRead
        | IncompleteDownloadError
        | TimeoutError
        | ConnectionError,
    )


def _with_retries(
    fn: Callable[[], Any],
    *,
    what: str,
    retries: int = RETRIES,
    backoff: float = BACKOFF_S,
    sleep: Sleeper = time.sleep,
) -> Any:
    """Call ``fn`` up to ``retries + 1`` times, sleeping backoff * 2**n between.

    Only transient failures are retried. A 404 is raised immediately as
    ``MonthNotPublishedError``; any other non-transient error propagates at once.
    """
    for attempt in range(retries + 1):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code in NOT_PUBLISHED_CODES:
                raise MonthNotPublishedError(f"HTTP {e.code} at {e.url}") from e
            if not _is_transient(e) or attempt == retries:
                raise
            err: BaseException = e
        except Exception as e:
            if not _is_transient(e) or attempt == retries:
                raise
            err = e
        delay = backoff * (2**attempt)
        log.warning(
            "%s failed (%s); retry %d/%d in %.0fs",
            what,
            err,
            attempt + 1,
            retries,
            delay,
        )
        sleep(delay)
    raise AssertionError("unreachable")


def head(url: str, *, opener: Opener = _default_opener) -> RemoteInfo:
    """Return the server's ETag and Content-Length without downloading."""
    req = urllib.request.Request(url, method="HEAD")
    with opener(req) as resp:
        etag = resp.headers.get("ETag")
        length = resp.headers.get("Content-Length")
    return RemoteInfo(
        etag=etag.strip('"') if etag else None,
        content_length=int(length) if length else None,
    )


def download(
    url: str, dest: Path, *, opener: Opener = _default_opener
) -> tuple[str, int]:
    """Stream ``url`` to ``dest`` once; return (md5, bytes) of what was received.

    Writes to a ``.part`` file and renames on success, so an interrupted
    download never leaves a plausible-looking file behind. Raises
    ``IncompleteDownloadError`` if fewer or more bytes arrive than the server's
    Content-Length declared.
    """
    part = dest.with_suffix(dest.suffix + ".part")
    md5 = hashlib.md5()
    size = 0
    try:
        with opener(url) as resp, part.open("wb") as out:
            declared = resp.headers.get("Content-Length")
            while chunk := resp.read(1 << 20):
                md5.update(chunk)
                out.write(chunk)
                size += len(chunk)
        if declared is not None and int(declared) != size:
            raise IncompleteDownloadError(
                f"{url}: received {size} bytes, Content-Length was {declared}"
            )
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    part.replace(dest)
    return md5.hexdigest(), size


# --- schema -----------------------------------------------------------------


def check_schema(schema_in: pa.Schema, schema: RawSchema) -> dict[str, Any]:
    """Compare a file's schema with the canonical config without touching data.

    Returns what ``normalise`` would do (mapping, missing optional columns) for
    the report. Raises ``SchemaDriftError`` on an unknown column or a missing
    required one.
    """
    vmap = schema.variant_map()
    seen = list(schema_in.names)
    unknown = [c for c in seen if c not in vmap]
    if unknown:
        raise SchemaDriftError(
            f"unknown column(s) {unknown}; add to configs/schema_raw.yaml if legitimate"
        )
    mapped = {c: vmap[c] for c in seen}
    present = set(mapped.values())
    missing_required = [
        c.name for c in schema.columns if not c.optional and c.name not in present
    ]
    if missing_required:
        raise SchemaDriftError(f"required column(s) missing: {missing_required}")
    missing_optional = [c.name for c in schema.columns if c.name not in present]
    return {
        "columns_seen": seen,
        "source_schema": {f.name: str(f.type) for f in schema_in},
        "canonical_map": {c: v for c, v in mapped.items() if c != v},
        "missing_optional": missing_optional,
    }


def normalise(table: pa.Table, schema: RawSchema) -> tuple[pa.Table, dict[str, Any]]:
    """Rename to canonical names, add missing optional columns as null, cast.

    Pure: used by the first pipeline stage, not by ingest. Never drops or
    reorders rows. Raises ``SchemaDriftError`` via ``check_schema``.
    """
    info = check_schema(table.schema, schema)
    vmap = schema.variant_map()
    table = table.rename_columns([vmap[c] for c in table.column_names])
    for name in info["missing_optional"]:
        dtype = next(c.dtype for c in schema.columns if c.name == name)
        table = table.append_column(name, pa.nulls(table.num_rows).cast(dtype))
    # Canonical order, then a *safe* cast: a value that would not survive the
    # cast (e.g. a fractional passenger_count) raises instead of being altered.
    table = table.select([c.name for c in schema.columns])
    table = table.cast(schema.arrow_schema(), safe=True)
    return table, info


# --- reports ----------------------------------------------------------------


def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _read_report(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- ingest -----------------------------------------------------------------


def ingest_month(
    service: str,
    month: str,
    schema: RawSchema,
    data_dir: Path,
    reports_dir: Path,
    *,
    force: bool = False,
    opener: Opener = _default_opener,
    sleep: Sleeper = time.sleep,
) -> Path:
    """Store one month's file untouched and write its provenance report.

    Skips the download when HEAD shows the same ETag and Content-Length as the
    existing report (unless ``force``). Returns the raw file path. Raises
    ``MonthNotPublishedError`` on 404 (nothing is written).
    """
    if not MONTH_RE.match(month):
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    url = schema.trip_url_template.format(service=service, month=month)
    out_path = data_dir / "raw" / service / f"{month}.parquet"
    report_path = reports_dir / "ingest" / f"{service}-{month}.json"
    previous = _read_report(report_path)

    remote: RemoteInfo = _with_retries(
        lambda: head(url, opener=opener), what=f"HEAD {url}", sleep=sleep
    )
    if (
        previous is not None
        and not force
        and out_path.exists()
        and remote.etag is not None
        and remote.etag == previous.get("etag")
        and remote.content_length == previous.get("bytes")
    ):
        log.info(
            "%s %s unchanged at source (etag=%s); skipping download",
            service,
            month,
            remote.etag,
        )
        return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("downloading %s", url)
    source_md5, size = _with_retries(
        lambda: download(url, out_path, opener=opener), what=f"GET {url}", sleep=sleep
    )
    try:
        info = check_schema(pq.read_schema(out_path), schema)
        rows = pq.read_metadata(out_path).num_rows
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise

    report: dict[str, Any] = {
        "service": service,
        "month": month,
        "source_url": url,
        "source_md5": source_md5,
        "etag": remote.etag,
        "bytes": size,
        "rows": rows,
        "path": str(out_path),
        "ingested_at": _now(),
        "pyarrow_version": pa.__version__,
        **info,
    }
    if previous is not None and previous.get("source_md5") != source_md5:
        report["replaced_source_md5"] = previous["source_md5"]
        report["replaced_ingested_at"] = previous.get("ingested_at")
        log.warning(
            "%s %s REPUBLISHED by TLC: md5 %s -> %s",
            service,
            month,
            previous["source_md5"],
            source_md5,
        )
    _write_report(report_path, report)
    log.info(
        "ingested %s %s: rows=%d bytes=%d md5=%s missing_optional=%s",
        service,
        month,
        rows,
        size,
        source_md5,
        info["missing_optional"],
    )
    return out_path


def ingest_zones(
    schema: RawSchema,
    data_dir: Path,
    reports_dir: Path,
    *,
    opener: Opener = _default_opener,
    sleep: Sleeper = time.sleep,
) -> Path:
    """Fetch the TLC zone lookup CSV byte-for-byte and check its header."""
    out_path = data_dir / "reference" / "taxi_zone_lookup.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    md5, size = _with_retries(
        lambda: download(schema.zone_lookup_url, out_path, opener=opener),
        what=f"GET {schema.zone_lookup_url}",
        sleep=sleep,
    )
    table = pcsv.read_csv(out_path)
    expected = list(schema.zone_lookup_columns)
    if table.column_names != expected:
        out_path.unlink()
        raise SchemaDriftError(
            f"zone lookup columns {table.column_names} != expected {expected}"
        )
    _write_report(
        reports_dir / "ingest" / "zones.json",
        {
            "source_url": schema.zone_lookup_url,
            "md5": md5,
            "bytes": size,
            "rows": table.num_rows,
            "columns": table.column_names,
            "ingested_at": _now(),
        },
    )
    log.info("ingested zone lookup: rows=%d md5=%s", table.num_rows, md5)
    return out_path


def verify_raw(data_dir: Path, reports_dir: Path) -> list[str]:
    """Recompute md5 of every raw file and compare with its report.

    Returns a list of human-readable failures (empty means every file matches).
    This is the proof that a recovered snapshot is byte-identical to what TLC
    served when it was ingested.
    """
    failures: list[str] = []
    reports = sorted((reports_dir / "ingest").glob("*-????-??.json"))
    if not reports:
        return ["no ingest reports found"]
    for rp in reports:
        report = json.loads(rp.read_text())
        path = Path(report["path"])
        if not path.exists():
            failures.append(f"{path}: missing (dvc pull?)")
            continue
        actual = _md5_file(path)
        if actual != report["source_md5"]:
            failures.append(f"{path}: md5 {actual} != report {report['source_md5']}")
        else:
            log.info("%s ok (%s)", path, actual)
    return failures


# --- CLI --------------------------------------------------------------------


def _month_arg(value: str) -> str:
    if not MONTH_RE.match(value):
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {value!r}")
    return value


def main(
    argv: Sequence[str] | None = None,
    *,
    opener: Opener = _default_opener,
    sleep: Sleeper = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest TLC months or the zone lookup."
    )
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--month", type=_month_arg, nargs="+", help="one or more YYYY-MM")
    what.add_argument("--zones", action="store_true", help="fetch taxi_zone_lookup.csv")
    what.add_argument("--verify", action="store_true", help="check raw md5s vs reports")
    parser.add_argument("--service", default="yellow")
    parser.add_argument(
        "--force", action="store_true", help="re-download even if unchanged"
    )
    parser.add_argument("--schema", type=Path, default=Path("configs/schema_raw.yaml"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    args = parser.parse_args(argv)

    if args.verify:
        failures = verify_raw(args.data_dir, args.reports_dir)
        for f in failures:
            log.error("verify: %s", f)
        return 1 if failures else 0

    schema = load_schema(args.schema)
    if args.zones:
        ingest_zones(
            schema, args.data_dir, args.reports_dir, opener=opener, sleep=sleep
        )
        return 0
    for month in args.month:
        try:
            ingest_month(
                args.service,
                month,
                schema,
                args.data_dir,
                args.reports_dir,
                force=args.force,
                opener=opener,
                sleep=sleep,
            )
        except MonthNotPublishedError as e:
            log.info("month %s not published yet (%s); nothing to do", month, e)
    return 0
