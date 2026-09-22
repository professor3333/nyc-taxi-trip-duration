# Promotions

One line per alias change, appended by `scripts/promote.py`.

| when (UTC) | action | from | to | test month | MAE test model | MAE test fallback | git sha | reason |
|---|---|---|---|---|---|---|---|---|
| 2026-09-22T05:54:08+00:00 | promote | - | 1 | 2024-12 | 4.6887 | 5.1113 | 49fb1c0d | first champion: beats fallback on 2024-12 (4.689 vs 5.111) |
| 2026-09-22T05:56:16+00:00 | promote | 1 | 2 | 2024-12 | 4.6615 | 5.1113 | 44c098eb | max_iter 300: test MAE 4.661 < champion 4.689 on 2024-12 |
| 2026-09-22T05:56:18+00:00 | rollback | 2 | 1 | 2024-12 | 4.6887 | 5.1113 | 49fb1c0d | exit criterion 2 rehearsal: prove rollback restores v1 |
| 2026-09-22T06:35:06+00:00 | promote | 1 | 3 | 2025-01 | 3.7898 | 3.9946 | f6b2f155 | retrain on 2024-10..11; test MAE 3.790 on 2025-01 < champion v1 prospective 3.841 |
| 2026-09-22T12:23:29+00:00 | rollback | 3 | 1 | 2024-12 | 4.6887 | 5.1113 | 49fb1c0d | exit criterion 2 live: prove rollback redeploys the previous champion |
| 2026-09-22T12:40:58+00:00 | promote | 1 | 3 | 2025-01 | 3.7898 | 3.9946 | f6b2f155 | re-promote v3 to capture reference_md5 and champion_meta.json |
| 2026-09-22T12:41:02+00:00 | rollback | 3 | 1 | 2024-12 | 4.6887 | 5.1113 | 49fb1c0d | exit criterion 2 live: rollback redeploys the previous champion |
| 2026-09-22T12:41:26+00:00 | promote | 1 | 3 | 2025-01 | 3.7898 | 3.9946 | f6b2f155 | capture reference_md5 |
| 2026-09-22T12:41:28+00:00 | rollback | 3 | 1 | 2024-12 | 4.6887 | 5.1113 | 49fb1c0d | exit criterion 2 live: rollback redeploys the previous champion |
| 2026-09-22T13:30:25+00:00 | promote | 1 | 3 | 2025-01 | 3.7898 | 3.9946 | f6b2f155 | restore v3 after the live rollback proof; test MAE 3.790 on 2025-01 |
| 2026-09-22T16:10:19+00:00 | rollback | 3 | 1 | 2024-12 | 4.6887 | 5.1113 | 2024-10 | f43aca65 | 49fb1c0d | milestone demo: select the previous version and verify its predictions are restored |
