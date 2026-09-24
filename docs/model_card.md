# Model card — nyc-taxi-trip-duration champion

**Champion:** registry version 1, restored 2026-09-23 by the runbook's
rollback verification (#72); v3 was champion 2026-09-22 → 2026-09-23. See
`docs/promotions.md`; `/health` reports the serving version.

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

## Deployed accuracy (live, real trips)

The numbers above are offline. `make live-accuracy` checks that the deployed
service achieves them. It draws a seeded sample of 5,000 real trips from the
champion's test month, sends them through the live `/predict/batch`, and
compares the answers with the champion's offline predictions, computed on
x86_64 in the canonical training image, and with the recorded MAE
(`scripts/live_accuracy.py`).

| checked | version | month | path | parity | live MAE [95% CI] | recorded MAE |
|---|---|---|---|---|---|---|
| 2026-09-24 | v1 | 2024-12 | Function URL (SigV4) | 5,000 / 5,000 identical | 4.702 [4.541, 4.863] | 4.689 |

Evidence: `reports/live_accuracy/2024-12-v1.json` (and the same result
through the Lambda invoke path). This shows the deployment reproduces the
offline model on real inputs. It does not show accuracy on today's traffic:
the newest month any number here was measured on is TLC's newest published
month, not September 2026.

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

Champion v3 was trained and measured on arm64, and it serves on x86_64
Lambda. One feature, `centroid_dist_km` (`np.hypot`), differs in its last bit
between the two libms. On the 80-row fixture grid that moves 10 predictions,
by at most 0.597 min; the model evaluates identically on identical features.
Models trained from now on are trained and evaluated in the canonical
environment, which shares the serving image's pinned base
(`docs/reproducibility.md`), so their reported numbers are computed with the
features they serve with.

## Champion selection and rollback

ADR-0007: on the same month (the challenger's test month, where the champion's
number is its prospective evaluation), a challenger must beat the fallback and
be at least 1% better than the champion, with a 95% day-block bootstrap
interval above zero, and no gated slice (period, airport, borough pair, busy
route) may be more than 3% worse (2026-09-24 amendment). `make promote` / `make rollback`; every change is a
row in `docs/promotions.md`; the deployed version is visible on `/health`.
