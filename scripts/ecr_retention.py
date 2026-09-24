"""Keep the serving image and its rollback target out of ECR's lifecycle expiry.

    uv run python scripts/ecr_retention.py pin --repo R --digest D1 [--digest D2] \
        [--exact]
    uv run python scripts/ecr_retention.py check --repo R --protect D1 [--protect D2]
    uv run python scripts/ecr_retention.py pinned --repo R   # one digest per line

The lifecycle policy (``deploy/aws/ecr-lifecycle.json``) keeps the newest 5
images of any tag. Rejected releases are pushed too (the scan and the smoke
test run after the push), so a few failed deploys could push the image Lambda
runs on out of the newest 5; AWS then expires it and the function can go to
``Failed``. Protection is explicit:

- ``pin`` gives each digest a ``keep-<hex digest>`` tag. With ``--exact`` it
  also removes ``keep-`` tags from every other image, so the pinned set is
  exactly the live image and its rollback target. A pin is removed only from
  an image that keeps another tag: deleting an image's last tag through
  ``BatchDeleteImage`` would delete the image.
- The policy's priority-1 rule selects ``keep-`` tags. AWS: "An image that
  matches the tagging requirements of a rule cannot be expired or archived by
  a rule with a lower priority", so the keep-last-5 rule cannot touch pins.
- ``check`` proves it against AWS's own evaluator: the repository's policy
  must have that rule first, every protected digest must carry its pin, and a
  lifecycle *preview* (a dry run, nothing is deleted) must not list any
  protected digest for expiry.

``simulate`` implements the documented evaluation rules for tests; the preview
is what decides in production.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

PIN_PREFIX = "keep-"
MAX_PINS = 10  # the keep rule's countNumber; pins beyond it would expire


def pin_tag(digest: str) -> str:
    """``sha256:ab..`` -> ``keep-sha256-ab..`` (tags cannot contain ':')."""
    algo, _, hexpart = digest.partition(":")
    if not hexpart:
        raise ValueError(f"not an image digest: {digest!r}")
    return f"{PIN_PREFIX}{algo}-{hexpart}"


def digest_of(image: str) -> str:
    """``repo@sha256:..`` or ``sha256:..`` -> ``sha256:..``."""
    return image.rsplit("@", 1)[-1]


@dataclass(frozen=True)
class Image:
    digest: str
    tags: tuple[str, ...]
    pushed_at: float


def policy_problems(policy: Mapping[str, Any]) -> list[str]:
    """Why ``policy`` does not protect ``keep-`` pins (empty = it does)."""
    rules = sorted(policy.get("rules", []), key=lambda r: r["rulePriority"])
    if not rules:
        return []  # no policy rules: nothing is ever expired
    keep = [
        r
        for r in rules
        if r["selection"].get("tagStatus") == "tagged"
        and PIN_PREFIX in r["selection"].get("tagPrefixList", [])
    ]
    if not keep:
        return [f"no rule selects the '{PIN_PREFIX}' pin tags"]
    first = keep[0]
    out = []
    if first is not rules[0]:
        out.append(
            f"the '{PIN_PREFIX}' rule has priority {first['rulePriority']}, but a "
            f"rule with priority {rules[0]['rulePriority']} is evaluated first"
        )
    sel = first["selection"]
    if sel.get("countType") != "imageCountMoreThan" or sel.get("countNumber", 0) < (
        MAX_PINS
    ):
        out.append(
            f"the '{PIN_PREFIX}' rule must keep at least {MAX_PINS} images "
            "(imageCountMoreThan)"
        )
    return out


def _matches(rule: Mapping[str, Any], img: Image) -> bool:
    sel = rule["selection"]
    status = sel["tagStatus"]
    if status == "untagged":
        return not img.tags
    if status == "tagged":
        prefixes = sel.get("tagPrefixList", [])
        return bool(img.tags) and all(
            any(t.startswith(p) for t in img.tags) for p in prefixes
        )
    return True  # any


def simulate(policy: Mapping[str, Any], images: Sequence[Image]) -> set[str]:
    """Digests the documented lifecycle evaluation would expire (count rules).

    All rules are evaluated on all images; each rule may mark only images
    that no higher-priority rule matched by its tagging requirements, and
    each image is expired by at most one rule.
    """
    rules = sorted(policy.get("rules", []), key=lambda r: r["rulePriority"])
    expired: set[str] = set()
    claimed: set[str] = set()  # matched by the tag requirements of a higher rule
    for rule in rules:
        sel = rule["selection"]
        if sel["countType"] != "imageCountMoreThan":
            raise NotImplementedError(sel["countType"])
        matched = [i for i in images if _matches(rule, i)]
        newest_first = sorted(matched, key=lambda i: i.pushed_at, reverse=True)
        for img in newest_first[sel["countNumber"] :]:
            if img.digest not in claimed and img.digest not in expired:
                expired.add(img.digest)
        claimed |= {i.digest for i in matched}
    return expired


def unpin_plan(
    images: Iterable[Image], keep: set[str]
) -> tuple[list[tuple[str, str]], list[str]]:
    """(digest, tag) pins to remove, and warnings for pins left in place."""
    remove: list[tuple[str, str]] = []
    warnings: list[str] = []
    for img in images:
        if img.digest in keep:
            continue
        pins = [t for t in img.tags if t.startswith(PIN_PREFIX)]
        others = [t for t in img.tags if not t.startswith(PIN_PREFIX)]
        if pins and not others:
            warnings.append(
                f"{img.digest} has no tag besides {pins}; left pinned "
                "(removing its last tag would delete it)"
            )
            continue
        remove += [(img.digest, t) for t in pins]
    return remove, warnings


# --- AWS -------------------------------------------------------------------------


def _images(ecr: Any, repo: str) -> list[Image]:
    out = []
    for page in ecr.get_paginator("describe_images").paginate(repositoryName=repo):
        for d in page["imageDetails"]:
            out.append(
                Image(
                    d["imageDigest"],
                    tuple(d.get("imageTags", [])),
                    d["imagePushedAt"].timestamp(),
                )
            )
    return out


def pin(ecr: Any, repo: str, digests: Sequence[str], exact: bool) -> int:
    from botocore.exceptions import ClientError

    if len(digests) > MAX_PINS:
        print(f"refusing to pin {len(digests)} > {MAX_PINS} images", file=sys.stderr)
        return 1
    images = {i.digest: i for i in _images(ecr, repo)}
    for d in digests:
        if d not in images:
            print(f"ERROR {d} is not in {repo}", file=sys.stderr)
            return 1
        tag = pin_tag(d)
        if tag in images[d].tags:
            print(f"pinned   {d} ({tag}, already)")
            continue
        got = ecr.batch_get_image(repositoryName=repo, imageIds=[{"imageDigest": d}])
        (img,) = got["images"]
        try:
            ecr.put_image(
                repositoryName=repo,
                imageManifest=img["imageManifest"],
                imageManifestMediaType=img["imageManifestMediaType"],
                imageTag=tag,
                imageDigest=d,
            )
        except ClientError as e:  # a concurrent run pinned it first
            if e.response["Error"]["Code"] != "ImageAlreadyExistsException":
                raise
        print(f"pinned   {d} ({tag})")
    if exact:
        remove, warnings = unpin_plan(images.values(), set(digests))
        for w in warnings:
            print(f"WARNING {w}")
        for d, tag in remove:
            ecr.batch_delete_image(repositoryName=repo, imageIds=[{"imageTag": tag}])
            print(f"unpinned {d} ({tag})")
    return 0


def _preview(ecr: Any, repo: str) -> set[str]:
    """Digests AWS's evaluator would expire now (a dry run)."""
    from botocore.exceptions import ClientError

    for _ in range(30):
        try:
            ecr.start_lifecycle_policy_preview(repositoryName=repo)
            break
        except ClientError as e:
            if (
                e.response["Error"]["Code"]
                != "LifecyclePolicyPreviewInProgressException"
            ):
                raise
            time.sleep(5)
    for _ in range(60):
        res = ecr.get_lifecycle_policy_preview(repositoryName=repo)
        if res["status"] == "COMPLETE":
            out: set[str] = set()
            for page in ecr.get_paginator("get_lifecycle_policy_preview").paginate(
                repositoryName=repo
            ):
                out |= {r["imageDigest"] for r in page.get("previewResults", [])}
            return out
        if res["status"] in ("FAILED", "EXPIRED"):
            raise RuntimeError(f"lifecycle preview {res['status']}")
        time.sleep(5)
    raise TimeoutError("lifecycle preview did not complete in 5 minutes")


