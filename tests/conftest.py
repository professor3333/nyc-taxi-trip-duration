"""Shared fixtures: params, schema, and builders for canonical-shaped frames."""

from __future__ import annotations

from pathlib import Path

import pytest

from tripduration.config import Params, load_params
from tripduration.ingest import RawSchema, load_schema

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def params() -> Params:
    return load_params(ROOT / "params.yaml")


@pytest.fixture(scope="session")
def raw_schema() -> RawSchema:
    return load_schema(ROOT / "configs" / "schema_raw.yaml")
