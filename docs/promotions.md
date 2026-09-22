# Promotions

One line per alias change, appended by `scripts/promote.py`.

| when (UTC) | action | from | to | test month | MAE test model | MAE test fallback | git sha | reason |
|---|---|---|---|---|---|---|---|---|
| 2026-09-22T05:54:08+00:00 | promote | - | 1 | 2024-12 | 4.6887 | 5.1113 | 49fb1c0d | first champion: beats fallback on 2024-12 (4.689 vs 5.111) |
| 2026-09-22T05:56:16+00:00 | promote | 1 | 2 | 2024-12 | 4.6615 | 5.1113 | 44c098eb | max_iter 300: test MAE 4.661 < champion 4.689 on 2024-12 |
| 2026-09-22T05:56:18+00:00 | rollback | 2 | 1 | 2024-12 | 4.6887 | 5.1113 | 49fb1c0d | exit criterion 2 rehearsal: prove rollback restores v1 |
