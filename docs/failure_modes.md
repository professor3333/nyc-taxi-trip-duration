# Failure catalogue

For each failure the system is designed to survive: behaviour, the log line, the test, the runbook entry.

| failure | observed behaviour | log line | test | runbook |
|---|---|---|---|---|
| malformed input (missing field, wrong type, zone 999/264/265/0, bad or out-of-window time, extra field, empty/non-JSON body) | 422 `{request_id, errors:[{field,message}]}` | WARNING `validation_error` with field list | `test_api.py::test_validation_errors_are_422_with_fields`, `test_non_json_and_empty_bodies`, `test_fuzz_never_500`; CI container smoke `deploy_check --malformed` | — |
| unknown zone reaching the predictor | cannot happen: schema bounds 1–263; `build_features` raises on unknown ids as a second guard | ERROR `internal_error` (would be a bug) | `test_features.py::test_unknown_zone_raises` | — |
| model file missing | fallback table serves; `/health` degraded with `load_error`; `/ready` 503 | ERROR `model_load_failed` + traceback at startup | `test_missing_model_degrades_to_fallback` | service degraded |
| corrupt model | same as missing | same | `test_corrupt_model_degrades_and_ready_503` | service degraded |
| feature-list mismatch (model trained with other features) | model refused; fallback serves; degraded | ERROR `model_load_failed` "feature list mismatch" | `test_feature_list_mismatch_refuses_model` | service degraded |
| **feature-list mismatch, observed live 2026-09-22** | `/health` → `{"status":"degraded","model_version":"fallback-v1","model_kind":"fallback","load_error":"ValueError: feature list mismatch: model has ['a','b'], code has [...12 features]"}`; `/ready` 503; `/predict` still 200 from the lookup table | as above, in CloudWatch | `test_promote_never_writes_repo_champion_files` (the regression that caused it) | service degraded |
| model and fallback both missing | **controlled 503**: `/health/live` 200, `/health/ready` 503, `/predict` 503 `{"error":"unavailable"}` with `retry-after: 30`; process stays up so `/version` and the logs remain available | ERROR `model_load_failed` and `fallback_load_failed`, then `unavailable` per request | `test_both_missing_serves_controlled_503` | deploy fails at `deploy_check`; roll back |
| **model loads but fails at prediction time** (raises, NaN/inf, wrong shape, non-numeric) | that request answered by the baseline (`model_kind: "fallback"`, `model_version: fallback-vN`); `/health` degraded with `predict_error`/`predict_failures` until the model answers again; `/health/ready` 503 meanwhile | ERROR `model_predict_failed` + traceback; request line `model_kind=fallback` (FallbackCount alarm) | `test_serving_failures.py::test_model_prediction_failure_answers_from_the_baseline` (5 cases), `test_degraded_clears_when_the_model_succeeds_again` | service degraded |
| model **and** baseline fail at prediction time, or the baseline returns non-finite values | 503 `unavailable`, `retry-after: 30`; process up | ERROR `fallback_predict_failed`, then `unavailable` | `test_model_and_baseline_both_failing_is_a_controlled_503`, `test_baseline_returning_nan_is_a_503_not_a_bad_answer` | roll back |
| `champion.json` corrupt or malformed | the loaded model serves, labelled `unregistered:<sha>`; `/health` degraded with `release_error`; `deploy_check --expect-version` fails | ERROR `release_metadata_invalid` | `test_corrupt_champion_json_keeps_serving_the_model` (5 cases) | rollback |
| reference data (centroids/holidays) missing or corrupt | **unavailable**: `/predict` 503, `/health/ready` 503, `/health/live` 200 | ERROR `reference_load_failed` | `test_missing_reference_data_is_unavailable_not_a_crash`, `test_corrupt_reference_data_is_unavailable`, `test_missing_holidays_is_unavailable` | rebuild the image |
| `params.yaml` / environment unusable (bad YAML, bad date, bad limits, bad `LOG_LEVEL`) | unavailable (503), not a crash | ERROR `config_invalid` | `test_corrupt_params_is_unavailable_not_a_crash` (3 cases) | fix config, redeploy |
| request body over `api.max_body_bytes` (declared, chunked, or with a false Content-Length) | 413 `{request_id, error:"payload_too_large"}` before JSON parsing | WARNING `payload_too_large` | `test_oversized_body_is_413_before_parsing`, `test_oversized_chunked_body_is_413`, `test_lying_content_length_does_not_bypass_the_limit` | — |
| batch longer than `max_batch` | 422 on `items` before any item is validated | WARNING `validation_error` | `test_batch_length_is_checked_before_items_are_validated` | — |
| slow or CPU-heavy prediction | runs off the event loop (limiter of `predict_workers`); probes and other requests keep answering | — | `test_slow_prediction_does_not_block_the_event_loop`; measured in ADR-0012 | — |
| non-champion model baked into the image | `model_version` = `unregistered:<sha>`; `deploy_check --expect-version` fails | request lines carry the version | `test_champion_version_reported_when_md5_matches`; observed 2026-09-22 with v2's pickle vs v1's champion.json | rollback |
| unexpected exception in a handler | 500 `{request_id, error:"internal"}` | ERROR `internal_error` with traceback | fuzz finds none; handler unit-tested by construction | Logs Insights by `request_id` |
| TLC month not published | ingest exits 0, nothing written | INFO `month 2026-08 not published yet (HTTP 403 at …)` | `test_403_means_not_published`, `test_403_is_not_retried`, `test_main_exits_zero_on_403`, and the 404 equivalents | none needed |
| TLC transient failure (5xx/network/short read) | retried 3× with backoff, then raises (exit 1) | WARNING per retry | `test_ingest_month_retries_transient_then_succeeds`, `..._gives_up_after_retries`, `test_download_rejects_incomplete_body` | re-run |
| TLC schema drift (new/renamed column) | ingest refuses the file, exit 1 | SchemaDriftError naming the column | `test_check_schema_rejects_unknown_column`, `test_ingest_month_refuses_file_with_schema_drift` | schema drift |
| TLC republishes a month | new bytes stored, `replaced_source_md5` recorded, one `.dvc` diff | WARNING `REPUBLISHED` | `test_ingest_month_detects_republish` | republished month |
| missing intermediate month | `prepare` refuses to skip it | ValueError naming the month | `test_month_sequence_is_contiguous_from_start` | ingest the gap |
| DVC remote unreachable at build | `fetch_champion.py` fails → `deploy.yml` fails before touching Lambda | dvc error | manual | check S3 / credentials |
| Lambda cold-start timeout | 30 s timeout → Function URL 5xx | Lambda REPORT line | `deploy_check --cold` measures | raise memory (ADR-0008) |
| ECR image missing | `update-function-code` fails; old version keeps serving | AWS error in the run | — | re-run deploy |
| DST fall-back hour in data | rows in the 3-hour window dropped in `validate` | rejection count `dst_transition_window` | `test_validate.py` DST cases | — |
| tz-aware request | converted to New York local | — | `test_tz_aware_departure_converted_to_new_york` | — |
| tz-aware request at the edge of `datetime`'s range | 422 with a field message; the conversion to New York would overflow | WARNING `validation_error` | `test_extreme_aware_timestamps_are_422_not_500` (6 cases), `test_extreme_aware_timestamp_in_batch_is_422`, fuzz over datetimes | — |

**Observed live on 2026-09-22** (account 560512681455): malformed input (all
nine cases → 422 with `request_id`, WARNING lines in CloudWatch), feature-list
mismatch → degraded fallback, Lambda cold-start timeout (at 1024 MB, before
ADR-0008's amendment), and ECR/Lambda deploy failures (OCI manifest rejected,
unreachable git revision, stale Function URL secret). Not yet observed: TLC
republishing a month, S3 unreachable at build time.
