"""The committed release record, not a fixture model: models/champion.json,
champion_meta.json and champion_fixture.csv are what deploy.yml ships and
checks, so they must agree with each other and with the code that serves
them. (The artefacts themselves live in the DVC remote; deploy.yml fetches
them by these md5s and proves the live service reproduces the fixture, 80/80.)
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pandas as pd

from tripduration.evaluate import FIXTURE_HOURS, FIXTURE_ROUTES
from tripduration.features import FEATURE_COLUMNS
from tripduration.registry import ChampionState

MODELS = Path(__file__).resolve().parents[1] / "models"


def test_champion_record_is_complete() -> None:
    state = ChampionState.read(MODELS / "champion.json")
    assert state is not None and state.version >= 1
    for md5 in (state.model_md5, state.fallback_md5, state.reference_md5):
        assert len(md5) == 32 and int(md5, 16) >= 0
    assert len(state.git_sha) == 40 and len(state.fixture_sha256) == 64
    assert state.train_months and state.test_month > max(state.train_months)
    assert state.mae_test_model < state.mae_test_fallback  # ADR-0007 gate held


def test_fixture_is_the_one_the_record_vouches_for() -> None:
    state = ChampionState.read(MODELS / "champion.json")
    assert state is not None
    got = hashlib.sha256((MODELS / "champion_fixture.csv").read_bytes()).hexdigest()
    assert got == state.fixture_sha256


def test_fixture_covers_exactly_the_code_defined_grid() -> None:
    rows = list(csv.DictReader((MODELS / "champion_fixture.csv").open()))
    grid = {
        (pu, do, f"2025-01-{day:02d} {hour:02d}:30:00")
        for pu, do in FIXTURE_ROUTES
        for hour in FIXTURE_HOURS
        for day in (6, 11)
    }
    seen = {
        (int(r["pu_location_id"]), int(r["do_location_id"]), r["departure_time"])
        for r in rows
    }
    assert seen == grid and len(rows) == len(grid) == 80
    frame = pd.DataFrame(rows).astype({"model_min": float, "fallback_min": float})
    assert (frame["model_min"] > 0).all() and (frame["fallback_min"] > 0).all()


def test_champion_meta_matches_the_serving_code() -> None:
    """The API refuses a model whose feature list differs from the code's;
    this is the same check before a deploy instead of after it."""
    meta = json.loads((MODELS / "champion_meta.json").read_text())
    assert meta["feature_columns"] == list(FEATURE_COLUMNS)
    state = ChampionState.read(MODELS / "champion.json")
    assert state is not None
    assert list(meta["train_months"]) == list(state.train_months)
