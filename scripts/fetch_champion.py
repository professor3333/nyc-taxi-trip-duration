"""Fetch the champion's artefacts from the DVC remote by content hash.

    uv run python scripts/fetch_champion.py [--out build/champion]

Reads ``models/champion.json`` (git) and pulls each artefact from the DVC
remote by its md5, because DVC's remote is content-addressed:
``<remote>/files/md5/<first 2>/<remaining 30>``. Nothing is read from git
history and MLflow is never contacted (G9) — deliberately, since the commit
that trained the model is unreachable on the remote after a squash merge.
Every file's md5 is recomputed and compared with ``champion.json``.

``models/champion_meta.json`` (written by promote/rollback, in git) is the
champion's ``model_meta.json``; it is copied out so the image carries the
feature list the champion was trained with.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def remote_url(repo: Path) -> str:
    """The default DVC remote's URL, from .dvc/config then .dvc/config.local."""
    cfg = configparser.ConfigParser()
    cfg.read([repo / ".dvc" / "config", repo / ".dvc" / "config.local"])
    name = cfg.get("core", "remote", fallback=None)
    if not name:
        raise SystemExit("no default DVC remote configured")
    # DVC writes the section header with literal quotes: ['remote "s3"'].
    for section in (f"'remote \"{name}\"'", f'remote "{name}"'):
        if cfg.has_section(section):
            return cfg.get(section, "url")
    raise SystemExit(f"remote {name} has no url in .dvc/config or .dvc/config.local")


def object_url(base: str, digest: str) -> str:
    return f"{base.rstrip('/')}/files/md5/{digest[:2]}/{digest[2:]}"


def fetch(base: str, digest: str, dest: Path) -> None:
    """dvc get-url handles every remote type DVC supports (s3://, local, …)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        ["uv", "run", "dvc", "get-url", object_url(base, digest), str(dest), "--force"]
    )
    got = md5(dest)
    if got != digest:
        raise SystemExit(f"md5 mismatch for {dest.name}: got {got}, expected {digest}")
    print(f"{dest.name}: md5 {got} ok")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--champion", type=Path, default=Path("models/champion.json"))
    ap.add_argument("--meta", type=Path, default=Path("models/champion_meta.json"))
    ap.add_argument("--out", type=Path, default=Path("build/champion"))
    ap.add_argument("--repo", type=Path, default=Path("."))
    args = ap.parse_args()

    champ = json.loads(args.champion.read_text())
    base = remote_url(args.repo)
    models, reference = args.out / "models", args.out / "reference"
    if args.out.exists():
        shutil.rmtree(args.out)

    fetch(base, champ["model_md5"], models / "model.pkl")
    fetch(base, champ["fallback_md5"], models / "fallback_table.parquet")
    if champ.get("reference_md5"):
        fetch(base, champ["reference_md5"], reference / "zone_centroids.csv")
    else:
        print(
            "champion.json has no reference_md5; copying the working tree's centroids"
        )
        reference.mkdir(parents=True, exist_ok=True)
        shutil.copy(
            Path("data/reference/zone_centroids.csv"), reference / "zone_centroids.csv"
        )

    shutil.copy(args.meta, models / "model_meta.json")
    shutil.copy(args.champion, models / "champion.json")
    print(
        f"champion v{champ['version']} (trained at {champ['git_sha'][:8]}) "
        f"fetched into {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
