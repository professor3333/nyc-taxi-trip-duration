"""The release ledger: what a rollback restores, and which release is deployed
as opposed to which champion is selected (ADR-0014)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import releases  # noqa: E402

SERVED = (
    "pu_location_id,do_location_id,departure_time,model_min\n"
    "132,161,2025-01-06 00:30:00,31.18\n"
)


def _manifest(rid: str = "a" * 64, version: str = "v3") -> dict[str, Any]:
    return {"release_id": rid, "components": {"model": {"version": version}}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket: str, Key: str, Body: bytes, **_: Any) -> None:  # noqa: N803
        self.objects[Key] = Body

    def get_paginator(self, _: str) -> Any:
        fake = self

        class P:
            def paginate(self, Bucket: str, Prefix: str) -> Any:  # noqa: N803
                yield {
                    "Contents": [
                        {"Key": k} for k in fake.objects if k.startswith(Prefix)
                    ]
                }

        return P()


CFG = {
    "MemorySize": 3008,
    "Timeout": 60,
    "Environment": {"Variables": {"LOG_LEVEL": "INFO", "OMP_NUM_THREADS": "2"}},
    "Architectures": ["x86_64"],
    "EphemeralStorage": {"Size": 512},
    "Role": "arn:aws:iam::1:role/exec",
}


def _put(
    s3: FakeS3,
    tmp: Path,
    rid: str,
    version: str,
    image: str,
    lv: str = "",
    *,
    served: str = SERVED,
    cfg: dict[str, Any] | None = None,
    capsys: pytest.CaptureFixture[str] | None = None,
) -> str:
    m, e, c = tmp / f"m-{rid[:4]}.json", tmp / "served.csv", tmp / "cfg.json"
    m.write_text(json.dumps(_manifest(rid, version)))
    e.write_text(served)
    c.write_text(json.dumps(cfg or CFG))
    args = ["put", "--bucket", "b", "--manifest", str(m), "--image", image]
    args += ["--model", version, "--evidence", str(e), "--lambda-version", lv]
    args += ["--lambda-config", str(c)]
    assert releases.main(args, s3=s3) == 0
    return releases.ref_of(
        releases.make_record(
            manifest=_manifest(rid, version),
            model_version=version,
            image_uri=image,
            lambda_version=lv or None,
            lambda_config=releases.config_snapshot(cfg or CFG),
            served_csv=served,
        )
    )


def _ev(s3: FakeS3, action: str, ref: str) -> int:
    return releases.main([action, "--bucket", "b", "--ref", ref], s3=s3)


def _find(s3: FakeS3, model: str, capsys: pytest.CaptureFixture[str]) -> str | None:
    capsys.readouterr()
    rc = releases.main(["find", "--bucket", "b", "--model", model], s3=s3)
    return capsys.readouterr().out.strip() if rc == 0 else None


def test_each_image_version_and_config_is_its_own_immutable_deployment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review 2026-09-29: fresh_build=true gave the same release another
    image and overwrote the top-level image and prediction CSV."""
    s3 = FakeS3()
    rid = "a" * 64
    first = _put(s3, tmp_path, rid, "v3", "r@sha256:" + "1" * 64, "7")
    rebuilt = _put(
        s3, tmp_path, rid, "v3", "r@sha256:" + "2" * 64, "9", served=SERVED + "x\n"
    )
    other_cfg = _put(
        s3, tmp_path, rid, "v3", "r@sha256:" + "1" * 64, "", cfg={**CFG, "Timeout": 90}
    )
    assert len({first, rebuilt, other_cfg}) == 3
    assert all(ref.startswith(rid + "/") for ref in (first, rebuilt, other_cfg))
    out = tmp_path / "restore"
    capsys.readouterr()
    assert (
        releases.main(
            ["get", "--bucket", "b", "--ref", first, "--out", str(out)], s3=s3
        )
        == 0
    )
    assert (out / "served_predictions.csv").read_text() == SERVED  # not overwritten
    assert json.loads((out / "lambda_config.json").read_text())["Timeout"] == 60


def test_recorded_evidence_cannot_be_replaced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s3 = FakeS3()
    ref = _put(s3, tmp_path, "a" * 64, "v3", "r@sha256:" + "1" * 64, "7")
    again = _put(s3, tmp_path, "a" * 64, "v3", "r@sha256:" + "1" * 64, "7")
    assert again == ref  # re-verification with identical evidence: one more event
    rec = json.loads(s3.objects[releases._key(ref)])
    assert [e["event"] for e in rec["events"]] == ["verified", "verified"]

    m, e, c = tmp_path / "m.json", tmp_path / "e.csv", tmp_path / "c.json"
    m.write_text(json.dumps(_manifest("a" * 64, "v3")))
    e.write_text(SERVED.replace("31.18", "31.19"))
    c.write_text(json.dumps(CFG))
    args = [
        "put",
        "--bucket",
        "b",
        "--manifest",
        str(m),
        "--image",
        "r@sha256:" + "1" * 64,
    ]
    args += ["--model", "v3", "--evidence", str(e), "--lambda-version", "7"]
    assert releases.main([*args, "--lambda-config", str(c)], s3=s3) == 1
    assert "immutable" in capsys.readouterr().err
    assert (
        json.loads(s3.objects[releases._key(ref)])["served_predictions_csv"] == SERVED
    )


def test_a_record_must_match_its_manifest() -> None:
    with pytest.raises(ValueError, match="manifest is for v3"):
        releases.make_record(
            manifest=_manifest(),
            model_version="v1",
            image_uri="i@sha256:1",
            lambda_version=None,
            lambda_config={},
            served_csv="",
        )


