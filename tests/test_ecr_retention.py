"""ECR retention: the live image and its rollback target never expire, however
many rejected releases are pushed after them (audit finding; AWS: a Lambda
whose image is deleted can go to Failed)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ecr_retention import (  # noqa: E402
    Image,
    digest_of,
    pin_tag,
    policy_problems,
    simulate,
    unpin_plan,
)

POLICY = json.loads((ROOT / "deploy" / "aws" / "ecr-lifecycle.json").read_text())
OLD_POLICY = {  # before this change: keep the newest 5 of anything
    "rules": [
        {
            "rulePriority": 1,
            "selection": {
                "tagStatus": "any",
                "countType": "imageCountMoreThan",
                "countNumber": 5,
            },
            "action": {"type": "expire"},
        }
    ]
}


def _d(n: int) -> str:
    return f"sha256:{n:064x}"


def _release(n: int, *, pinned: bool = False) -> Image:
    tags = (f"v1-{n:040x}",) + ((pin_tag(_d(n)),) if pinned else ())
    return Image(_d(n), tags, pushed_at=float(n))


def _after_failed_releases(
    live: int, rollback: int, failed: int, pin: bool
) -> list[Image]:
    older = [_release(n, pinned=pin and n in (live, rollback)) for n in range(1, 4)]
    rejected = [_release(n) for n in range(10, 10 + failed)]
    return older + rejected


def test_the_committed_policy_protects_pins() -> None:
    assert policy_problems(POLICY) == []
    assert "ecr-lifecycle.json" in (ROOT / "deploy" / "aws" / "ecr.sh").read_text()


@pytest.mark.parametrize("failed", [5, 12, 40])
def test_failed_releases_cannot_expire_the_pinned_live_and_rollback(
    failed: int,
) -> None:
    """The audit scenario: newer rejected images push live out of the newest 5."""
    live, rollback = 3, 2
    images = _after_failed_releases(live, rollback, failed, pin=True)
    expired = simulate(POLICY, images)
    assert _d(live) not in expired and _d(rollback) not in expired
    # the rejected images themselves are still cleaned up
    assert len([i for i in images if i.digest not in expired]) <= 5 + 2


def test_without_pins_the_old_policy_expires_the_live_image() -> None:
    """Negative control: the defect, reproduced with the documented rules."""
    images = _after_failed_releases(3, 2, failed=5, pin=False)
    assert _d(3) in simulate(OLD_POLICY, images)
    assert _d(3) in simulate(POLICY, images)  # the rule alone is not enough: pin


def test_simulator_follows_the_documented_example() -> None:
    """AWS 'Examples of lifecycle policies', example B (count rules only):
    a higher rule's tag match shields an image from lower rules."""
    policy = {
        "rules": [
            {
                "rulePriority": 1,
                "selection": {
                    "tagStatus": "tagged",
                    "tagPrefixList": ["alpha"],
                    "countType": "imageCountMoreThan",
                    "countNumber": 1,
                },
            },
            {
                "rulePriority": 3,
                "selection": {
                    "tagStatus": "any",
                    "countType": "imageCountMoreThan",
                    "countNumber": 1,
                },
            },
        ]
    }
    images = [
        Image("A", ("alpha-1", "beta-1"), 1),
        Image("B", (), 2),
        Image("C", ("alpha-2",), 3),
        Image("D", ("git hash",), 4),
        Image("E", (), 4.5),
    ]
    # rule 1 expires A (keeps C); rule 3 keeps the newest (E) and may not
    # touch A or C, so it expires B and D.
    assert simulate(policy, images) == {"A", "B", "D"}


@pytest.mark.parametrize(
    ("policy", "problem"),
    [
        (OLD_POLICY, "no rule selects"),
        (
            {
                "rules": [
                    {**OLD_POLICY["rules"][0], "rulePriority": 1},
                    {**POLICY["rules"][0], "rulePriority": 2},
                ]
            },
            "is evaluated first",
        ),
        (
            {
                "rules": [
                    {
                        **POLICY["rules"][0],
                        "selection": {
                            **POLICY["rules"][0]["selection"],
                            "countNumber": 1,
                        },
                    },
                    POLICY["rules"][1],
                ]
            },
            "must keep at least",
        ),
    ],
)
def test_policies_that_do_not_protect_pins_are_named(
    policy: dict, problem: str
) -> None:
    assert any(problem in p for p in policy_problems(policy))


def test_unpinning_never_deletes_an_image() -> None:
    """BatchDeleteImage on an image's last tag deletes the image."""
    only_pin = Image(_d(1), (pin_tag(_d(1)),), 1)
    old_pin = Image(_d(2), ("v1-x", pin_tag(_d(2))), 2)
    live = Image(_d(3), ("v1-y", pin_tag(_d(3))), 3)
    remove, warnings = unpin_plan([only_pin, old_pin, live], keep={_d(3)})
    assert remove == [(_d(2), pin_tag(_d(2)))]
    assert len(warnings) == 1 and _d(1) in warnings[0]


