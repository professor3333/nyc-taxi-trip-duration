"""Ingest stage: download one TLC month, normalise its schema, record provenance.

Ingest never mutates rows. It renames columns to the canonical lower-snake
schema in ``configs/schema_raw.yaml``, casts each to its canonical Arrow type,
and adds optional columns that a month lacks as all-null, so every file under
``data/raw/`` has one schema. A column the config does not know is an error:
drift is seen, not absorbed. A month TLC has not published yet (HTTP 404) is a
distinct, expected condition (``MonthNotPublishedError``) that the CLI turns into a
clean exit 0; every other failure propagates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
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

_ARROW_TYPES: dict[str, pa.DataType] = {
    "int64": pa.int64(),
    "float64": pa.float64(),
    "string": pa.string(),
    "timestamp[us]": pa.timestamp("us"),
}

# Anything that behaves like urllib.request.urlopen: takes a URL, returns a
# context manager whose value has .read(n). Injected so tests never hit the
# network and can simulate a 404 without patching globals.
Opener = Callable[[str], Any]


class MonthNotPublishedError(Exception):
    """The source URL returned 404: TLC has not published this month yet."""


class SchemaDriftError(ValueError):
    """The file's columns do not match configs/schema_raw.yaml."""


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


def download(url: str, dest: Path, *, opener: Opener = urllib.request.urlopen) -> str:
    """Stream ``url`` to ``dest`` and return the md5 of the bytes as received.

    Writes to ``dest`` with a ``.part`` suffix first, then renames, so an
    interrupted download never leaves a plausible-looking file behind.
    """
    part = dest.with_suffix(dest.suffix + ".part")
    md5 = hashlib.md5()
    try:
        with opener(url) as resp, part.open("wb") as out:
            while chunk := resp.read(1 << 20):
                md5.update(chunk)
                out.write(chunk)
    except urllib.error.HTTPError as e:
        part.unlink(missing_ok=True)
        if e.code == 404:
            raise MonthNotPublishedError(url) from e
        raise
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    part.replace(dest)
    return md5.hexdigest()


def normalise(table: pa.Table, schema: RawSchema) -> tuple[pa.Table, dict[str, Any]]:
    """Rename to canonical names, add missing optional columns, cast dtypes.

    Returns the normalised table and a dict describing what was done, for the
    ingest report. Raises ``SchemaDriftError`` on an unknown column or a missing
    required one. Never drops or reorders rows.
    """
    vmap = schema.variant_map()
    # Captured first, before any rename/cast/append: this is what TLC shipped.
    source_schema = {f.name: str(f.type) for f in table.schema}
    seen = list(table.column_names)
    unknown = [c for c in seen if c not in vmap]
    if unknown:
        raise SchemaDriftError(
            f"unknown column(s) {unknown}; add to configs/schema_raw.yaml if legitimate"
        )
    renamed = {c: vmap[c] for c in seen if vmap[c] != c}
    table = table.rename_columns([vmap[c] for c in seen])

    present = set(table.column_names)
    missing_required = [
        c.name for c in schema.columns if not c.optional and c.name not in present
    ]
    if missing_required:
        raise SchemaDriftError(f"required column(s) missing: {missing_required}")

    added_null: list[str] = []
    for col in schema.columns:
        if col.name not in present:
            table = table.append_column(
                col.name, pa.nulls(table.num_rows).cast(col.dtype)
            )
            added_null.append(col.name)

    # Canonical order, then a *safe* cast: a value that would not survive the
    # cast (e.g. a fractional passenger_count) raises instead of being altered.
    table = table.select([c.name for c in schema.columns])
    table = table.cast(schema.arrow_schema(), safe=True)
    return table, {
        "columns_seen": seen,
        "source_schema": source_schema,
        "renamed": renamed,
        "added_null": added_null,
    }


def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def ingest_month(
    service: str,
    month: str,
    schema: RawSchema,
    data_dir: Path,
    reports_dir: Path,
    *,
    opener: Opener = urllib.request.urlopen,
) -> Path:
    """Download, normalise and store one month; write its provenance report.

    Returns the path of the written parquet. Raises ``MonthNotPublishedError`` if
    the source returns 404 (nothing is written).
    """
    if not MONTH_RE.match(month):
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    url = schema.trip_url_template.format(service=service, month=month)
    out_dir = data_dir / "raw" / service
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{month}.parquet"
    src_path = out_dir / f"{month}.source.parquet"

    log.info("downloading %s", url)
    source_md5 = download(url, src_path, opener=opener)
    try:
        table = pq.read_table(src_path)
        table, changes = normalise(table, schema)
        pq.write_table(table, out_path)
    finally:
        src_path.unlink(missing_ok=True)

    report = {
        "service": service,
        "month": month,
        "source_url": url,
        "source_md5": source_md5,
        "output_path": str(out_path),
        "output_md5": _md5_file(out_path),
        "rows": table.num_rows,
        "ingested_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "pyarrow_version": pa.__version__,
        **changes,
    }
    _write_report(reports_dir / "ingest" / f"{service}-{month}.json", report)
    log.info(
        "ingested %s %s: rows=%d renamed=%d added_null=%s source_md5=%s",
        service,
        month,
        table.num_rows,
        len(changes["renamed"]),
        changes["added_null"],
        source_md5,
    )
    return out_path


def ingest_zones(
    schema: RawSchema,
    data_dir: Path,
    reports_dir: Path,
    *,
    opener: Opener = urllib.request.urlopen,
) -> Path:
    """Fetch the TLC zone lookup CSV byte-for-byte and check its header."""
    out_dir = data_dir / "reference"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "taxi_zone_lookup.csv"
    md5 = download(schema.zone_lookup_url, out_path, opener=opener)
    table = pcsv.read_csv(out_path)
    expected = list(schema.zone_lookup_columns)
    if table.column_names != expected:
        out_path.unlink()
        raise SchemaDriftError(
            f"zone lookup columns {table.column_names} != expected {expected}"
        )
    report = {
        "source_url": schema.zone_lookup_url,
        "md5": md5,
        "rows": table.num_rows,
        "columns": table.column_names,
        "ingested_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    _write_report(reports_dir / "ingest" / "zones.json", report)
    log.info("ingested zone lookup: rows=%d md5=%s", table.num_rows, md5)
    return out_path


def _month_arg(value: str) -> str:
    if not MONTH_RE.match(value):
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {value!r}")
    return value


def main(
    argv: Sequence[str] | None = None, *, opener: Opener = urllib.request.urlopen
) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest one TLC month or the zone lookup."
    )
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--month", type=_month_arg, help="month to ingest, YYYY-MM")
    what.add_argument("--zones", action="store_true", help="fetch taxi_zone_lookup.csv")
    parser.add_argument("--service", default="yellow")
    parser.add_argument("--schema", type=Path, default=Path("configs/schema_raw.yaml"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    args = parser.parse_args(argv)

    schema = load_schema(args.schema)
    if args.zones:
        ingest_zones(schema, args.data_dir, args.reports_dir, opener=opener)
        return 0
    try:
        ingest_month(
            args.service,
            args.month,
            schema,
            args.data_dir,
            args.reports_dir,
            opener=opener,
        )
    except MonthNotPublishedError as e:
        log.info("month %s not published yet (404 at %s); nothing to do", args.month, e)
        return 0
    return 0
