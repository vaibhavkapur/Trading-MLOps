# Validation and Acceptance Evidence Summary

| Milestone / Scenario | Status | Key Evidence |
|---|---|---|
| M1 Foundation & Registry | PASSED | 3 datasets, 2 feature sets, `order_execution_enabled=false` |
| M2 Training & Signed Artifacts | PASSED | Byte-identical reproducibility=`True`, v1 net Sharpe=`2.050025` (n=120) |
| M3 Staged Promotion & RBAC | PASSED | SHADOW->PAPER->LIVE_GATED (dual sign-off), live execution blocked=`EXECUTION_DISABLED_GUARD` |
| M4 Drift & Recovery Drill | PASSED | Drift severity=`CRITICAL` (4 alerts), rollback restored `v1.0.0`, audit chain verified |
| Scenario: `data_leakage` | PASSED | `{"error_code": "DATA_LEAKAGE_DETECTED", "failed_experiment_recorded": true, "message": "Point-in-time leakage at record 25: availability_ts_utc precedes event_ts_utc.", "passed": true, "recorded_status": "REJECTED_LEAKAGE"}` |
| Scenario: `artifact_tampering` | PASSED | `{"error_code": "ARTIFACT_TAMPERING_DETECTED", "message": "SHA-256 mismatch on weights.json for artifact 'art_alpha_ridge_eq_1_0_0_863c0f8c77'.", "passed": true}` |
| Scenario: `failed_validation` | PASSED | `{"failed_checks": ["min_net_sharpe", "min_mean_net_return_bps", "min_hit_rate", "min_information_coefficient", "validation_split_positive_signal"], "passed": true, "promotion_error_code": "VALIDATION_GATE_FAILED", "validation_passed": false}` |
| Scenario: `unauthorized_promotion` | PASSED | `{"passed": true, "self_approval_error_code": "SELF_APPROVAL_FORBIDDEN", "wrong_role_error_code": "UNAUTHORIZED_PROMOTION"}` |
| Scenario: `incompatible_feature_version` | PASSED | `{"error_code": "INCOMPATIBLE_FEATURE_VERSION", "message": "Breaking MAJOR feature set mismatch: model v2.0.0 vs serving v1.0.0.", "passed": true}` |
| Scenario: `rollback` | PASSED | `{"from_artifact_id": "art_alpha_ridge_eq_2_0_0_96ce83a46f", "passed": true, "restored_artifact_id": "art_alpha_ridge_eq_1_0_0_863c0f8c77", "restored_model_version": "1.0.0", "rollback_id": "rbk_d50d101ae9252580"}` |
| Scenario: `repeated_job` | PASSED | `{"conflicting_spec_error_code": "IDEMPOTENCY_CONFLICT", "idempotent_replay_detected": true, "passed": true, "same_bundle_sha256": true, "same_run_id": true}` |
