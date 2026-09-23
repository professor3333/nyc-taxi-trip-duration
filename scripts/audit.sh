#!/usr/bin/env bash
# Dependency vulnerability audit from uv.lock (pip-audit, OSV + PyPI advisories).
#   runtime  exactly what the image installs: any finding fails, no ignores
#   all      every group (train, dev): fails unless the ID is accepted, with a
#            reason and a review date, in security/pip-audit-ignore.txt
set -euo pipefail
ROOT=$(git rev-parse --show-toplevel)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
AUDIT=(uvx --from "pip-audit==2.9.0" pip-audit --disable-pip --no-deps --progress-spinner off)

uv export --frozen --no-dev --no-group train --no-emit-project --no-hashes -q -o "$TMP/runtime.txt"
echo "[audit] runtime ($(grep -c '==' "$TMP/runtime.txt") packages, no ignores)"
"${AUDIT[@]}" -r "$TMP/runtime.txt"

uv export --frozen --all-groups --no-emit-project --no-hashes -q -o "$TMP/all.txt"
IGNORES=()
while read -r id _; do IGNORES+=(--ignore-vuln "$id"); done < <(grep -v '^#' "$ROOT/security/pip-audit-ignore.txt" | grep -v '^\s*$')
echo "[audit] all groups ($(grep -c '==' "$TMP/all.txt") packages, $(( ${#IGNORES[@]} / 2 )) accepted IDs, see security/pip-audit-ignore.txt)"
"${AUDIT[@]}" -r "$TMP/all.txt" "${IGNORES[@]}"
