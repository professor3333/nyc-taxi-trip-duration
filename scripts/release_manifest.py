"""Write build/champion/models/release.json for the image deploy.yml builds.

    uv run python scripts/release_manifest.py --commit $GITHUB_SHA

Run after fetch_champion.py. Prints the release id; the Dockerfile copies the
file into the image and ``/version`` reports the id.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tripduration.release import MANIFEST_FILE, build_manifest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", type=Path, default=Path("build/champion"))
    ap.add_argument("--repo", type=Path, default=Path("."))
    ap.add_argument("--commit", required=True)
    args = ap.parse_args()
    models, reference = args.build / "models", args.build / "reference"
    champion = json.loads((models / "champion.json").read_text())
    manifest = build_manifest(
        champion=champion,
        models_dir=models,
        reference_dir=reference,
        repo=args.repo,
        commit=args.commit,
    )
    (models / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(manifest["release_id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
