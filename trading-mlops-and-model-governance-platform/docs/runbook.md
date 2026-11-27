# Operator Runbook

## 1. Local Setup & Verification

```bash
bash scripts/setup.sh
python3 scripts/validate.py
```

`scripts/validate.py` verifies synthetic fixture checksums, runs all unit/integration/scenario tests (`tests/`), executes `scripts/demo.py`, and regenerates all milestone and failure scenario evidence files in `reports/`.

## 2. Standard Model Lifecycle Workflow

1. **Register Dataset & Feature Contract (M1)**: `RESEARCHER` registers immutable `DatasetSnapshot` (with SHA-256, UTC bounds, entitlement, license) and `FeatureSetContract` (SemVer). `RELEASE_OPERATOR` binds the serving `FeatureSetContract` to each target environment (`shadow`, `paper`, `live_gated`).
2. **Submit Reproducible Training Job (M2)**: `RESEARCHER` submits `TrainingJobSpec` with unique `idempotency_key`, pinned `code_commit`, fixed `seed`, and `embargo_seconds >= 3600`. Both succeeded and rejected/failed runs are recorded.
3. **Run Multi-Metric Validation Gate (M3)**: Independent `VALIDATOR` runs `validate_artifact()`. Passing generates an immutable `ValidationReport` and JSON/Markdown `ModelCard` and moves stage to `VALIDATED`. Failing moves stage to `REJECTED` and blocks approvals/promotions.
4. **Independent Approval & Staged Promotion (M3)**:
   - `RISK_APPROVER` (distinct from creator) approves `SHADOW` -> `RELEASE_OPERATOR` promotes to `shadow` (`order_execution_enabled=false`).
   - Monitor shadow divergence against champion via `monitor_deployment(..., champion_artifact_id=...)`.
   - `RISK_APPROVER` approves `PAPER` -> `RELEASE_OPERATOR` promotes to `paper` (`order_execution_enabled=false` by default).
   - `LIVE_GATED` requires dual independent approvals (`RISK_APPROVER` + `COMPLIANCE_OFFICER`) and still blocks real-capital execution in MVP.

## 3. Drift Alert Response & Safe Fallback (M4)

| Drift Severity | Trigger | Mandatory Operator Response |
|---|---|---|
| `INFO` | PSI < 0.15 and \|z-shift\| < 1.5 | Log telemetry; continue normal monitoring. |
| `WARNING` | PSI >= 0.15 or \|z-shift\| >= 1.5 or shadow disagreement >= 20% | Pause pending promotions, keep `order_execution_enabled=false`, inspect upstream data lake/feature store (#17/#18) for schema or feed anomalies. |
| `CRITICAL` | PSI >= 0.25 or \|z-shift\| >= 2.5 or shadow disagreement >= 50% | Freeze promotions immediately, confirm execution is disabled, execute rollback to last verified known-good version, open incident review. **Do NOT trigger blind retraining on recent returns.** |

## 4. Emergency Rollback Drill Procedure

1. Identify affected environment and model (e.g., `paper`, `alpha_ridge_eq`).
2. Execute `platform.rollback_deployment(environment, model_name, operator, reason)` as `RELEASE_OPERATOR` or `RISK_APPROVER`.
3. Confirm rollback succeeded:
   - Platform automatically re-verifies SHA-256 checksum, HMAC-SHA256 signature, and serving feature compatibility of the target prior version before switching.
   - Restored deployment forces `order_execution_enabled=false`.
   - Verify audit chain integrity:
     ```bash
     PYTHONPATH=src python3 -m trading_mlops.cli --state-dir .tmp_run/reproduce_state verify-audit
     PYTHONPATH=src python3 -m trading_mlops.cli --state-dir .tmp_run/reproduce_state governance-review
     ```

## 5. Tampering or Audit Chain Failure Response

- If `ARTIFACT_TAMPERING_DETECTED` or `AUDIT_CHAIN_TAMPERED` is raised:
  1. Treat the affected environment as compromised; halt all promotions immediately.
  2. Preserve raw artifact directories, SQLite database, and `audit_log.jsonl` for forensic inspection.
  3. Rotate `TRADING_MLOPS_SIGNING_KEY` via secret manager and re-verify all active deployments from immutable backups.
