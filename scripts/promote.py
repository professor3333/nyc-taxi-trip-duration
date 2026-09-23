"""Promote a version to champion, or roll back to the previous champion.

    uv run python scripts/promote.py --version 2 --reason "beats v1 on 2024-12"
    uv run python scripts/promote.py --rollback --reason "deploy_check failed"
    uv run python scripts/promote.py --status
    uv run python scripts/promote.py --recover   # finish an interrupted operation
    uv run python scripts/promote.py --abort     # or undo it

Checks ADR-0007's gate, then runs one transaction: verify and stage every new
file, write a journal, move the aliases, install the files. A failure before
the journal changes nothing; after it, every command refuses until --recover
or --abort. Refuses if the alias, champion.json and the fixture disagree.
"""

import argparse
import json
import os
import sys

from tripduration.registry import (
    RegistryError,
    abort,
    promote,
    recover,
    refresh,
    rollback,
    summary,
)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--version", type=int)
    g.add_argument("--rollback", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument(
        "--refresh",
        action="store_true",
        help="rewrite the current champion's release record; no alias change",
    )
    g.add_argument(
        "--recover", action="store_true", help="finish an interrupted operation"
    )
    g.add_argument("--abort", action="store_true", help="undo an interrupted operation")
    ap.add_argument("--reason", default="")
    ap.add_argument(
        "--force", action="store_true", help="bypass the gate; recorded as FORCED"
    )
    args = ap.parse_args()
    uri = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5001")
    try:
        if args.recover:
            j = recover(uri)
            print(f"finished the interrupted {j['action']} to v{j['version']}")
        elif args.abort:
            j = abort(uri)
            print(f"undid the interrupted {j['action']} to v{j['version']}")
            print("aliases and release files are back to their prior state")
            sys.exit(0)
        elif args.refresh:
            s = refresh(uri)
            print(f"release record refreshed for v{s.version} (aliases unchanged)")
        elif args.status:
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
