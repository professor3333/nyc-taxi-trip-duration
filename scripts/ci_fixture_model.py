"""Train a tiny model + fallback table from tests/fixtures for the CI container
smoke, where no DVC remote is available. Not a real model.

    uv run python scripts/ci_fixture_model.py --out build/ci-models
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

from tripduration import prepare, train, validate
from tripduration.config import load_params
from tripduration.features import ReferenceData
from tripduration.ingest import load_schema

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("build/ci-models"))
    args = ap.parse_args()
    params = load_params(ROOT / "params.yaml")
    schema = load_schema(ROOT / "configs" / "schema_raw.yaml")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shutil.copytree(FIX / "raw", root / "raw")
        (root / "reference").mkdir()
        shutil.copy(
            FIX / "zone_centroids.csv", root / "reference" / "zone_centroids.csv"
        )
        p = replace(
            params,
            n_threads=1,
            data=replace(
                params.data,
                raw_dir=root / "raw",
                validated_dir=root / "validated",
                reference_dir=root / "reference",
            ),
            model={**params.model, "max_iter": 30},
        )
        ref = ReferenceData.load(
            root / "reference" / "zone_centroids.csv", ROOT / "configs" / "holidays.csv"
        )
        validate.run(p, schema, root / "reports")
        prepare.run(p, ref, root / "processed", root / "reports" / "prepare.json")
        meta = train.run(
            p,
            ref,
            root / "processed",
            args.out,
            root / "reports" / "prepare.json",
            tracking_uri=None,
        )
    # champion.json for the image: md5s that match what was just written, and
    # version 0 - an integer like every registry version (the API rejects any
    # other type, ADR-0012) but never a real one, since the registry starts at 1.
    import hashlib

    args.out.parent.mkdir(parents=True, exist_ok=True)
    (args.out / "champion.json").write_text(
        json.dumps(
            {
                "model_name": "ci-fixture",
                "version": 0,
                "model_md5": hashlib.md5(
                    (args.out / "model.pkl").read_bytes()
                ).hexdigest(),
                "fallback_md5": hashlib.md5(
                    (args.out / "fallback_table.parquet").read_bytes()
                ).hexdigest(),
                "git_sha": meta["git_sha"],
            }
        )
    )
    print(f"fixture model written to {args.out} (val MAE {meta['val_mae_model']:.3f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