def check(ecr: Any, repo: str, protect: Sequence[str]) -> int:
    import json

    from botocore.exceptions import ClientError

    failures: list[str] = []
    try:
        text = ecr.get_lifecycle_policy(repositoryName=repo)["lifecyclePolicyText"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "LifecyclePolicyNotFoundException":
            raise
        print(f"PASS  {repo} has no lifecycle policy: nothing expires")
        return 0
    failures += [f"policy: {p}" for p in policy_problems(json.loads(text))]
    images = {i.digest: i for i in _images(ecr, repo)}
    for d in protect:
        if d not in images:
            failures.append(f"{d} is not in {repo} (already expired or deleted?)")
        elif pin_tag(d) not in images[d].tags:
            failures.append(f"{d} is not pinned ({pin_tag(d)} missing)")
    expiring = _preview(ecr, repo)
    for d in protect:
        if d in expiring:
            failures.append(f"{d} WOULD EXPIRE (lifecycle preview)")
    pinned = sum(
        1 for i in images.values() if any(t.startswith(PIN_PREFIX) for t in i.tags)
    )
    print(
        f"{'FAIL' if failures else 'PASS'}  retention of {len(protect)} protected "
        f"image(s) in {repo}: {len(images)} images, {len(expiring)} would expire, "
        f"{pinned} pinned"
    )
    for f in failures:
        print(f"FAIL  {f}")
    return 1 if failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pin")
    p.add_argument("--repo", required=True)
    p.add_argument("--digest", action="append", required=True, help="image or digest")
    p.add_argument("--exact", action="store_true", help="unpin every other image")
    sub.add_parser("pinned").add_argument("--repo", required=True)
    c = sub.add_parser("check")
    c.add_argument("--repo", required=True)
    c.add_argument("--protect", action="append", required=True, help="image or digest")
    args = ap.parse_args(argv)

    import boto3

    ecr = boto3.client("ecr")
    if args.cmd == "pinned":
        for img in _images(ecr, args.repo):
            if any(t.startswith(PIN_PREFIX) for t in img.tags):
                print(img.digest)
        return 0
    if args.cmd == "pin":
        digests = list(dict.fromkeys(digest_of(d) for d in args.digest if d))
        return pin(ecr, args.repo, digests, args.exact)
    digests = list(dict.fromkeys(digest_of(d) for d in args.protect if d))
    return check(ecr, args.repo, digests)


if __name__ == "__main__":
    sys.exit(main())
