"""Settle a Lambda update, and restore a previous image, with explicit decisions.

    uv run python scripts/lambda_restore.py settle  --function FN [--timeout 300]
    uv run python scripts/lambda_restore.py restore --function FN --image PREV

``aws lambda wait function-updated`` fails when an update ends ``Failed``. The
restore step used to call it before restoring, so the case restoration exists
for exited the step before anything was restored. Here every outcome of
``LastUpdateStatus`` is handled explicitly, and waiting is bounded:

- ``Successful``: the update finished, with the function on its new code.
- ``Failed``: AWS aborted the change, and the previous code and configuration
  stay Active and serving (AWS, "Tracking the state of Lambda functions").
  The reason and reason code are printed.
- ``InProgress`` past ``--timeout``: nothing else can be started (Lambda
  refuses a second update), so it stops and says so.

``restore`` decides: it settles first. If the function is Successful and
already on PREV, there is nothing to do. Failed or Successful on another
image: update to PREV, which is correct whether or not AWS kept PREV. Still
InProgress: exit 3, and the run fails with the manual next step. The
restore's own update must then settle Successful on PREV's digest.

Exit codes: 0 done; 1 settled Failed (settle) or the restore update failed;
3 still InProgress at the deadline.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Settled:
    status: str  # Successful | Failed | InProgress (deadline reached)
    state: str
    reason: str
    code: str
    image: str  # the resolved image digest URI the function reports


def digest_of(image: str) -> str:
    return image.rsplit("@", 1)[-1]


def settle(
    lam: Any,
    fn: str,
    timeout: float,
    poll: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Settled:
    deadline = clock() + timeout
    while True:
        cfg = lam.get_function_configuration(FunctionName=fn)
        status = cfg.get("LastUpdateStatus", "Successful")
        if status != "InProgress" or clock() >= deadline:
            code = lam.get_function(FunctionName=fn)["Code"]
            return Settled(
                status=status,
                state=cfg.get("State", ""),
                reason=cfg.get("LastUpdateStatusReason", ""),
                code=cfg.get("LastUpdateStatusReasonCode", ""),
                image=code.get("ResolvedImageUri") or code.get("ImageUri", ""),
            )
        sleep(poll)


def _say(s: Settled, what: str) -> None:
    extra = f": {s.code} {s.reason}".rstrip() if s.status == "Failed" else ""
    print(
        f"{what}: LastUpdateStatus {s.status}, State {s.state}, image {s.image}{extra}"
    )


def restore(
    lam: Any,
    fn: str,
    prev: str,
    timeout: float,
    poll: float = 5.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    from botocore.exceptions import ClientError

    def wait(what: str) -> Settled:
        s = settle(lam, fn, timeout, poll, clock, sleep)
        _say(s, what)
        return s

    now = wait("before restore")
    if now.status == "InProgress":
        print(
            "::error::the failed deploy's update is still InProgress after "
            f"{timeout:.0f} s; a restore cannot start until it ends. Re-run: python "
            f"scripts/lambda_restore.py restore --function {fn} --image {prev}"
        )
        return 3
    if now.status == "Successful" and digest_of(now.image) == digest_of(prev):
        print("decision: already on the previous image; nothing to update")
    else:
        why = (
            "update Failed (AWS kept the previous code; re-applying it explicitly)"
            if now.status == "Failed"
            else "function is on the failed release"
        )
        print(f"decision: {why} -> update to {prev}")
        for attempt in (1, 2):
            try:
                lam.update_function_code(FunctionName=fn, ImageUri=prev)
                break
            except ClientError as e:  # an update slipped in between: settle, retry
                if e.response["Error"]["Code"] != "ResourceConflictException" or (
                    attempt == 2
                ):
                    raise
                if wait("conflict, settling again").status == "InProgress":
                    return 3
        after = wait("after restore")
        if after.status != "Successful":
            print(f"::error::the restore update ended {after.status}: {after.reason}")
            return 3 if after.status == "InProgress" else 1
        if digest_of(after.image) != digest_of(prev):
            print(f"::error::restored, but the function reports {after.image}")
            return 1
    print(f"restored: {fn} runs {prev}")
    return 0


def main(argv: list[str] | None = None, lam: Any = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("action", choices=["settle", "restore"])
    ap.add_argument("--function", required=True)
    ap.add_argument("--image", help="restore: the image to put back")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--poll", type=float, default=5.0)
    args = ap.parse_args(argv)
    if lam is None:
        import boto3

        lam = boto3.client("lambda")
    if args.action == "settle":
        s = settle(lam, args.function, args.timeout, args.poll)
        _say(s, "update")
        if s.status == "Failed":
            print(f"::error::Lambda update Failed: {s.code} {s.reason}")
            return 1
        if s.status == "InProgress":
            print(f"::error::Lambda update still InProgress after {args.timeout:.0f} s")
            return 3
        return 0
    if not args.image:
        ap.error("restore needs --image")
    return restore(lam, args.function, args.image, args.timeout, args.poll)


if __name__ == "__main__":
    sys.exit(main())
