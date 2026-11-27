"""Scenario tests covering all 7 required failure paths from plan.md plus M3/M4 drill and load baseline."""

from __future__ import annotations

import unittest

from trading_observability.scenarios import (
    run_load_benchmark,
    run_m3_m4_policy_and_retention_drill,
    run_scenario_alert_duplication,
    run_scenario_delayed_trace,
    run_scenario_excessive_cardinality,
    run_scenario_lost_feed,
    run_scenario_sensitive_field_leak,
    run_scenario_slow_broker,
    run_scenario_unavailable_telemetry_backend,
)


class TestFaultScenarios(unittest.TestCase):
    def test_01_lost_feed(self) -> None:
        outcome = run_scenario_lost_feed(seed=20261127)
        self.assertTrue(outcome.passed, outcome.evidence)
        self.assertIn("STALE_MARKET_DATA", outcome.alerts_fired)

    def test_02_slow_broker(self) -> None:
        outcome = run_scenario_slow_broker(seed=20261127)
        self.assertTrue(outcome.passed, outcome.evidence)
        self.assertIn("BROKER_LATENCY_DEGRADED", outcome.alerts_fired)
        self.assertIn("OMS_QUEUE_LAG", outcome.alerts_fired)

    def test_03_excessive_cardinality(self) -> None:
        outcome = run_scenario_excessive_cardinality(seed=20261127)
        self.assertTrue(outcome.passed, outcome.evidence)
        self.assertIn("CARDINALITY_BUDGET_EXCEEDED", outcome.alerts_fired)

    def test_04_sensitive_field_leak(self) -> None:
        outcome = run_scenario_sensitive_field_leak(seed=20261127)
        self.assertTrue(outcome.passed, outcome.evidence)
        self.assertEqual(outcome.evidence["leaks_found"], [])
        self.assertIn("SENSITIVE_FIELD_LEAK_ATTEMPT", outcome.alerts_fired)

    def test_05_delayed_trace(self) -> None:
        outcome = run_scenario_delayed_trace(seed=20261127)
        self.assertTrue(outcome.passed, outcome.evidence)
        self.assertIn("DELAYED_TRACE_INGESTION", outcome.alerts_fired)

    def test_06_alert_duplication(self) -> None:
        outcome = run_scenario_alert_duplication(seed=20261127)
        self.assertTrue(outcome.passed, outcome.evidence)
        self.assertEqual(outcome.evidence["deduplicated_count"], 9)

    def test_07_unavailable_telemetry_backend(self) -> None:
        outcome = run_scenario_unavailable_telemetry_backend(seed=20261127)
        self.assertTrue(outcome.passed, outcome.evidence)
        self.assertIn("TELEMETRY_BACKEND_UNAVAILABLE", outcome.alerts_fired)
        self.assertIn("MONITORING_HEARTBEAT_MISSING", outcome.alerts_fired)

    def test_08_m3_m4_policy_and_retention_drill(self) -> None:
        outcome = run_m3_m4_policy_and_retention_drill(seed=20261127)
        self.assertTrue(outcome.passed, outcome.evidence)

    def test_09_local_synthetic_load_baseline(self) -> None:
        bm = run_load_benchmark(order_count=50, seed=20261127)
        self.assertEqual(bm["measurement_status"], "MEASURED_LOCAL_SYNTHETIC_BASELINE")
        self.assertEqual(bm["results"]["orders_processed"], 50)
        self.assertEqual(bm["results"]["spans_ingested"], 300)
        self.assertLessEqual(
            bm["results"]["cardinality_total_series"], bm["results"]["cardinality_max_series"]
        )


if __name__ == "__main__":
    unittest.main()
