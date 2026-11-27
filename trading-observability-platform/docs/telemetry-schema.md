# Telemetry Schema, Correlation IDs, SLIs & Sensitive-Field Rules

Schema version: `1.0.0` (`SCHEMA_VERSION` in `src/trading_observability/__init__.py`).

## 1. Correlation Envelope (`CorrelationContext`)

Every cross-service hop propagates W3C TraceContext plus trading domain identifiers:

| Field | Format / Rule | Propagation Header | Allowed in Metric Labels? |
|---|---|---|---|
| `trace_id` | 32 lowercase hex chars (W3C) | `traceparent` | **FORBIDDEN** (high cardinality) |
| `span_id` | 16 lowercase hex chars (W3C) | `traceparent` | **FORBIDDEN** (high cardinality) |
| `parent_span_id` | 16 lowercase hex chars or `null` | `traceparent` | **FORBIDDEN** |
| `correlation_id` | Non-empty stable string (`corr-...`) | `x-correlation-id` | **FORBIDDEN** |
| `order_id` | Stable OMS order identifier (`ORD-...`) | `x-order-id` | **FORBIDDEN** |
| `client_order_id` | Idempotency key (`CLORD-...`) | Internal / audit only | **FORBIDDEN** |
| `feed_batch_id` | Market data batch ID (`FB-...`) | `x-feed-batch-id` | **FORBIDDEN** |
| `service` | Bounded allowlist (`market_data`, `research`, `risk`, `oms`, `broker`, `portfolio`, `observability`) | Span resource | **ALLOWED** |
| `venue` | Bounded allowlist (`SYNTH_EXCH_A`, `SYNTH_EXCH_B`, `PAPER_BROKER_1`, `UNKNOWN`) | Attribute | **ALLOWED** |
| `symbol` | Bounded allowlist in `config/platform.json` (overflow -> `__overflow__`) | Attribute | **ALLOWED (bounded)** |
| `status` | Bounded enum (`ok`, `filled`, `rejected`, `stale`, etc.) | Attribute | **ALLOWED** |

All timestamps are strictly UTC (`YYYY-MM-DDTHH:MM:SS.ffffffZ` and integer nanoseconds since epoch). Both `event_time_utc` and `available_time_utc` are recorded on market ticks; both `end_time_ns` and `ingested_at_ns` are recorded on spans to detect delayed trace ingestion.

## 2. Metrics Inventory & Cardinality Limits

Defined in `src/trading_observability/metrics.py` and configured in `config/platform.json`:
- Per-metric series cap: `64` (`max_series_per_metric`)
- Global series cap: `512` (`max_total_series`)
- Overflow bucket: `__overflow__`

| Metric Name | Type | Unit | Labels |
|---|---|---|---|
| `trading_market_data_ticks_total` | Counter | ticks | `venue`, `symbol`, `status` |
| `trading_market_data_age_seconds` | Gauge | seconds | `venue` |
| `trading_market_data_sequence_gaps_total` | Counter | gaps | `venue` |
| `trading_orders_total` | Counter | orders | `service`, `symbol`, `status` |
| `trading_oms_queue_lag_seconds` | Histogram | seconds | `service` |
| `trading_broker_ack_latency_seconds` | Histogram | seconds | `venue`, `status` |
| `trading_pipeline_end_to_end_latency_seconds` | Histogram | seconds | `status` |
| `trading_reconciliation_mismatches_total` | Counter | mismatches | `venue`, `symbol` |
| `trading_risk_state_code` | Gauge | enum (0/1/2) | `service` |
| `trading_alerts_fired_total` | Counter | alerts | `severity`, `status` |
| `trading_alerts_deduplicated_total` | Counter | alerts | `severity` |
| `observability_active_series_total` | Gauge | series | *(none)* |
| `observability_cardinality_overflow_total` | Counter | events | `metric`, `reason` |
| `observability_forbidden_label_rejected_total` | Counter | events | `metric` |
| `observability_sensitive_field_leak_blocked_total` | Counter | fields | `service` |
| `observability_trace_ingest_delay_seconds` | Histogram | seconds | `service` |
| `observability_backend_healthy` | Gauge | boolean | `backend` |
| `observability_spool_depth` | Gauge | events | `backend` |
| `observability_watchdog_heartbeat_age_seconds` | Gauge | seconds | `service` |

## 3. Sensitive-Field Classification & Redaction Rules

Enforced by `SensitiveFieldScrubber` (`src/trading_observability/redaction.py`) and mirrored in `config/otel/otel-collector-config.yaml`:

| Classification | Example Keys / Patterns | Action | Replacement Token |
|---|---|---|---|
| **Secrets & Credentials** | `api_key`, `secret`, `password`, `token`, `authorization`, `Bearer ...`, `sk_live_...` | Always scrubbed across all logs, traces, alerts, and roles | `[REDACTED:SECRET]` |
| **Confidential Order & Alpha Details** | `limit_price`, `order_qty`, `client_account`, `account_number`, `counterparty`, `strategy_params`, `alpha_weights`, `raw_order_payload`, `ACCT-...` | Scrubbed before storage/emission; extra masking of `client_order_id`/`strategy_id` for `VIEWER` | `[REDACTED:CONFIDENTIAL_ORDER]` |
| **PII** | `email`, `phone`, `ssn`, `trader_name`, `client_name`, email regex | Always scrubbed | `[REDACTED:PII]` |

Every interception increments `observability_sensitive_field_leak_blocked_total` and triggers `SENSITIVE_FIELD_LEAK_ATTEMPT`.

## 4. SLI / SLO Status Policy

All SLIs in `config/slis.json` are documented as **PROPOSED** for local/sandbox calibration. Local benchmarks report exact sample sizes, environment metadata, and `MEASURED_LOCAL_SYNTHETIC_BASELINE` tags. Production SLAs must never be inferred from synthetic benchmarks.
