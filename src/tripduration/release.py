"""What a release *is*: every input that decides the service's behaviour (ADR-0014).

A release is the immutable combination of

- the model: model, fallback table and feature list, by content hash;
- the reference data it predicts with: zone centroids and holidays;
- the preprocessing and serving code: a hash over every file under ``src/``;
- the environment: ``uv.lock``, ``pyproject.toml`` and the ``Dockerfile``
  (base image pinned by digest);
- the configuration: ``params.yaml``.

``release_id`` is a sha256 over those component hashes only. Two builds with
the same content get the same id, and a documentation-only commit is not a
new release. The commit is recorded as provenance but is not part of the id.

The image digest cannot be inside the image it identifies. After
verification, ``deploy.yml`` binds it to the manifest in the release ledger
(``scripts/releases.py``). A rollback re-activates a recorded release (that
image, that Lambda version); it never rebuilds an old model with today's
code.
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
    canon = json.dumps(components, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def build_manifest(
    *,
    champion: dict[str, Any],
    models_dir: Path,
    reference_dir: Path,
    holidays: Path,
    repo: Path,
    commit: str,
) -> dict[str, Any]:
    """Describe the release built from ``models_dir``, the references and ``repo``.

    Refuses when a packaged artefact does not match the md5 the champion
    record promises: a manifest must describe what is actually in the image.
    """
    expected = {
        models_dir / "model.pkl": champion["model_md5"],
        models_dir / "fallback_table.parquet": champion["fallback_md5"],
    }
    if champion.get("reference_md5"):
        expected[reference_dir / "zone_centroids.csv"] = champion["reference_md5"]
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
            "feature_columns": list(meta.get("feature_columns", [])),
        },
        "references": {
            "zone_centroids_md5": md5_file(reference_dir / "zone_centroids.csv"),
            "holidays_md5": md5_file(holidays),
        },
        "code": {
            "src_sha256": tree_sha256(repo / "src"),
            "features_sha256": sha256_file(repo / "src/tripduration/features.py"),
        },
        "environment": {
            "uv_lock_sha256": sha256_file(repo / "uv.lock"),
            "pyproject_sha256": sha256_file(repo / "pyproject.toml"),
            "dockerfile_sha256": sha256_file(repo / "Dockerfile"),
        },
        "config": {"params_sha256": sha256_file(repo / "params.yaml")},
    }
    return {
        "release_id": release_id(components),
        "components": components,
        "provenance": {
            "code_commit": commit,
            "model_trained_commit": champion.get("git_sha", ""),
            "model_run_id": champion.get("run_id", ""),
            "train_months": list(champion.get("train_months", [])),
        },
    }


def read_release(models_dir: Path) -> tuple[str | None, str | None]:
    """(release id, error) of the running image. No manifest is not an error
    (a dev build, or an image built before ADR-0014); an unreadable one is."""
    path = models_dir / MANIFEST_FILE
    if not path.exists():
        return None, None
    try:
        doc = json.loads(path.read_text())
        rid = doc["release_id"] if isinstance(doc, dict) else None
        if not isinstance(rid, str) or len(rid) != 64:
            raise ValueError(f"release_id is {rid!r}")
        return rid, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
