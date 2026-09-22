"""Zone-pair median lookup table: the baseline and the production fallback (G5).

Built from **training months only** (G2). Prediction cascades through levels
until a cell with enough support is found:

    1. (pu, do, hour_bucket)        median, n >= min_count
    2. (pu, do)                     median, n >= min_count
    3. (pu_borough, do_borough, hour_bucket)
    4. global median

The table is one tidy parquet (``level``, keys, ``median``, ``n``) so it is a
shipped, versioned, testable artefact rather than a notebook cell. Prediction
needs only the three request fields plus the zone -> borough map.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from tripduration.features import DEPARTURE, DO, PU, TARGET, ReferenceData

LEVELS: tuple[str, ...] = ("pair_hour", "pair", "borough_hour", "global")
KEYS: dict[str, tuple[str, ...]] = {
    "pair_hour": (PU, DO, "hour_bucket"),
    "pair": (PU, DO),
    "borough_hour": ("pu_borough", "do_borough", "hour_bucket"),
    "global": (),
}
COLUMNS = ["level", PU, DO, "pu_borough", "do_borough", "hour_bucket", "median", "n"]


def hour_bucket(departure: pd.Series, edges: tuple[int, ...]) -> pd.Series:
    hours = pd.DatetimeIndex(departure).hour
    return pd.Series(
        np.searchsorted(np.asarray(edges[1:]), hours, side="right").astype(np.int8),
        index=departure.index,
        name="hour_bucket",
    )


def _with_keys(
    frame: pd.DataFrame, ref: ReferenceData, edges: tuple[int, ...]
) -> pd.DataFrame:
    boroughs = ref.centroids["borough"]
    return pd.DataFrame(
        {
            PU: frame[PU].to_numpy(),
            DO: frame[DO].to_numpy(),
            "pu_borough": boroughs.reindex(frame[PU].to_numpy()).to_numpy(),
            "do_borough": boroughs.reindex(frame[DO].to_numpy()).to_numpy(),
            "hour_bucket": hour_bucket(frame[DEPARTURE], edges).to_numpy(),
        },
        index=frame.index,
    )


def table_version(table: pd.DataFrame) -> str:
    """A content-derived version for the baseline: same medians, same version.

    The fallback ships in every image and can serve on its own, so it needs an
    identity independent of whichever model it accompanies.
    """
    payload = table.sort_values(COLUMNS[:-2], na_position="first").to_csv(index=False)
    return "fb-" + hashlib.sha256(payload.encode()).hexdigest()[:12]


@dataclass(frozen=True)
class FallbackTable:
    table: pd.DataFrame  # COLUMNS
    edges: tuple[int, ...]
    min_count: int

    @property
    def version(self) -> str:
        return table_version(self.table)

    @classmethod
    def fit(
        cls,
        train: pd.DataFrame,
        ref: ReferenceData,
        edges: tuple[int, ...],
        min_count: int,
    ) -> FallbackTable:
        keyed = _with_keys(train, ref, edges)
        keyed[TARGET] = train[TARGET].to_numpy()
        parts: list[pd.DataFrame] = []
        for level in LEVELS:
            keys = list(KEYS[level])
            if keys:
                grouped = keyed.groupby(keys, observed=True)[TARGET]
                g = pd.DataFrame(grouped.agg(median="median", n="size")).reset_index()
                g = g[g["n"] >= min_count]
            else:
                g = pd.DataFrame(
                    {"median": [keyed[TARGET].median()], "n": [len(keyed)]}
                )
            parts.append(g.assign(level=level))
        table = pd.concat(parts, ignore_index=True).reindex(columns=COLUMNS)
        return cls(table=table, edges=edges, min_count=min_count)

    def predict(
        self, frame: pd.DataFrame, ref: ReferenceData
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return (duration_min, level_used) for each row; never NaN."""
        keyed = _with_keys(frame, ref, self.edges)
        pred = np.full(len(keyed), np.nan)
        used = np.full(len(keyed), "", dtype=object)
        for level in LEVELS:
            todo = np.isnan(pred)
            if not todo.any():
                break
            keys = list(KEYS[level])
            sub = self.table[self.table["level"] == level]
            if keys:
                merged = keyed.loc[todo, keys].merge(
                    sub[keys + ["median"]], on=keys, how="left"
                )
                hit = merged["median"].to_numpy()
            else:
                hit = np.full(int(todo.sum()), float(sub["median"].iloc[0]))
            idx = np.flatnonzero(todo)
            found = ~np.isnan(hit)
            pred[idx[found]] = hit[found]
            used[idx[found]] = level
        assert not np.isnan(pred).any(), "global level must always resolve"
        return pred, used.astype(str)

    def save(self, path: Path) -> None:
        meta = pd.DataFrame(
            {
                "level": ["_meta"],
                "median": [float(self.min_count)],
                "n": [len(self.edges)],
            }
        ).reindex(columns=COLUMNS)
        edges_row = pd.DataFrame(
            {"level": ["_edges"] * len(self.edges), "hour_bucket": list(self.edges)}
        ).reindex(columns=COLUMNS)
        pd.concat([self.table, meta, edges_row], ignore_index=True).to_parquet(
            path, index=False
        )

    @classmethod
    def load(cls, path: Path) -> FallbackTable:
        df = pd.read_parquet(path)
        meta = df[df["level"] == "_meta"].iloc[0]
        edges = tuple(int(e) for e in df.loc[df["level"] == "_edges", "hour_bucket"])
        table = df[~df["level"].isin(["_meta", "_edges"])].reset_index(drop=True)
        return cls(table=table, edges=edges, min_count=int(meta["median"]))

    def level_counts(self) -> dict[str, int]:
        return {lvl: int((self.table["level"] == lvl).sum()) for lvl in LEVELS}
