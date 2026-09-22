# Model card — nyc-taxi-trip-duration champion

**Champion:** registry version 3, promoted 2026-09-22 (`docs/promotions.md`).

## Intended use

Estimated travel time in minutes for a yellow-taxi trip between two TLC zones
at a departure time, for journey planning and dispatch. A **historical**
estimator: no live traffic, weather or events. Not a fare estimate, not an ETA
guarantee. Zones 264 (Unknown) and 265 (Outside NYC) are refused.

## Data and training

- TLC yellow-taxi trip records, months 2024-10 … 2025-01, stored byte-for-byte
  (`data/raw/`), validity rules per ADR-0002 (≈ 96 % of rows kept; no filter
  uses a post-trip column).
- v3: train 2024-10 + 2024-11 (7.20M rows), validation 2024-12, test 2025-01
  (ADR-0003 rolling window, expanding until six months).
- Features (ADR-0005): zone-centroid coordinates and planar distance, PU/DO
  borough, hour, minute of day, weekday, weekend, US holiday. Nothing recorded
  during or after the trip.
- Model (ADR-0006): `HistGradientBoostingRegressor(loss=absolute_error,
  max_iter=300, learning_rate=0.1, max_leaf_nodes=63, min_samples_leaf=200,
  l2_regularization=1.0)`, seed 42, 4 threads. Fit 142 s.
- Fallback (ADR-0006): cascading zone-pair medians from the training months;
  serves when the model cannot load.

## Metrics (minutes)

| version | train months | test month | MAE model | MAE fallback | P90 AE model | bias |
|---|---|---|---|---|---|---|
| v1 | 2024-10 | 2024-12 | 4.689 | 5.111 | 10.53 | −1.82 |
| v2 | 2024-10 (300 iters) | 2024-12 | 4.661 | 5.111 | 10.48 | — |
| **v3** | 2024-10..11 | **2025-01** | **3.790** | 3.995 | 7.95 | — |
| v1 prospective | 2024-10 | 2025-01 | 3.841 | 4.086 | 8.05 | +1.47 |

MAPE is 31–35 % and dominated by short trips; MAE is the primary metric
(ADR-0001). Per-hour and per-borough-pair MAE tables: `reports/eval/`.

## Known failure modes and limits

- **Unseen zone pairs:** the model generalises through coordinates; the
  fallback drops to borough-pair or global medians (≈ 3 % of test requests).
- **Regime changes:** the Congestion Relief Zone (2025-01-05) made January
  trips faster than the December-trained model expected (bias +1.47 min).
  Prospective evaluation surfaces this monthly; the rolling window absorbs it.
- **Holidays and events:** only US federal holidays are features; parades,
  storms and strikes are not.
- **Long trips:** trained on ≤ 180 min; longer requests are extrapolations.
- **Sub-minute and intra-zone trips:** kept when ≥ 60 s; predictions for
  PU == DO reflect the median intra-zone trip, not a zero.
- **Time semantics:** naive input is New York local time (ADR-0009).

## A caveat on the reported numbers

The metrics above were computed on the machine that trained the model
(arm64). Serving runs on x86_64 Lambda, where the same model's predictions
differ by a mean of 0.043 min and at most 0.597 min — up to 2% — because a
feature value differing in its last bits can fall the other side of a tree
split. The measurement and its consequences are in
`docs/reproducibility.md`. Models promoted from a scheduled retrain PR are
trained on `ubuntu-latest` (x86_64) and therefore measured where they serve.

## Champion selection and rollback

ADR-0007: a challenger is promoted only if it beats the champion's MAE on the
same month (or the champion's prospective MAE on the challenger's test month)
and beats the fallback. `make promote` / `make rollback`; every change is a
row in `docs/promotions.md`; the deployed version is visible on `/health`.
