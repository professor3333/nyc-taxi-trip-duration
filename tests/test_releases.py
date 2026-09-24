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


def _put(
    s3: FakeS3, tmp: Path, rid: str, version: str, image: str, lv: str = ""
) -> None:
    m, e = tmp / f"m-{rid[:4]}.json", tmp / "served.csv"
    m.write_text(json.dumps(_manifest(rid, version)))
    e.write_text(SERVED)
    args = ["put", "--bucket", "b", "--manifest", str(m), "--image", image]
    args += ["--model", version, "--evidence", str(e), "--lambda-version", lv]
    assert releases.main(args, s3=s3) == 0


def test_a_rebuild_of_the_same_content_keeps_its_history() -> None:
    first = releases.make_record(
        None,
        manifest=_manifest(),
        model_version="v3",
        image_uri="r@sha256:1",
        lambda_version="7",
        served_csv=SERVED,
        run_url="u1",
        verified_at="2026-09-24T01:00:00+00:00",
    )
    again = releases.make_record(
        first,
        manifest=_manifest(),
        model_version="v3",
        image_uri="r@sha256:2",
        lambda_version="9",
        served_csv=SERVED,
        run_url="u2",
        verified_at="2026-09-24T02:00:00+00:00",
    )
    assert again["image_uri"] == "r@sha256:2" and again["lambda_version"] == "9"
    assert [h["image_uri"] for h in again["history"]] == ["r@sha256:1", "r@sha256:2"]


def test_a_record_must_match_its_manifest() -> None:
    with pytest.raises(ValueError, match="manifest is for v3"):
        releases.make_record(
            None,
            manifest=_manifest(),
            model_version="v1",
            image_uri="i",
            lambda_version=None,
            served_csv="",
            run_url="",
            verified_at="t",
        )


def test_find_restores_the_latest_verified_release_of_that_model(
    tmp_path: Path,
) -> None:
    def rec(rid: str, model: str, at: str) -> dict[str, str]:
        return {
            "release_id": rid,
            "model_version": model,
            "image_uri": f"img-{rid}",
            "verified_at": at,
        }

    recs = [
        rec("old", "v1", "2026-09-01"),
        rec("new", "v1", "2026-09-20"),
        rec("v3", "v3", "2026-09-23"),
        rec("unverified", "v1", ""),
    ]
    assert releases.latest_for_model(recs, "v1")["release_id"] == "new"
    assert releases.latest_for_model(recs, "v2") is None  # never deployed: no rebuild


def test_ledger_round_trip_and_live_pointer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s3 = FakeS3()
    _put(s3, tmp_path, "1" * 64, "v1", "r@sha256:aa", "4")
    _put(s3, tmp_path, "3" * 64, "v3", "r@sha256:cc", "6")
    capsys.readouterr()

    assert releases.main(["find", "--bucket", "b", "--model", "v1"], s3=s3) == 0
    assert capsys.readouterr().out.strip() == "1" * 64
    assert releases.main(["find", "--bucket", "b", "--model", "v2"], s3=s3) == 1

    out = tmp_path / "restore"
    assert (
        releases.main(
            ["get", "--bucket", "b", "--release-id", "1" * 64, "--out", str(out)], s3=s3
        )
        == 0
    )
    env = dict(line.split("=", 1) for line in capsys.readouterr().out.split())
    assert env == {
        "RELEASE_ID": "1" * 64,
        "MODEL_VERSION": "v1",
        "IMAGE_URI": "r@sha256:aa",
        "LAMBDA_VERSION": "4",
    }
    assert (out / "served_predictions.csv").read_text() == SERVED

    # nothing is deployed until mark-live: selected and deployed differ
    champ = tmp_path / "champion.json"
    champ.write_text(json.dumps({"version": 3, "action": "promote"}))
    status = ["status", "--bucket", "b", "--champion", str(champ), "--strict"]
    assert releases.main(status, s3=s3) == 1
    assert "no recorded release" in capsys.readouterr().out

    assert (
        releases.main(["mark-live", "--bucket", "b", "--release-id", "3" * 64], s3=s3)
        == 0
    )
    assert releases.main(status, s3=s3) == 0
    # a promotion (registry) moves the selection before any deploy succeeds
    champ.write_text(json.dumps({"version": 4, "action": "promote"}))
    assert releases.main(status, s3=s3) == 1
    assert "MISMATCH" in capsys.readouterr().out


def test_mark_live_refuses_an_unrecorded_release() -> None:
    s3 = FakeS3()
    assert (
        releases.main(["mark-live", "--bucket", "b", "--release-id", "9" * 64], s3=s3)
        == 1
    )
    assert releases.LIVE_KEY not in s3.objects


def test_live_id_distinguishes_not_seeded_from_unreadable(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    s3 = FakeS3()
    assert releases.main(["live-id", "--bucket", "b"], s3=s3) == 0
    assert capsys.readouterr().out.strip() == ""  # not seeded: nothing to expect
    _put(s3, tmp_path, "3" * 64, "v3", "r@sha256:cc")
    releases.main(["mark-live", "--bucket", "b", "--release-id", "3" * 64], s3=s3)
    capsys.readouterr()
    assert releases.main(["live-id", "--bucket", "b"], s3=s3) == 0
    assert capsys.readouterr().out.strip() == "3" * 64

    class Denied(FakeS3):
        def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    with pytest.raises(ClientError):  # an unreadable ledger is a failure
        releases.main(["live-id", "--bucket", "b"], s3=Denied())
