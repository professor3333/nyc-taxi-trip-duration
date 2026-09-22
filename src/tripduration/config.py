"""Load params.yaml into typed, frozen objects. One reader for every stage."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class DataParams:
    service: str
    raw_dir: Path
    validated_dir: Path
    reference_dir: Path
    timezone: str


@dataclass(frozen=True)
class SplitParams:
    start_month: str
    train_window_max: int
    train_sample_frac: float


@dataclass(frozen=True)
class ValidityParams:
    zone_min: int
    zone_max: int
    min_duration_s: int
    max_duration_min: int
    dst_window_hours: int
    dedupe_key: tuple[str, ...]


@dataclass(frozen=True)
class Params:
    seed: int
    n_threads: int
    data: DataParams
    split: SplitParams
    validity: ValidityParams
    raw: dict[str, Any]


def load_params(path: Path = Path("params.yaml")) -> Params:
    with path.open() as fh:
        raw = yaml.safe_load(fh)
    d, s, v = raw["data"], raw["split"], raw["validity"]
    return Params(
        seed=int(raw["seed"]),
        n_threads=int(raw["n_threads"]),
        data=DataParams(
            service=d["service"],
            raw_dir=Path(d["raw_dir"]),
            validated_dir=Path(d["validated_dir"]),
            reference_dir=Path(d["reference_dir"]),
            timezone=d["timezone"],
        ),
        split=SplitParams(
            start_month=str(s["start_month"]),
            train_window_max=int(s["train_window_max"]),
            train_sample_frac=float(s["train_sample_frac"]),
        ),
        validity=ValidityParams(
            zone_min=int(v["zone_min"]),
            zone_max=int(v["zone_max"]),
            min_duration_s=int(v["min_duration_s"]),
            max_duration_min=int(v["max_duration_min"]),
            dst_window_hours=int(v["dst_window_hours"]),
            dedupe_key=tuple(v["dedupe_key"]),
        ),
        raw=raw,
    )
