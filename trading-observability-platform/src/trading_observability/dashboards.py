"""Trading health, latency, SLI/SLO evaluation, and observability self-health dashboards."""

from __future__ import annotations

from typing import Any, Dict, List

from trading_observability.alerting import AlertRouter
from trading_observability.config import PlatformConfig, load_slis_catalog
from trading_observability.metrics import BoundedMetricsRegistry
from trading_observability.schema import ns_to_utc_iso, utc_now_ns
from trading_observability.tracing import TraceStore


def build_dashboard_snapshot(
    config: PlatformConfig,
    metrics: BoundedMetricsRegistry,
    trace_store: TraceStore,
    router: AlertRouter,
) -> Dict[str, Any]:
    """Build deterministic Trading Health, Latency, and Observability Self-Health dashboard data."""
    slis_catalog = load_slis_catalog()
    e2e_lat = metrics.get_histogram_quantiles("trading_pipeline_end_to_end_latency_seconds")
    oms_lat = metrics.get_histogram_quantiles("trading_oms_queue_lag_seconds")
    broker_lat = metrics.get_histogram_quantiles("trading_broker_ack_latency_seconds")
    trace_delay = metrics.get_histogram_quantiles("observability_trace_ingest_delay_seconds")

    total_orders = metrics.get_counter_sum("trading_orders_total", {"service": "oms"})
    filled_orders = metrics.get_counter_sum("trading_orders_total", {"service": "oms", "status": "filled"})
    rejected_orders = metrics.get_counter_sum("trading_orders_total", {"service": "oms", "status": "rejected"})
    ticks_total = metrics.get_counter_sum("trading_market_data_ticks_total")
    max_feed_age = metrics.get_gauge_max("trading_market_data_age_seconds") or 0.0
    mismatches = metrics.get_counter_sum("trading_reconciliation_mismatches_total")
    risk_code = metrics.get_gauge_max("trading_risk_state_code") or 0.0
    card = metrics.cardinality_report()

    active_alerts = [a.to_dict() for a in router.active_alerts()]
    risk_state_str = "HEALTHY" if risk_code == 0 else ("DEGRADED" if risk_code == 1 else "BREACHED_KILL_SWITCH")

    sli_status: List[Dict[str, Any]] = [
        {
            "id": "sli_market_data_freshness",
            "measured_value": round(max_feed_age, 4),
            "unit": "seconds",
            "target_status": "PROPOSED",
            "threshold": config.alerting.stale_market_data_seconds,
            "within_proposed_threshold": max_feed_age <= config.alerting.stale_market_data_seconds,
        },
        {
            "id": "sli_oms_queue_latency",
            "measured_value": round(oms_lat["p99"] * 1000.0, 3),
            "unit": "ms (p99)",
            "target_status": "PROPOSED",
            "threshold": config.alerting.oms_queue_lag_ms,
            "within_proposed_threshold": (oms_lat["p99"] * 1000.0) <= config.alerting.oms_queue_lag_ms,
        },
        {
            "id": "sli_broker_ack_latency",
            "measured_value": round(broker_lat["p95"] * 1000.0, 3),
            "unit": "ms (p95)",
            "target_status": "PROPOSED",
            "threshold": config.alerting.broker_latency_ms,
            "within_proposed_threshold": (broker_lat["p95"] * 1000.0) <= config.alerting.broker_latency_ms,
        },
        {
            "id": "sli_order_reject_ratio",
            "measured_value": round(rejected_orders / total_orders, 4) if total_orders > 0 else 0.0,
            "unit": "ratio",
            "target_status": "PROPOSED",
            "threshold": config.alerting.order_reject_rate_ratio,
            "within_proposed_threshold": (
                (rejected_orders / total_orders) < config.alerting.order_reject_rate_ratio
                if total_orders > 0
                else True
            ),
        },
        {
            "id": "sli_reconciliation_accuracy",
            "measured_value": int(mismatches),
            "unit": "mismatches",
            "target_status": "PROPOSED",
            "threshold": 0,
            "within_proposed_threshold": mismatches == 0,
        },
        {
            "id": "sli_trace_correlation_completeness",
            "measured_value": round(trace_delay["max"], 4),
            "unit": "seconds (max ingest delay)",
            "target_status": "PROPOSED",
            "threshold": config.telemetry.trace_ingest_delay_threshold_seconds,
            "within_proposed_threshold": trace_delay["max"] <= config.telemetry.trace_ingest_delay_threshold_seconds,
        },
    ]

    return {
        "generated_at_utc": ns_to_utc_iso(utc_now_ns()),
        "environment": config.environment,
        "execution_mode": config.execution_policy.mode,
        "slo_disclaimer": slis_catalog.get("disclaimer", ""),
        "trading_health": {
            "market_data_ticks_total": int(ticks_total),
            "market_data_max_age_seconds": round(max_feed_age, 4),
            "orders_total": int(total_orders),
            "orders_filled": int(filled_orders),
            "orders_rejected": int(rejected_orders),
            "order_reject_ratio": round(rejected_orders / total_orders, 4) if total_orders > 0 else 0.0,
            "reconciliation_mismatches": int(mismatches),
            "risk_state": risk_state_str,
            "active_alerts_count": len(active_alerts),
        },
        "latency_ms": {
            "measurement_type": "MEASURED_LOCAL_SYNTHETIC_BASELINE",
            "end_to_end": _to_ms_summary(e2e_lat),
            "oms_queue_lag": _to_ms_summary(oms_lat),
            "broker_ack": _to_ms_summary(broker_lat),
        },
        "observability_self_health": {
            "trace_backend_available": trace_store.is_backend_available,
            "trace_spool_depth": trace_store.spool_depth,
            "trace_total_spans": trace_store.total_span_count(),
            "trace_delayed_spans": trace_store.delayed_spans_count,
            "trace_ingest_delay_seconds": {k: round(v, 4) for k, v in trace_delay.items()},
            "cardinality": card,
            "sensitive_fields_blocked": int(
                metrics.get_counter_sum("observability_sensitive_field_leak_blocked_total")
            ),
            "alerts_deduplicated": router.deduplicated_count,
        },
        "sli_evaluations": sli_status,
        "active_alerts": active_alerts,
        "recent_traces": trace_store.list_recent_traces(limit=10),
    }


def _to_ms_summary(quantiles: Dict[str, float]) -> Dict[str, float]:
    return {
        "sample_count": int(quantiles["count"]),
        "p50_ms": round(quantiles["p50"] * 1000.0, 3),
        "p95_ms": round(quantiles["p95"] * 1000.0, 3),
        "p99_ms": round(quantiles["p99"] * 1000.0, 3),
        "max_ms": round(quantiles["max"] * 1000.0, 3),
    }
