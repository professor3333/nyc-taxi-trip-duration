"""The release ledger picks the release a rollback restores."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from releases import latest_for_model  # noqa: E402


def test_latest_verified_release_of_the_model_wins() -> None:
    records = [
        {
            "release_id": "a",
            "model_version": "v3",
            "verified_at": "2026-09-22T10:00:00Z",
        },
        {
            "release_id": "b",
            "model_version": "v3",
            "verified_at": "2026-09-23T10:00:00Z",
        },
        {
            "release_id": "c",
            "model_version": "v5",
            "verified_at": "2026-09-24T10:00:00Z",
        },
        {"release_id": "d", "model_version": "v3", "verified_at": ""},  # never verified
    ]
    assert latest_for_model(records, "v3")["release_id"] == "b"
    assert latest_for_model(records, "v5")["release_id"] == "c"


def test_no_verified_release_means_no_silent_rebuild() -> None:
    assert latest_for_model([], "v3") is None
    assert latest_for_model([{"model_version": "v3", "verified_at": ""}], "v3") is None
