# Trading Observability Platform — Fault Scenarios & Load Baseline Evidence

- **Generated UTC:** `2026-11-27T10:15:13.751102Z`
- **Fixed Seed:** `20261127`
- **Result:** `8/8` scenarios passed (`all_scenarios_passed=True`)

## Fault Scenario Matrix

| Scenario | Passed | Alerts Fired | Summary |
|---|---|---|---|
| `lost_feed` | **PASS** | `STALE_MARKET_DATA` | Lost/stale market data triggered STALE_MARKET_DATA alert, halted trading safely, and recovered cleanly. |
| `slow_broker` | **PASS** | `OMS_QUEUE_LAG`, `BROKER_LATENCY_DEGRADED` | Injected broker + OMS latency triggered BROKER_LATENCY_DEGRADED and OMS_QUEUE_LAG and was localized to broker spans. |
| `excessive_cardinality` | **PASS** | `CARDINALITY_BUDGET_EXCEEDED` | Forbidden label key rejected, 700 high-cardinality label values bounded via __overflow__, and CARDINALITY_BUDGET_EXCEEDED fired. |
| `sensitive_field_leak` | **PASS** | `SENSITIVE_FIELD_LEAK_ATTEMPT` | Injected API keys, bearer tokens, account numbers, and confidential order params were 100% scrubbed while preserving full 6-service trace correlation. |
| `delayed_trace` | **PASS** | `DELAYED_TRACE_INGESTION` | 25s delayed broker/portfolio spans were correlated into complete 6-span order traces and flagged by DELAYED_TRACE_INGESTION. |
| `alert_duplication` | **PASS** | `ORDER_REJECT_SPIKE` | 10 repeated alerts within cooldown collapsed into 1 routed incident (9 deduplicated), then re-notified cleanly after cooldown. |
| `unavailable_telemetry_backend` | **PASS** | `TELEMETRY_BACKEND_UNAVAILABLE`, `MONITORING_HEARTBEAT_MISSING`, `MONITORING_HEARTBEAT_MISSING`, `MONITORING_HEARTBEAT_MISSING` | Backend outage did not block paper orders; 18 spans spooled locally, independent watchdog fired out-of-band P1 alerts, and all 18 spans replayed on recovery. |
| `m3_m4_alert_policies_and_retention` | **PASS** | `ORDER_REJECT_SPIKE`, `RECONCILIATION_DRIFT`, `RISK_STATE_BREACH` | Verified ORDER_REJECT_SPIKE, RECONCILIATION_DRIFT, RISK_STATE_BREACH alerts and TTL trace retention pruning. |

## Local Synthetic Load Baseline (Measured — Not a Production SLA)

> Local deterministic synthetic benchmark only. Never infer live trading throughput or production SLA from this test.

- **Measurement Status:** `MEASURED_LOCAL_SYNTHETIC_BASELINE`
- **Environment:** `macOS-26.5.1-arm64-arm-64bit` | Python `3.9.6` | Arch `arm64`
- **Sample Size:** `200` synthetic paper orders (`1200` spans, `1200` redacted logs)
- **Wall Clock:** `0.0618s` (`3236.29` orders/sec, `0.309 ms/order`)
- **Simulated End-to-End Pipeline Latency (ms):** p50=`21.428`, p95=`27.746`, p99=`28.149`
- **Metrics Cardinality:** `31/512` active series (utilization `6.05%`, overflows `0`)
