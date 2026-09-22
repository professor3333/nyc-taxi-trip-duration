"""Fetch the champion's artefacts from the DVC remote at the champion's commit.

    uv run python scripts/fetch_champion.py [--out build/champion/models]

Reads models/champion.json (git), runs `dvc get . <path> --rev <git_sha>` for
the two DVC-tracked artefacts and `git show <git_sha>:models/model_meta.json`
for the metadata, verifies the md5s against champion.json, and writes them
where the Docker build copies from. Never talks to MLflow (G9).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


def sh(*cmd: str) -> str:
    return subprocess.check_output(cmd, text=True).strip()


def remote_config_args() -> list[str]:
    """`dvc get` clones the repo, so a per-machine remote (.dvc/config.local)
    must be passed explicitly. A committed S3 remote needs nothing."""
    try:
        name = sh("uv", "run", "dvc", "config", "core.remote")
        url = sh("uv", "run", "dvc", "config", f"remote.{name}.url")
    except subprocess.CalledProcessError:
        return []
    return ["--remote", name, "--remote-config", f"url={url}"]


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--champion", type=Path, default=Path("models/champion.json"))
    ap.add_argument("--out", type=Path, default=Path("build/champion/models"))
    ap.add_argument("--repo", default=".")
    args = ap.parse_args()

    champ = json.loads(args.champion.read_text())
    sha, out = champ["git_sha"], args.out
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    remote = remote_config_args()
    for name, key in (
        ("model.pkl", "model_md5"),
        ("fallback_table.parquet", "fallback_md5"),
    ):
        subprocess.check_call(
            [
                "uv",
                "run",
                "dvc",
                "get",
                args.repo,
                f"models/{name}",
                "--rev",
                sha,
                "-o",
                str(out / name),
                *remote,
            ]
        )
        got = md5(out / name)
        if got != champ[key]:
            print(
                f"md5 mismatch for {name}: got {got}, champion.json says {champ[key]}",
                file=sys.stderr,
            )
            return 1
        print(f"{name}: md5 {got} ok")
    (out / "model_meta.json").write_text(
        sh("git", "show", f"{sha}:models/model_meta.json") + "\n"
    )
    shutil.copy(args.champion, out / "champion.json")
    ref_out = out.parent / "reference"
    ref_out.mkdir(exist_ok=True)
    subprocess.check_call(
        [
            "uv",
            "run",
            "dvc",
            "get",
            args.repo,
            "data/reference/zone_centroids.csv",
            "--rev",
            sha,
            "-o",
            str(ref_out / "zone_centroids.csv"),
            "--force",
            *remote,
        ]
    )
    print(f"zone_centroids.csv fetched at {sha[:8]} into {ref_out}")
    print(f"champion v{champ['version']} (commit {sha[:8]}) fetched into {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
