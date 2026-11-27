# Assumptions, Integration Dependencies & Security / IP Review

## 1. External Foundation Dependencies (Section 2 of `plan.md`)

`plan.md` references four external repositories and four related plans (#13, #14, #20, #22):
- `Trading-Software — 08-market-data-platform`
- `Trading-Software — 01-agentic-trading-platform`
- `High-Frequency-Trading — 09-latency-performance-lab`
- `Trading-Software — 13-broker-reliability-reconciliation`

### IP, Licensing & Access Disposition
Per `plan.md` Section 2 ("Confirm access, licenses, background IP and SAAT ownership terms before copying or adapting any personal repository... If access or licensing is unresolved, implement against clean, documented contracts instead"):
- **Zero code was copied** from any external personal repository.
- Clean, versioned Python `Protocol` contracts (`MarketDataAdapter`, `ResearchSignalAdapter`, `BrokerReconciliationAdapter`) are defined in `src/trading_observability/adapters.py`.
- Once access, licensing, and SAAT background IP terms are formally approved, adapters for those repositories can plug directly into these contracts without modifying core observability logic.

## 2. Data Provenance & Entitlements

- All fixtures under `data/fixtures/` are **100% synthetic** (`data/fixtures/manifest.json`, seed `20261127`).
- No vendor market data, no live client accounts, and no real broker credentials exist anywhere in the repository.

## 3. Security & Operational Controls Checklist

| Control | Implementation | Verification |
|---|---|---|
| No secrets in repository | `config/` and `docker-compose.yml` contain zero secrets; auditor token read from `TRADING_OBS_AUDITOR_TOKEN`; Grafana requires `GRAFANA_ADMIN_PASSWORD` env var | Automated unit/integration tests + config audit |
| No confidential order exposure | `SensitiveFieldScrubber` strips secrets, PII, `limit_price`, `order_qty`, `client_account`, `strategy_params`; `/traces/<order_id>` defaults to `VIEWER` and returns HTTP `403` for unauthorized auditor requests | `test_schema_and_redaction.py`, `test_pipeline_and_http.py`, `sensitive_field_leak` scenario |
| Execution disabled by default | `config/platform.json` sets `execution_policy.mode = "DISABLED"` and `allow_live_execution = false`; `LIVE` raises `PermissionError` | `test_execution_policy_fails_closed_by_default` |
| No invented SLAs | `config/slis.json` and dashboard snapshots explicitly tag targets as `PROPOSED` and benchmarks as `MEASURED_LOCAL_SYNTHETIC_BASELINE` | `test_pipeline_and_http.py`, `reports/scenario_validation_report.md` |
| Loss of monitoring detectable | `MonitoringWatchdog` monitors both `trace_store.is_backend_available` and per-service heartbeats, emitting P1 alerts to an out-of-band fallback channel | `unavailable_telemetry_backend` scenario |

## 4. Known Limitations & Next Decisions

1. **Asset Class & Horizon:** Confirm actual SAAT asset class, strategy horizon, and real broker/market-data vendor entitlements before building production adapters.
2. **Trace Storage & Metrics Backend:** Local MVP uses SQLite + in-process Prometheus registry; production requires persistent multi-node Prometheus/Mimir, Tempo/Jaeger, and centralized log aggregation.
3. **Simulated Timings:** Pipeline latencies in local demos and fault scenarios use deterministic simulated durations for reproducibility; real baseline measurements must be collected in the target sandbox/staging environment before ratifying SLO thresholds.
