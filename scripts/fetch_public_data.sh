#!/usr/bin/env bash
# Rebuild every DVC-tracked input from TLC's public URLs - no DVC remote, no
# AWS - and prove each file byte-identical to its committed .dvc pointer.
#
#   scripts/fetch_public_data.sh        # from the repository root
#
# The raw months and the zone lookup are downloaded as served; the zone
# centroids are rebuilt from TLC's shapefile (scripts/build_zone_centroids.py).
# A pointer that does not match means TLC republished that file since it was
# committed: bit-identical reproduction of that commit is then impossible from
# public data, and this says which file, instead of failing later.
set -euo pipefail

MONTHS=()
while read -r m; do MONTHS+=("$m"); done < <(find data/raw/yellow -name '*.parquet.dvc' | sed 's#.*/##; s#\.parquet\.dvc$##' | sort)
echo "[public] months named by the committed pointers: ${MONTHS[*]}"
uv run python scripts/ingest.py --month "${MONTHS[@]}"
uv run python scripts/ingest.py --zones
uv run python scripts/build_zone_centroids.py

echo "[public] checking every .dvc pointer against the downloaded bytes"
uv run python - <<'PY'
import hashlib, pathlib, sys, yaml
bad = 0
for ptr in sorted(pathlib.Path("data").rglob("*.dvc")):
    for out in yaml.safe_load(ptr.read_text())["outs"]:
        path = ptr.parent / out["path"]
        got = hashlib.md5(path.read_bytes()).hexdigest() if path.exists() else "missing"
        ok = got == out["md5"]
        bad += not ok
        print(f"  {'ok  ' if ok else 'DIFF'} {path}  {got}" + ("" if ok else f" != {out['md5']}"))
sys.exit(1 if bad else 0)
PY
echo "[public] every input matches its pointer"
