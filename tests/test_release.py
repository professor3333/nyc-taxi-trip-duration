"""A release is content-addressed: every behaviour-deciding input is in its id."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tripduration.release import build_manifest, md5_file, read_release_id

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def release(tmp_path: Path) -> dict[str, Path]:
    repo = tmp_path / "repo"
    (repo / "src/tripduration").mkdir(parents=True)
    (repo / "src/tripduration/features.py").write_text("def f(): return 1\n")
    (repo / "src/tripduration/api.py").write_text("x = 1\n")
    for name, body in {
        "uv.lock": "lock\n",
        "pyproject.toml": "[project]\n",
        "params.yaml": "seed: 1\n",
        "Dockerfile": "FROM python@sha256:abc\n",
    }.items():
        (repo / name).write_text(body)
    models, ref = tmp_path / "models", tmp_path / "reference"
    models.mkdir()
    ref.mkdir()
    (models / "model.pkl").write_bytes(b"model")
    (models / "fallback_table.parquet").write_bytes(b"fallback")
    (models / "model_meta.json").write_text(json.dumps({"feature_columns": ["a"]}))
    (ref / "zone_centroids.csv").write_text("id,x\n1,0\n")
    (ref / "holidays.csv").write_text("date\n2025-01-01\n")
    return {"repo": repo, "models": models, "ref": ref}


def _champion(p: dict[str, Path]) -> dict[str, object]:
    return {
        "version": 3,
        "model_md5": md5_file(p["models"] / "model.pkl"),
        "fallback_md5": md5_file(p["models"] / "fallback_table.parquet"),
        "reference_md5": md5_file(p["ref"] / "zone_centroids.csv"),
        "holidays_md5": md5_file(p["ref"] / "holidays.csv"),
        "git_sha": "t" * 40,
    }


def _id(p: dict[str, Path], commit: str = "c" * 40) -> str:
    m = build_manifest(
        champion=_champion(p),
        models_dir=p["models"],
        reference_dir=p["ref"],
        repo=p["repo"],
        commit=commit,
    )
    return str(m["release_id"])


def test_same_content_same_id_regardless_of_commit(release: dict[str, Path]) -> None:
    assert _id(release, "a" * 40) == _id(release, "b" * 40)


@pytest.mark.parametrize(
    "change",
    [
        "src/tripduration/features.py",  # preprocessing code
        "src/tripduration/api.py",  # serving code
        "uv.lock",  # dependencies
        "params.yaml",  # configuration
        "Dockerfile",  # base image digests
    ],
)
def test_any_component_change_is_a_new_release(
    release: dict[str, Path], change: str
) -> None:
    before = _id(release)
    with (release["repo"] / change).open("a") as fh:
        fh.write("# changed\n")
    assert _id(release) != before


def test_reference_change_is_a_new_release(release: dict[str, Path]) -> None:
    """Holidays and centroids are part of the release, not the environment."""
    before = _id(release)
    (release["ref"] / "holidays.csv").write_text("date\n2025-07-04\n")
    assert _id(release) != before  # _champion() re-reads the new md5


def test_refuses_artefacts_that_do_not_match_the_champion(
    release: dict[str, Path],
) -> None:
    champ = _champion(release)
    (release["ref"] / "holidays.csv").write_text("date\n1999-01-01\n")
    with pytest.raises(ValueError, match="holidays.csv"):
        build_manifest(
            champion=champ,
            models_dir=release["models"],
            reference_dir=release["ref"],
            repo=release["repo"],
            commit="c",
        )


def test_read_release_id(release: dict[str, Path], tmp_path: Path) -> None:
    assert read_release_id(release["models"]) is None
    (release["models"] / "release.json").write_text(json.dumps({"release_id": "x"}))
    assert read_release_id(release["models"]) == "x"


def test_real_dockerfile_pins_every_base_image_by_digest() -> None:
    """A floating tag would make the same commit build a different release."""
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    images = [ln.split("=", 1)[1] for ln in lines if re.match(r"ARG \w+_IMAGE=", ln)]
    assert len(images) == 3
    assert all("@sha256:" in i for i in images), images
    assert not [ln for ln in lines if ln.startswith("FROM ") and ":" in ln.split()[1]]
