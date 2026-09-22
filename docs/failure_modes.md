# Failure catalogue

For each failure the system is designed to survive: behaviour, the log line, the test, the runbook entry.

| failure | observed behaviour | log line | test | runbook |
|---|---|---|---|---|
| malformed input (missing field, wrong type, zone 999/264/265/0, bad or out-of-window time, extra field, empty/non-JSON body) | 422 `{request_id, errors:[{field,message}]}` | WARNING `validation_error` with field list | `test_api.py::test_validation_errors_are_422_with_fields`, `test_non_json_and_empty_bodies`, `test_fuzz_never_500`; CI container smoke `deploy_check --malformed` | — |
| unknown zone reaching the predictor | cannot happen: schema bounds 1–263; `build_features` raises on unknown ids as a second guard | ERROR `internal_error` (would be a bug) | `test_features.py::test_unknown_zone_raises` | — |
| model file missing | fallback table serves; `/health` degraded with `load_error`; `/ready` 503 | ERROR `model_load_failed` + traceback at startup | `test_missing_model_degrades_to_fallback` | service degraded |
| corrupt model | same as missing | same | `test_corrupt_model_degrades_and_ready_503` | service degraded |
| feature-list mismatch (model trained with other features) | model refused; fallback serves; degraded | ERROR `model_load_failed` "feature list mismatch" | `test_feature_list_mismatch_refuses_model` | service degraded |
| model and fallback both missing | process exits non-zero at startup (broken build) | ERROR + traceback | `test_both_missing_is_a_broken_build` | deploy fails at `deploy_check`; roll back |
| non-champion model baked into the image | `model_version` = `unregistered:<sha>`; `deploy_check --expect-version` fails | request lines carry the version | `test_champion_version_reported_when_md5_matches`; observed 2026-09-22 with v2's pickle vs v1's champion.json | rollback |
| unexpected exception in a handler | 500 `{request_id, error:"internal"}` | ERROR `internal_error` with traceback | fuzz finds none; handler unit-tested by construction | Logs Insights by `request_id` |
| TLC month not published | ingest exits 0, nothing written | INFO "not published yet (404 …)" | `test_ingest_month_404_*`, `test_main_exits_zero_on_404` | none needed |
| TLC transient failure (5xx/network/short read) | retried 3× with backoff, then raises (exit 1) | WARNING per retry | `test_ingest_month_retries_transient_then_succeeds`, `..._gives_up_after_retries`, `test_download_rejects_incomplete_body` | re-run |
| TLC schema drift (new/renamed column) | ingest refuses the file, exit 1 | SchemaDriftError naming the column | `test_check_schema_rejects_unknown_column`, `test_ingest_month_refuses_file_with_schema_drift` | schema drift |
| TLC republishes a month | new bytes stored, `replaced_source_md5` recorded, one `.dvc` diff | WARNING `REPUBLISHED` | `test_ingest_month_detects_republish` | republished month |
| missing intermediate month | `prepare` refuses to skip it | ValueError naming the month | `test_month_sequence_is_contiguous_from_start` | ingest the gap |
| DVC remote unreachable at build | `fetch_champion.py` fails → `deploy.yml` fails before touching Lambda | dvc error | manual | check S3 / credentials |
| Lambda cold-start timeout | 30 s timeout → Function URL 5xx | Lambda REPORT line | `deploy_check --cold` measures | raise memory (ADR-0008) |
| ECR image missing | `update-function-code` fails; old version keeps serving | AWS error in the run | — | re-run deploy |
| DST fall-back hour in data | rows in the 3-hour window dropped in `validate` | rejection count `dst_transition_window` | `test_validate.py` DST cases | — |
| tz-aware request | converted to New York local | — | `test_tz_aware_departure_converted_to_new_york` | — |

**Not yet observed live:** everything in the Lambda/CloudWatch rows — the AWS half is scripted and unrun as of 2026-09-22.
