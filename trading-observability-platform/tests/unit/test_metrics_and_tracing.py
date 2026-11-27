"""Unit tests for bounded-cardinality metrics registry and SQLite/OTLP trace store."""

from __future__ import annotations

import unittest

from trading_observability.config import TelemetryConfig
from trading_observability.metrics import (
    OVERFLOW_LABEL_VALUE,
    BoundedMetricsRegistry,
    CardinalityPolicyError,
    register_trading_metrics,
)
from trading_observability.schema import SpanRecord, generate_span_id, generate_trace_id, utc_now_ns
from trading_observability.tracing import TraceStore


class TestMetricsAndTracing(unittest.TestCase):
    def test_forbidden_label_rejected_at_registration_and_emission(self) -> None:
        cfg = TelemetryConfig(max_series_per_metric=16, max_total_series=128)
        reg = BoundedMetricsRegistry(cfg)

        with self.assertRaises(CardinalityPolicyError):
            reg.register_counter("bad_metric", "Bad metric", label_names=("order_id",))

        register_trading_metrics(reg, cfg)
        with self.assertRaises(CardinalityPolicyError):
            reg.inc_counter(
                "trading_orders_total",
                1.0,
                {"service": "oms", "symbol": "SYN-EQ-01", "status": "filled", "trace_id": "abc"},
            )

    def test_cardinality_caps_and_prometheus_export(self) -> None:
        cfg = TelemetryConfig(max_series_per_metric=8, max_total_series=128)
        reg = BoundedMetricsRegistry(cfg)
        reg.register_counter("demo_events_total", "Demo events", label_names=("bucket",), max_series=5)
        for i in range(50):
            reg.inc_counter("demo_events_total", 1.0, {"bucket": f"val-{i}"})

        report = reg.cardinality_report()
        self.assertLessEqual(report["per_metric_series"]["demo_events_total"], 6)
        self.assertGreater(report["overflow_events"], 0)

        prom_text = reg.export_prometheus()
        self.assertIn("# HELP demo_events_total", prom_text)
        self.assertIn("# TYPE demo_events_total counter", prom_text)
        self.assertIn(OVERFLOW_LABEL_VALUE, prom_text)

    def test_trace_store_otlp_export_and_retention(self) -> None:
        cfg = TelemetryConfig(trace_retention_seconds=60, trace_max_spans=3, trace_ingest_delay_threshold_seconds=5.0)
        store = TraceStore(config=cfg)
        try:
            tid = generate_trace_id("retention-test")
            base_ns = utc_now_ns()
            for i in range(5):
                sp = SpanRecord(
                    trace_id=tid,
                    span_id=generate_span_id(f"sp-{i}"),
                    parent_span_id=None,
                    correlation_id="corr-ret",
                    service="oms",
                    operation=f"op.{i}",
                    order_id="ORD-RET-1",
                    start_time_ns=base_ns + i * 1_000_000,
                    end_time_ns=base_ns + (i + 1) * 1_000_000,
                    attributes={"step": i, "api_key": "sk_live_SHOULD_NOT_STORE"},
                )
                store.ingest_span(sp, ingested_at_ns=sp.end_time_ns + 1_000)

            otlp = store.export_otlp_json(trace_id=tid)
            self.assertEqual(len(otlp["resourceSpans"]), 1)
            self.assertNotIn("sk_live_SHOULD_NOT_STORE", str(otlp))

            # Enforce max_spans cap (5 -> 3)
            res_cap = store.enforce_retention(now_ns=base_ns + 10_000_000_000)
            self.assertEqual(res_cap["deleted_by_cap"], 2)
            self.assertEqual(res_cap["remaining_spans"], 3)

            # Enforce TTL expiry (3 -> 0)
            res_age = store.enforce_retention(now_ns=base_ns + 120_000_000_000)
            self.assertEqual(res_age["deleted_by_age"], 3)
            self.assertEqual(res_age["remaining_spans"], 0)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
