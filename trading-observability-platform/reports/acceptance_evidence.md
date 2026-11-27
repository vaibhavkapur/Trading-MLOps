# Acceptance Criteria & Milestone Evidence — Trading Observability Platform

Generated from verified execution of `bash scripts/reproduce.sh` (fixed seed `20261127`, Python `3.9.6`, `macOS-26.5.1-arm64-arm-64bit`).

## Section 6 Acceptance Criteria Checklist

| Criterion (`plan.md` Section 6) | Status | Verification & Evidence Artifact |
|---|---|---|
| An order can be traced across services without exposing secrets | **PASS** | All 6 services (`market_data`, `research`, `risk`, `oms`, `broker`, `portfolio`) correlated by W3C `trace_id` & `order_id` (`reports/demo_sample_trace_otlp.json`, `test_end_to_end_pipeline_and_idempotency`); `sensitive_field_leak` scenario verified 0 leaks across logs/traces and HTTP `403` for unauthorized auditor access (`test_http_endpoints_and_role_access_control`). |
| Alerts identify injected failures | **PASS** | `8/8` fault scenarios passed in `reports/scenario_validation_report.md`: `STALE_MARKET_DATA`, `OMS_QUEUE_LAG`, `BROKER_LATENCY_DEGRADED`, `ORDER_REJECT_SPIKE`, `RECONCILIATION_DRIFT`, `RISK_STATE_BREACH`, `CARDINALITY_BUDGET_EXCEEDED`, `SENSITIVE_FIELD_LEAK_ATTEMPT`, `DELAYED_TRACE_INGESTION`, `TELEMETRY_BACKEND_UNAVAILABLE`, `MONITORING_HEARTBEAT_MISSING`. |
| SLO targets are documented as proposed or measured | **PASS** | `config/slis.json` explicitly tags every SLI target as `PROPOSED`; `reports/scenario_validation_report.md` and `reports/demo_dashboard_snapshot.json` explicitly tag benchmark timings as `MEASURED_LOCAL_SYNTHETIC_BASELINE` with environment and sample size (`200` orders / `1200` spans). |
| Metrics cardinality is bounded | **PASS** | Forbidden label keys (`order_id`, `trace_id`, etc.) raise `CardinalityPolicyError`; 700 high-cardinality label values bounded via `__overflow__` below `512` global cap (`excessive_cardinality` scenario, `test_cardinality_caps_and_prometheus_export`). Baseline demo uses `31/512` series (`6.05%` utilization, `0` overflows). |
| Loss of monitoring is itself detectable | **PASS** | `MonitoringWatchdog` detects trace backend outage and missing heartbeats, emitting P1 alerts (`TELEMETRY_BACKEND_UNAVAILABLE`, `MONITORING_HEARTBEAT_MISSING`) on an out-of-band fallback channel while local spool buffers and replays spans on recovery (`unavailable_telemetry_backend` scenario); `/ready` returns HTTP `503`. |
| A clean checkout can run the documented setup, tests and demo with permitted fixtures | **PASS** | `bash scripts/reproduce.sh` succeeded offline with zero external dependencies (`21/21` tests in `0.042s`, `8/8` scenarios, 10-order demo) using synthetic fixture `data/fixtures/synthetic_ticks.jsonl` + `data/fixtures/manifest.json`. |
| Architecture, dependency assumptions, known limitations and runbook are committed | **PASS** | Committed in `docs/architecture.md`, `docs/telemetry-schema.md`, `docs/adr-001-telemetry-boundaries.md`, `docs/assumptions-and-dependencies.md`, `docs/runbooks.md`, and `README.md`. |
| Relevant CI checks pass and raw evaluation or test evidence is retained | **PASS** | `21/21` unit, integration, and scenario tests pass; raw evidence retained in `reports/scenario_validation_report.json`, `reports/scenario_validation_report.md`, `reports/demo_dashboard_snapshot.json`, `reports/demo_sample_trace_otlp.json`, `reports/demo_metrics.prom`, and `reports/demo_logs.jsonl`. |
| Security, data licensing, IP and deployment approval requirements are reviewed | **PASS** | Zero external repository code copied (clean adapter Protocols in `src/trading_observability/adapters.py`); 100% synthetic fixture provenance (`data/fixtures/manifest.json`); execution policy fails closed (`DISABLED` by default, `LIVE` hard-rejected); zero secrets in config (`docs/assumptions-and-dependencies.md`). |

## Milestone Completion Summary (`plan.md` Section 4)

- **M1 — Foundation and design:** Completed (`config/platform.json`, `config/slis.json`, `src/trading_observability/{config,schema,redaction}.py`, `docs/telemetry-schema.md`, `docs/adr-001-telemetry-boundaries.md`, `tests/unit/test_schema_and_redaction.py`).
- **M2 — Core implementation:** Completed (`src/trading_observability/{metrics,logging_service,tracing,adapters,pipeline,dashboards,server}.py`, `config/grafana/dashboards/*.json`, `scripts/demo.sh`, `tests/unit/test_metrics_and_tracing.py`, `tests/integration/test_pipeline_and_http.py`).
- **M3 — Integration and validation:** Completed (`src/trading_observability/alerting.py`, `config/prometheus/{prometheus,alerts}.yml`, `config/alertmanager/alertmanager.yml`, `config/otel/otel-collector-config.yaml`, `docker-compose.yml`).
- **M4 — Hardening and delivery:** Completed (`src/trading_observability/scenarios.py`, `tests/scenarios/test_fault_scenarios.py`, `docs/runbooks.md`, `docs/assumptions-and-dependencies.md`, `reports/*`).
