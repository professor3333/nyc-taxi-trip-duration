"""The raw TLC schema contract: ``configs/schema_raw.yaml``, checked and applied.

Shared by ingest (which refuses a file whose columns drift) and by the first
pipeline stages (which rename and cast to the canonical schema). It holds no
network or file-acquisition code, so changing how months are downloaded does
not invalidate the DVC stages that only need the contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import yaml

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

_ARROW_TYPES: dict[str, pa.DataType] = {
    "int64": pa.int64(),
    "float64": pa.float64(),
    "string": pa.string(),
    "timestamp[us]": pa.timestamp("us"),
}


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
