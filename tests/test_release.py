"""A release is content-addressed: every input that decides behaviour changes
its id, nothing else does, and a manifest must describe what it packages."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from tripduration.release import build_manifest, read_release, release_id

ROOT = Path(__file__).resolve().parents[1]


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


@pytest.fixture
def build(tmp_path: Path) -> dict[str, Path]:
    """A miniature repo + build/champion, as deploy.yml lays them out."""
    repo = tmp_path / "repo"
    (repo / "src" / "tripduration").mkdir(parents=True)
    for name in ("features.py", "api.py"):
        (repo / "src" / "tripduration" / name).write_text(f"# {name}\n")
    for name in ("uv.lock", "pyproject.toml", "Dockerfile", "params.yaml", "README.md"):
        (repo / name).write_text(f"{name}\n")
    models = tmp_path / "build" / "models"
    ref = tmp_path / "build" / "reference"
    models.mkdir(parents=True)
    ref.mkdir(parents=True)
    (models / "model.pkl").write_bytes(b"model")
    (models / "fallback_table.parquet").write_bytes(b"table")
    (models / "model_meta.json").write_text(json.dumps({"feature_columns": ["a", "b"]}))
    shutil.copy(
        ROOT / "tests" / "fixtures" / "zone_centroids.csv", ref / "zone_centroids.csv"
    )
    holidays = tmp_path / "holidays.csv"
    holidays.write_text("date\n2025-01-01\n")
    return {"repo": repo, "models": models, "ref": ref, "holidays": holidays}


def _champion(b: dict[str, Path], **over: object) -> dict[str, object]:
    return {
        "version": 3,
        "model_md5": _md5(b["models"] / "model.pkl"),
        "fallback_md5": _md5(b["models"] / "fallback_table.parquet"),
        "reference_md5": _md5(b["ref"] / "zone_centroids.csv"),
        "git_sha": "c" * 40,
        **over,
    }


def _id(b: dict[str, Path], commit: str = "a" * 40, **over: object) -> str:
    m = build_manifest(
        champion=_champion(b, **over),
        models_dir=b["models"],
        reference_dir=b["ref"],
        holidays=b["holidays"],
        repo=b["repo"],
        commit=commit,
    )
    assert m["release_id"] == release_id(m["components"])
    return str(m["release_id"])


def test_same_content_same_id_whatever_the_commit(build: dict[str, Path]) -> None:
    assert _id(build, commit="a" * 40) == _id(build, commit="b" * 40)
    (build["repo"] / "README.md").write_text("docs changed\n")  # not an input
    assert _id(build) == _id(build, commit="c" * 40)


@pytest.mark.parametrize(
    "change",
    [
        "src/tripduration/features.py",  # preprocessing
        "src/tripduration/api.py",  # serving code
        "uv.lock",  # environment
        "pyproject.toml",
        "Dockerfile",
        "params.yaml",  # configuration
        "holidays",  # reference data
        "model_meta.json",  # feature list / model metadata
    ],
)
def test_every_behavioural_input_changes_the_id(
    build: dict[str, Path], change: str
) -> None:
    before = _id(build)
    target = {
        "holidays": build["holidays"],
        "model_meta.json": build["models"] / "model_meta.json",
    }.get(change, build["repo"] / change)
    if target.suffix == ".json":
        target.write_text(json.dumps({"feature_columns": ["a", "b", "c"]}))
    else:
        target.write_text(target.read_text() + "changed\n")
    assert _id(build) != before


def test_the_model_version_is_part_of_the_release(build: dict[str, Path]) -> None:
    assert _id(build) != _id(build, version=4)


def test_a_manifest_refuses_artefacts_the_champion_did_not_promise(
    build: dict[str, Path],
) -> None:
    (build["models"] / "model.pkl").write_bytes(b"other model")
    with pytest.raises(ValueError, match="model.pkl: md5"):
        _id(build, model_md5=_md5(build["ref"] / "zone_centroids.csv"))


def test_read_release(tmp_path: Path) -> None:
    assert read_release(tmp_path) == (None, None)  # dev build
    (tmp_path / "release.json").write_text(json.dumps({"release_id": "f" * 64}))
    assert read_release(tmp_path) == ("f" * 64, None)
    for bad in ('{"release_id": "short"}', "[]", "not json"):
        (tmp_path / "release.json").write_text(bad)
        rid, err = read_release(tmp_path)
        assert rid is None and err


def test_the_image_carries_the_manifest_when_present() -> None:
    text = (ROOT / "Dockerfile").read_text()
    assert "${MODELS_SRC}/release.jso[n] ./models/" in text
