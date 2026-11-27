# ADR-001: Telemetry Boundaries, Cardinality Enforcement, and Fail-Safe Execution

- **Status:** Accepted (MVP Local/Sandbox Prototype)
- **Date:** 2026-11-27

## Context
Trading systems span multiple latency-sensitive services (`market_data`, `research`, `risk`, `oms`, `broker`, `portfolio`). Naive observability implementations introduce three high-severity operational hazards:
1. Unbounded metric labels (`order_id`, arbitrary tickers) causing Prometheus OOM crashes.
2. Accidental logging of broker API keys or confidential order details (`limit_price`, `order_qty`, `strategy_params`) to shared dashboards.
3. Synchronous telemetry failures stalling order execution or silently failing without alerting operators that monitoring itself is down.

## Decision
1. **Zero-Dependency Core with Standards-Compliant Wire Formats:** Implement the core telemetry engine in Python 3.9+ stdlib while exporting standard Prometheus text exposition (`0.0.4`), W3C `traceparent` headers, OpenTelemetry OTLP/JSON (`resourceSpans`), and Grafana/Prometheus/Alertmanager/OTel-Collector configs. This guarantees a clean checkout runs setup, tests, and demos deterministically without external network downloads.
2. **Strict Bounded Cardinality by Construction:** Maintain an explicit forbidden-label set (`order_id`, `trace_id`, `account_id`, etc.) that raises `CardinalityPolicyError`, combined with per-label allowlists, per-metric/global series caps, and `__overflow__` folding.
3. **Mandatory Redaction Firewall + RBAC Trace Projection:** Route all logs, span attributes, and alert annotations through `SensitiveFieldScrubber`. Default `/traces/<order_id>` to `VIEWER` and gate `AUDITOR_AUTHORIZED` behind an environment-supplied token (`TRADING_OBS_AUDITOR_TOKEN`).
4. **Fail-Open Telemetry / Fail-Closed Trading Execution:** Telemetry storage outages buffer into a bounded local spool and trigger an out-of-band `MonitoringWatchdog` alert without blocking trading; conversely, order execution defaults to `DISABLED` and rejects `LIVE` mode outright.

## Consequences
- Operators get full 6-service order trace correlation without confidential data leaks.
- Cardinality is provably bounded under adversarial label floods.
- Moving from local/sandbox to production requires replacing SQLite with managed trace storage (e.g., Tempo/Jaeger), configuring external secret stores for Alertmanager/Grafana, and establishing real environment baselines before ratifying SLOs.
