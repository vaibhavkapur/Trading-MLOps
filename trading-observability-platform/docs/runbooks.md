# Operator Incident Runbooks

Every alert emitted by `AlertPolicyEngine` and Prometheus (`config/prometheus/alerts.yml`) links directly to a section below. Never expose confidential order details or raw credentials in incident tickets or chat channels.

---

## `#stale-market-data` — `STALE_MARKET_DATA` (`P1_CRITICAL`)
- **Trigger:** `trading_market_data_age_seconds > 2.0s` or `trading_market_data_sequence_gaps_total > 0`.
- **Automated Safe Fallback:** Pipeline halts new order submission (`halted_stale_feed`) while feed is stale or gapped.
- **Operator Steps:**
  1. Check `/dashboard` -> `trading_health.market_data_max_age_seconds` and venue label in `/metrics`.
  2. Inspect `market_data` logs for disconnect, sequence gap, or upstream entitlement errors.
  3. Confirm OMS is not dispatching orders against stale quotes.
  4. Reconnect or fail over feed adapter; verify `trading_market_data_age_seconds` returns below threshold and sequence numbers resume contiguously before clearing incident.

## `#oms-queue-lag` — `OMS_QUEUE_LAG` (`P2_HIGH`)
- **Trigger:** `trading_oms_queue_lag_seconds` p95 > 150ms (proposed sandbox threshold).
- **Operator Steps:**
  1. Check `/dashboard` -> `latency_ms.oms_queue_lag` and inspect recent traces via `/traces/<order_id>`.
  2. Determine whether lag originates in OMS dispatch or downstream broker backpressure.
  3. Throttle non-critical paper/sandbox order generators and verify queue drains without duplicate orders (`client_order_id` idempotency).

## `#broker-latency-degraded` — `BROKER_LATENCY_DEGRADED` (`P2_HIGH`)
- **Trigger:** `trading_broker_ack_latency_seconds` p95 > 250ms (proposed sandbox threshold).
- **Operator Steps:**
  1. Inspect trace waterfall (`/traces/<order_id>`) to confirm `broker.execute_paper_order` span accounts for the delay.
  2. Check venue-scoped latency in `/metrics` (`trading_broker_ack_latency_seconds_bucket`).
  3. Verify timeout/retry policies are bounded and check for uncertain order outcomes requiring reconciliation.

## `#order-reject-spike` — `ORDER_REJECT_SPIKE` (`P2_HIGH`)
- **Trigger:** Rolling OMS order reject ratio >= 25% (minimum 4 orders).
- **Operator Steps:**
  1. Check `oms` and `broker` structured logs by `correlation_id` for reject reason codes.
  2. Verify whether rejects stem from risk limits, malformed synthetic inputs, or broker sandbox availability.
  3. Pause automated strategy order submission until root cause is resolved.

## `#reconciliation-drift` — `RECONCILIATION_DRIFT` (`P1_CRITICAL`)
- **Trigger:** `trading_reconciliation_mismatches_total > 0`.
- **Operator Steps:**
  1. Immediately halt new execution (keep risk state degraded/kill-switch engaged until reconciled).
  2. Pull correlated traces (`/traces/<order_id>`) for affected `symbol` and `venue`.
  3. Compare broker execution reports vs portfolio ledger state using an authorized auditor role (`AUDITOR_AUTHORIZED`).
  4. Resolve position discrepancy and verify zero mismatches over the next reconciliation cycle before resuming.

## `#risk-state-breach` — `RISK_STATE_BREACH` (`P1_CRITICAL` / `P2_HIGH`)
- **Trigger:** `trading_risk_state_code >= 1` (`1=DEGRADED`, `2=BREACHED_KILL_SWITCH`).
- **Operator Steps:**
  1. Confirm kill-switch has blocked further order submissions (`rejected_risk`).
  2. Review `risk.pre_trade_check` spans and risk engine logs.
  3. Do **not** override kill-switch without documented engineering/risk approval.

## `#cardinality-budget-exceeded` — `CARDINALITY_BUDGET_EXCEEDED` (`P2_HIGH`)
- **Trigger:** Active series utilization >= 80% of cap (`512`), `__overflow__` folding active, or forbidden label key rejected.
- **Operator Steps:**
  1. Inspect `/dashboard` -> `observability_self_health.cardinality.per_metric_series`.
  2. Check `observability_cardinality_overflow_total` and `observability_forbidden_label_rejected_total` to identify the offending metric and service.
  3. Fix instrumentation to move unbounded IDs (`order_id`, `trace_id`, dynamic symbols) into trace attributes/logs rather than metric labels.

## `#sensitive-field-leak-attempt` — `SENSITIVE_FIELD_LEAK_ATTEMPT` (`P2_HIGH`)
- **Trigger:** `observability_sensitive_field_leak_blocked_total > 0`.
- **Operator Steps:**
  1. Confirm the redaction firewall blocked the raw value (check logs contain `[REDACTED:*]`, never raw secrets/order prices).
  2. Identify the emitting `service` from `observability_sensitive_field_leak_blocked_total{service="..."}`.
  3. Rotate any real credential if a non-synthetic secret ever reached application memory, and patch the emitting call site.

## `#delayed-trace-ingestion` — `DELAYED_TRACE_INGESTION` (`P3_WARNING`)
- **Trigger:** `observability_trace_ingest_delay_seconds` > 5.0s or `delayed_spans > 0`.
- **Operator Steps:**
  1. Verify trace correlation completeness (`/traces/<order_id>` should still stitch all 6 services once spans arrive).
  2. Check OTel collector batch queue, network latency, and clock skew across services (NTP/UTC sync).

## `#telemetry-backend-unavailable` — `TELEMETRY_BACKEND_UNAVAILABLE` (`P1_CRITICAL`)
- **Trigger:** `observability_backend_healthy < 1` (raised via independent `MonitoringWatchdog` out-of-band channel).
- **Operator Steps:**
  1. Confirm trading services remain unblocked while `observability_spool_depth` buffers telemetry locally.
  2. Check disk/storage health and SQLite/OTLP backend connectivity (`/ready`).
  3. Restore backend and verify spooled spans replay (`spool_depth` returns to `0`) and check `dropped_spool_events` for any gap during prolonged outage.

## `#monitoring-heartbeat-missing` — `MONITORING_HEARTBEAT_MISSING` (`P1_CRITICAL`)
- **Trigger:** `observability_watchdog_heartbeat_age_seconds > 10.0s` or missing metric series.
- **Operator Steps:**
  1. Treat loss of monitoring as a blind-flying condition — halt unattended strategy runs if telemetry cannot be restored promptly.
  2. Check process liveness, scrape targets, and network path between services and the collector.
  3. Verify heartbeats resume for all 6 trading services before clearing alert.
