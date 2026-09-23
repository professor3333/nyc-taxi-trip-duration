"""`make reproduce`'s comparator gives no false assurance.

Each test is a way the previous comparator passed something it should not
have: NaN (NaN > tol is False), values rounded before comparison, and
expected results read from a mutable working tree.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from compare_run import compare_metrics, compare_predictions, git_file  # noqa: E402

HEADER = (
    "pu_location_id,do_location_id,departure_time,"
    "model_min,fallback_min,fallback_level\n"
)


def _csv(*values: str) -> str:
    return HEADER + "".join(
        f"132,161,2025-01-06 0{i}:30:00,{v},31.1,pair_hour\n"
        for i, v in enumerate(values)
    )


def test_identical_predictions_pass() -> None:
    assert compare_predictions(_csv("28.5", "12.25"), _csv("28.5", "12.25"), 0.0) == []


@pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-inf", "", "abc"])
@pytest.mark.parametrize("side", ["expected", "reproduced"])
def test_non_finite_or_unparsable_fails_on_either_side(bad: str, side: str) -> None:
    good = _csv("28.5")
    bad_csv = _csv(bad)
    exp, rep = (bad_csv, good) if side == "expected" else (good, bad_csv)
    problems = compare_predictions(exp, rep, 1e9)  # even with a huge tolerance
    assert problems and "non-finite or unparsable" in problems[0]


def test_one_ulp_difference_fails_at_default_tolerance() -> None:
    x = 28.967310603254
    y = float(np.nextafter(x, np.inf))
    assert compare_predictions(_csv(repr(x)), _csv(repr(y)), 0.0)


def test_empty_expected_fails() -> None:
    assert compare_predictions(HEADER, HEADER, 0.0) == [
        "expected predictions are empty"
    ]


def test_fixture_csv_round_trips_bit_exactly(tmp_path: Path) -> None:
    """evaluate writes unrounded floats; reading them back loses no bits."""
    rng = np.random.default_rng(0)
    values = rng.uniform(0, 200, 1000)
    pd.DataFrame({"model_min": values}).to_csv(tmp_path / "f.csv", index=False)
    back = pd.read_csv(tmp_path / "f.csv", float_precision="round_trip")["model_min"]
    assert np.array_equal(back.to_numpy().view(np.int64), values.view(np.int64))


def test_evaluate_does_not_round_fixture_predictions() -> None:
    src = (ROOT / "src" / "tripduration" / "evaluate.py").read_text()
    assert "np.round(pred_model" not in src and "np.round(pred_fb" not in src


@pytest.mark.parametrize(
    ("expected", "reproduced", "needle"),
    [
        ({"mae": float("nan")}, {"mae": float("nan")}, "non-finite"),
        ({"mae": 3.1}, {"mae": float("inf")}, "non-finite"),
        ({"mae": 3.1, "rmse": 5.0}, {"mae": 3.1}, "missing from reproduced"),
        ({"mae": 3.1}, {"mae": 3.1, "extra": 1}, "missing from expected"),
        ({"mae": 3.1}, {"mae": "3.1"}, "expected=3.1"),
        ({"a": [1.0, 2.0]}, {"a": [1.0, float("nan")]}, "non-finite"),
    ],
)
def test_metric_problems_are_reported(expected, reproduced, needle) -> None:  # type: ignore[no-untyped-def]
    problems = compare_metrics(json.dumps(expected), json.dumps(reproduced), 1e9)
    assert any(needle in p for p in problems), problems


def test_git_ignored_metric_and_equal_metrics_pass() -> None:
    a = json.dumps({"git_sha": "a", "test": {"mae": 3.5}})
    b = json.dumps({"git_sha": "b", "test": {"mae": 3.5}})
    assert compare_metrics(a, b, 0.0) == []


def test_expected_comes_from_the_commit_not_the_working_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    target = tmp_path / "reports" / "eval" / "fixture_predictions.csv"
    target.parent.mkdir(parents=True)
    target.write_text(_csv("28.5"))
    git("add", ".")
    git("commit", "-qm", "results")
    sha = git("rev-parse", "HEAD")
    target.write_text(_csv("99.9"))  # the working tree now disagrees
    monkeypatch.chdir(tmp_path)
    assert git_file(sha, Path("reports/eval/fixture_predictions.csv")) == _csv("28.5")
    with pytest.raises(SystemExit):
        git_file(sha, Path("reports/eval/not_committed.csv"))