def test_digests_and_tags() -> None:
    uri = "1.dkr.ecr.us-east-1.amazonaws.com/r@sha256:" + "a" * 64
    assert digest_of(uri) == "sha256:" + "a" * 64
    tag = pin_tag(digest_of(uri))
    assert tag == "keep-sha256-" + "a" * 64 and len(tag) <= 128
    with pytest.raises(ValueError):
        pin_tag("v1-latest")


class FakeECR:
    """Enough of the ECR API for pin/check, with ECR's tag semantics:
    deleting an image's last tag deletes the image."""

    def __init__(self, images: list[Image], policy: dict, expiring: set[str]):
        self.images = {i.digest: list(i.tags) for i in images}
        self.pushed = {i.digest: i.pushed_at for i in images}
        self.policy = policy
        self.expiring = expiring
        self.calls: list[str] = []

    def get_paginator(self, op: str):  # noqa: ANN201
        fake = self

        class P:
            def paginate(self, **_: object):  # noqa: ANN202
                from datetime import UTC, datetime

                if op == "describe_images":
                    yield {
                        "imageDetails": [
                            {
                                "imageDigest": d,
                                "imageTags": t,
                                "imagePushedAt": datetime.fromtimestamp(
                                    fake.pushed[d], UTC
                                ),
                            }
                            for d, t in fake.images.items()
                        ]
                    }
                else:
                    yield {
                        "previewResults": [{"imageDigest": d} for d in fake.expiring]
                    }

        return P()

    def batch_get_image(self, repositoryName: str, imageIds: list) -> dict:  # noqa: N803
        return {"images": [{"imageManifest": "{}", "imageManifestMediaType": "m"}]}

    def put_image(self, **kw: str) -> None:
        self.calls.append(f"tag {kw['imageDigest']} {kw['imageTag']}")
        self.images[kw["imageDigest"]].append(kw["imageTag"])

    def batch_delete_image(self, repositoryName: str, imageIds: list) -> None:  # noqa: N803
        (ident,) = imageIds
        for d, tags in list(self.images.items()):
            if ident["imageTag"] in tags:
                tags.remove(ident["imageTag"])
                self.calls.append(f"untag {d} {ident['imageTag']}")
                if not tags:
                    del self.images[d]

    def get_lifecycle_policy(self, repositoryName: str) -> dict:  # noqa: N803
        return {"lifecyclePolicyText": json.dumps(self.policy)}

    def start_lifecycle_policy_preview(self, repositoryName: str) -> None:  # noqa: N803
        pass

    def get_lifecycle_policy_preview(self, repositoryName: str) -> dict:  # noqa: N803
        return {"status": "COMPLETE"}


def test_pin_exact_moves_pins_without_deleting_images(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ecr_retention import pin

    ecr = FakeECR(
        [_release(1, pinned=True), _release(2, pinned=True), _release(3)],
        POLICY,
        set(),
    )
    # release 3 goes live; release 2 (the old live) is its rollback target
    assert pin(ecr, "r", [_d(3), _d(2)], exact=True) == 0
    assert set(ecr.images) == {_d(1), _d(2), _d(3)}  # nothing deleted
    assert pin_tag(_d(3)) in ecr.images[_d(3)] and pin_tag(_d(2)) in ecr.images[_d(2)]
    assert ecr.images[_d(1)] == [f"v1-{1:040x}"]  # only its pin removed
    assert pin(ecr, "r", [_d(3), _d(2)], exact=True) == 0  # idempotent
    assert sum(c.startswith("tag") for c in ecr.calls) == 1


def test_pin_refuses_a_digest_not_in_the_repository() -> None:
    from ecr_retention import pin

    ecr = FakeECR([_release(1)], POLICY, set())
    assert pin(ecr, "r", [_d(9)], exact=False) == 1 and ecr.calls == []


@pytest.mark.parametrize(
    ("policy", "pinned", "expiring", "ok"),
    [
        (POLICY, True, set(), True),
        (POLICY, False, set(), False),  # not pinned
        (POLICY, True, {3}, False),  # AWS's own preview says it would expire
        (OLD_POLICY, True, set(), False),  # policy has no keep- rule
    ],
)
def test_check_requires_rule_pin_and_a_clean_preview(
    policy: dict, pinned: bool, expiring: set[int], ok: bool
) -> None:
    from ecr_retention import check

    ecr = FakeECR([_release(3, pinned=pinned)], policy, {_d(n) for n in expiring})
    assert (check(ecr, "r", [_d(3)]) == 0) is ok
