"""Promote a version to champion, or roll back to the previous champion.

    uv run python scripts/promote.py --version 2 --reason "beats v1 on 2024-12"
    uv run python scripts/promote.py --rollback --reason "deploy_check failed"
    uv run python scripts/promote.py --status

Checks ADR-0007's gate, sets aliases, writes models/champion.json, appends
docs/promotions.md. Refuses if the alias and champion.json disagree.
"""

import argparse
import json
import os
import sys

from tripduration.registry import RegistryError, promote, rollback, summary

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--version", type=int)
    g.add_argument("--rollback", action="store_true")
    g.add_argument("--status", action="store_true")
    ap.add_argument("--reason", default="")
    ap.add_argument(
        "--force", action="store_true", help="bypass the gate; recorded as FORCED"
    )
    args = ap.parse_args()
    uri = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5001")
    try:
        if args.status:
            print(json.dumps(summary(uri), indent=2))
        elif args.rollback:
            if not args.reason:
                ap.error("--reason is required for --rollback")
            s = rollback(uri, reason=args.reason)
            print(
                f"rolled back: champion is now v{s.version} (was v{s.previous_version})"
            )
        else:
            if not args.reason:
                ap.error("--reason is required for --version")
            s = promote(uri, args.version, reason=args.reason, force=args.force)
            print(f"promoted: champion is now v{s.version} (was v{s.previous_version})")
        print("next: commit models/champion.json and docs/promotions.md")
    except RegistryError as e:
        print(f"refused: {e}", file=sys.stderr)
        sys.exit(2)
