"""The Compose MLflow server is fully pinned (2026-09-29: an unpinned
transitive SQLAlchemy 2.1 switched postgresql:// to an absent driver and the
registry stopped starting)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MLFLOW = ROOT / "deploy" / "mlflow"


def _pins() -> dict[str, str]:
    lines = [
        ln.strip()
        for ln in (MLFLOW / "requirements.txt").read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    assert all(re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+==[^\s=]+", ln) for ln in lines), (
        lines
    )
    return {n.lower(): v for n, v in (ln.split("==") for ln in lines)}


def test_every_package_and_the_base_image_are_pinned() -> None:
    dockerfile = (MLFLOW / "Dockerfile").read_text()
    assert re.search(r"^FROM \S+@sha256:[0-9a-f]{64}$", dockerfile, re.M)
    assert "--no-deps -r /tmp/requirements.txt" in dockerfile
    assert "psycopg2-binary" in _pins()


def test_server_matches_the_locked_client() -> None:
    lock = (ROOT / "uv.lock").read_text()
    pins = _pins()
    for pkg in ("mlflow", "sqlalchemy"):
        m = re.search(rf'^name = "{pkg}"\nversion = "([^"]+)"', lock, re.M)
        assert m and pins[pkg] == m.group(1), pkg


def test_the_backend_names_its_driver() -> None:
    assert (
        "--backend-store-uri postgresql+psycopg2://"
        in (ROOT / "compose.yaml").read_text()
    )
