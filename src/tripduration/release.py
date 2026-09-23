"""What a release *is*: every input that decides the service's behaviour.

A release is the immutable combination of

- the model (model, fallback table and feature list, by content hash),
- the reference data it predicts with (zone centroids, holidays),
- the preprocessing and serving code (hash of every file under ``src/``),
- the dependency lock (``uv.lock``, ``pyproject.toml``),
- the configuration (``params.yaml``) and the image recipe (``Dockerfile``,
  whose base images are pinned by digest).

``release_id`` is a sha256 over those component hashes only, so two builds
with the same content get the same id and a documentation-only commit does
not create a "new" release. The commit is recorded but not part of the id.

The image digest cannot be inside the image it identifies; it is bound to
the manifest by the release record deploy.yml writes after verification
(``s3://<bucket>/releases/<release_id>.json``). Rollback re-activates that
recorded image - it never rebuilds (ADR-0012).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

MANIFEST_FILE = "release.json"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def md5_file(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def tree_sha256(root: Path, pattern: str = "**/*.py") -> str:
    """One hash for a source tree: sorted (relative path, content hash) pairs."""
    h = hashlib.sha256()
    for p in sorted(root.glob(pattern)):
        if p.is_file() and "__pycache__" not in p.parts:
            h.update(f"{p.relative_to(root).as_posix()}\0{sha256_file(p)}\n".encode())
    return h.hexdigest()


def release_id(components: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(components, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def build_manifest(
    *,
    champion: dict[str, Any],
    models_dir: Path,
    reference_dir: Path,
    repo: Path,
    commit: str,
) -> dict[str, Any]:
    """Describe the release built from ``models_dir`` + ``reference_dir`` + ``repo``.

    Refuses if a packaged artefact does not match the md5 the champion
    record promises: a manifest must describe what is actually in the image.
    """
    expected = {
        models_dir / "model.pkl": champion["model_md5"],
        models_dir / "fallback_table.parquet": champion["fallback_md5"],
    }
    if champion.get("reference_md5"):
        expected[reference_dir / "zone_centroids.csv"] = champion["reference_md5"]
    if champion.get("holidays_md5"):
        expected[reference_dir / "holidays.csv"] = champion["holidays_md5"]
    for path, want in expected.items():
        got = md5_file(path)
        if got != want:
            raise ValueError(f"{path}: md5 {got} != champion record {want}")

    meta = json.loads((models_dir / "model_meta.json").read_text())
    components = {
        "model": {
            "version": f"v{champion['version']}",
            "model_md5": champion["model_md5"],
            "fallback_md5": champion["fallback_md5"],
            "model_meta_sha256": sha256_file(models_dir / "model_meta.json"),
            "feature_columns": meta.get("feature_columns", []),
        },
        "references": {
            "zone_centroids_md5": md5_file(reference_dir / "zone_centroids.csv"),
            "holidays_md5": md5_file(reference_dir / "holidays.csv"),
        },
        "code": {
            "src_sha256": tree_sha256(repo / "src"),
            "features_sha256": sha256_file(repo / "src/tripduration/features.py"),
        },
        "dependencies": {
            "uv_lock_sha256": sha256_file(repo / "uv.lock"),
            "pyproject_sha256": sha256_file(repo / "pyproject.toml"),
        },
        "config": {
            "params_sha256": sha256_file(repo / "params.yaml"),
            "dockerfile_sha256": sha256_file(repo / "Dockerfile"),
        },
    }
    return {
        "release_id": release_id(components),
        "components": components,
        "provenance": {
            "code_commit": commit,
            "model_trained_commit": champion.get("git_sha", ""),
            "model_run_id": champion.get("run_id", ""),
            "train_months": champion.get("train_months", []),
        },
    }


def read_release_id(models_dir: Path) -> str | None:
    """The running image's release id, or None for a dev build without one."""
    path = models_dir / MANIFEST_FILE
    if not path.exists():
        return None
    return str(json.loads(path.read_text())["release_id"])