def test_a_rollback_selects_the_last_successfully_activated_deployment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review 2026-09-29: a candidate recorded before the alias move/URL check
    stayed selectable after its activation failed."""
    s3 = FakeS3()
    good = _put(s3, tmp_path, "1" * 64, "v1", "r@sha256:" + "a" * 64, "4")
    assert _find(s3, "v1", capsys) is None  # verified is not enough
    assert _ev(s3, "activate", good) == 0
    assert _find(s3, "v1", capsys) == good

    # a later deploy of v1 is verified, then its URL check fails
    bad = _put(s3, tmp_path, "2" * 64, "v1", "r@sha256:" + "b" * 64, "5")
    assert _ev(s3, "fail", bad) == 0
    assert _find(s3, "v1", capsys) == good  # the failed candidate is never picked

    # a deployment that went live once but whose re-activation failed is not
    # the default either
    newer = _put(s3, tmp_path, "3" * 64, "v1", "r@sha256:" + "c" * 64, "6")
    assert _ev(s3, "activate", newer) == 0
    assert _find(s3, "v1", capsys) == newer
    assert _ev(s3, "fail", newer) == 0
    assert _find(s3, "v1", capsys) == good
    assert _find(s3, "v2", capsys) is None  # never deployed: no rebuild

    # a release id resolves to its activated deployment; an explicit ref to a
    # failed one restores it, with a warning
    out = tmp_path / "o"
    assert (
        releases.main(
            ["get", "--bucket", "b", "--ref", "2" * 64, "--out", str(out)], s3=s3
        )
        == 1
    )
    assert (
        releases.main(["get", "--bucket", "b", "--ref", bad, "--out", str(out)], s3=s3)
        == 0
    )
    assert "warning" in capsys.readouterr().err


def test_ledger_round_trip_and_live_pointer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s3 = FakeS3()
    v1 = _put(s3, tmp_path, "1" * 64, "v1", "r@sha256:" + "a" * 64, "4")
    v3 = _put(s3, tmp_path, "3" * 64, "v3", "r@sha256:" + "c" * 64, "6")
    assert _ev(s3, "activate", v1) == 0
    capsys.readouterr()

    out = tmp_path / "restore"
    assert (
        releases.main(
            ["get", "--bucket", "b", "--ref", "1" * 64, "--out", str(out)], s3=s3
        )
        == 0
    )
    env = dict(line.split("=", 1) for line in capsys.readouterr().out.split())
    assert env == {
        "RELEASE_ID": "1" * 64,
        "DEPLOYMENT_REF": v1,
        "MODEL_VERSION": "v1",
        "IMAGE_URI": "r@sha256:" + "a" * 64,
        "LAMBDA_VERSION": "4",
    }
    assert (out / "served_predictions.csv").read_text() == SERVED

    champ = tmp_path / "champion.json"
    champ.write_text(json.dumps({"version": 3, "action": "promote"}))
    status = ["status", "--bucket", "b", "--champion", str(champ), "--strict"]
    assert releases.main(status, s3=s3) == 1  # v1 deployed, v3 selected
    assert "MISMATCH" in capsys.readouterr().out

    assert _ev(s3, "activate", v3) == 0
    assert releases.main(status, s3=s3) == 0
    live = json.loads(s3.objects[releases.LIVE_KEY])
    assert live["deployment_id"] == v3.split("/")[1]
    # a promotion (registry) moves the selection before any deploy succeeds
    champ.write_text(json.dumps({"version": 4, "action": "promote"}))
    assert releases.main(status, s3=s3) == 1


def test_a_failed_activation_never_moves_the_live_pointer(tmp_path: Path) -> None:
    s3 = FakeS3()
    ref = _put(s3, tmp_path, "3" * 64, "v3", "r@sha256:" + "c" * 64)
    assert _ev(s3, "fail", ref) == 0
    assert releases.LIVE_KEY not in s3.objects


def test_events_refuse_an_unrecorded_deployment() -> None:
    s3 = FakeS3()
    assert _ev(s3, "activate", "9" * 64 + "/x-1-y") == 1
    assert releases.LIVE_KEY not in s3.objects


def test_config_snapshot_keeps_behaviour_and_drops_infrastructure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snap = releases.config_snapshot(CFG)
    assert "Role" not in snap
    assert snap["Environment"] == {"LOG_LEVEL": "INFO", "OMP_NUM_THREADS": "2"}
    rec, act = tmp_path / "rec.json", tmp_path / "act.json"
    rec.write_text(json.dumps(snap))
    act.write_text(json.dumps({**CFG, "Role": "arn:aws:iam::1:role/other"}))
    diff = ["config-diff", "--recorded", str(rec), "--actual", str(act)]
    assert releases.main(diff) == 0  # a role change is not a behaviour change
    env = {"Variables": {**CFG["Environment"]["Variables"], "MAX_BATCH": "10"}}
    act.write_text(json.dumps({**CFG, "Environment": env}))
    assert releases.main(diff) == 1
    assert "Environment" in capsys.readouterr().out


def test_live_id_distinguishes_not_seeded_from_unreadable(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    s3 = FakeS3()
    assert releases.main(["live-id", "--bucket", "b"], s3=s3) == 0
    assert capsys.readouterr().out.strip() == ""  # not seeded: nothing to expect
    ref = _put(s3, tmp_path, "3" * 64, "v3", "r@sha256:" + "c" * 64)
    _ev(s3, "activate", ref)
    capsys.readouterr()
    assert releases.main(["live-id", "--bucket", "b"], s3=s3) == 0
    assert capsys.readouterr().out.strip() == "3" * 64

    class Denied(FakeS3):
        def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    with pytest.raises(ClientError):  # an unreadable ledger is a failure
        releases.main(["live-id", "--bucket", "b"], s3=Denied())
