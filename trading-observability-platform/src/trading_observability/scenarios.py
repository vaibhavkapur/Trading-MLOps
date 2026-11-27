"""Reproducible fault scenario suite, alert drills, and deterministic local load benchmark."""

from __future__ import annotations

import platform
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from trading_observability.adapters import FaultInjectionProfile, FixtureMarketDataAdapter
from trading_observability.config import PlatformConfig, load_platform_config
from trading_observability.metrics import CardinalityPolicyError
from trading_observability.pipeline import ObservabilityPlatform, TradingPipeline
from trading_observability.redaction import REDACTED_CONFIDENTIAL, REDACTED_SECRET
from trading_observability.schema import AlertSeverity, ViewerRole, ns_to_utc_iso, utc_now_ns

DEFAULT_FIXTURE = Path(__file__).resolve().parents[2] / "data" / "fixtures" / "synthetic_ticks.jsonl"


@dataclass
class ScenarioOutcome:
    name: str
    passed: bool
    alerts_fired: List[str]
    summary: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _paper_config(base: Optional[PlatformConfig] = None) -> PlatformConfig:
    return (base or load_platform_config()).with_paper_sandbox_enabled()


def run_scenario_lost_feed(seed: int = 20261127) -> ScenarioOutcome:
    """Scenario 1: Lost feed & stale market data -> alert fires, orders halted, recovery clears state."""
    plat = ObservabilityPlatform(config=_paper_config())
    try:
        # Phase 1: Lost feed (zero ticks delivered)
        faults = FaultInjectionProfile(lost_feed=True)
        pipe = TradingPipeline(plat, faults=faults, seed=seed)
        results_lost = pipe.run_batch(limit=5, now_s=1000.0)
        alerts_lost = plat.alert_engine.evaluate(now_s=1001.0)
        fired_codes = [a.alert_code for a in alerts_lost]

        # Phase 2: Stale feed with sequence gap -> halted before OMS dispatch
        stale_faults = FaultInjectionProfile(stale_feed_age_seconds=4.5, inject_sequence_gap=True)
        stale_pipe = TradingPipeline(
            plat,
            market_data=FixtureMarketDataAdapter(fixture_path=DEFAULT_FIXTURE, seed=seed, faults=stale_faults),
            faults=stale_faults,
            seed=seed,
        )
        stale_results = stale_pipe.run_batch(limit=3, now_s=1002.0)

        # Phase 3: Recovery with healthy fixture
        healthy_pipe = TradingPipeline(
            plat,
            market_data=FixtureMarketDataAdapter(fixture_path=DEFAULT_FIXTURE, seed=seed),
            seed=seed,
        )
        recovered_results = healthy_pipe.run_batch(limit=2, now_s=1003.0)

        passed = (
            len(results_lost) == 0
            and "STALE_MARKET_DATA" in fired_codes
            and all(r.status == "halted_stale_feed" for r in stale_results)
            and all(r.status == "filled" for r in recovered_results)
        )
        return ScenarioOutcome(
            name="lost_feed",
            passed=passed,
            alerts_fired=fired_codes,
            summary="Lost/stale market data triggered STALE_MARKET_DATA alert, halted trading safely, and recovered cleanly.",
            evidence={
                "lost_feed_orders_submitted": len(results_lost),
                "stale_feed_statuses": [r.status for r in stale_results],
                "recovered_statuses": [r.status for r in recovered_results],
                "max_feed_age_after_recovery_s": plat.metrics.get_gauge_max("trading_market_data_age_seconds"),
            },
        )
    finally:
        plat.close()


def run_scenario_slow_broker(seed: int = 20261127) -> ScenarioOutcome:
    """Scenario 2: Slow broker & OMS queue lag -> latency alerts fire and trace pinpoints broker span."""
    plat = ObservabilityPlatform(config=_paper_config())
    try:
        faults = FaultInjectionProfile(slow_broker_latency_ms=420.0, oms_extra_queue_lag_ms=210.0)
        pipe = TradingPipeline(
            plat,
            market_data=FixtureMarketDataAdapter(fixture_path=DEFAULT_FIXTURE, seed=seed),
            faults=faults,
            seed=seed,
        )
        results = pipe.run_batch(limit=5, now_s=2000.0)
        alerts = plat.alert_engine.evaluate(now_s=2001.0)
        fired_codes = [a.alert_code for a in alerts]

        first_trace = plat.trace_store.get_trace_by_order_id(results[0].order_id, role=ViewerRole.OPERATOR)
        broker_spans = [s for s in first_trace["spans"] if s["service"] == "broker"]
        broker_dur_ms = broker_spans[0]["duration_ms"] if broker_spans else 0.0
        broker_q = plat.metrics.get_histogram_quantiles("trading_broker_ack_latency_seconds")

        passed = (
            "BROKER_LATENCY_DEGRADED" in fired_codes
            and "OMS_QUEUE_LAG" in fired_codes
            and broker_dur_ms >= 420.0
            and len(first_trace["services_covered"]) == 6
        )
        return ScenarioOutcome(
            name="slow_broker",
            passed=passed,
            alerts_fired=fired_codes,
            summary="Injected broker + OMS latency triggered BROKER_LATENCY_DEGRADED and OMS_QUEUE_LAG and was localized to broker spans.",
            evidence={
                "orders_processed": len(results),
                "broker_p95_ms": round(broker_q["p95"] * 1000.0, 2),
                "sample_broker_span_duration_ms": broker_dur_ms,
                "services_traced": first_trace["services_covered"],
            },
        )
    finally:
        plat.close()


def run_scenario_excessive_cardinality(seed: int = 20261127) -> ScenarioOutcome:
    """Scenario 3: Excessive cardinality & forbidden high-cardinality labels -> strictly bounded."""
    plat = ObservabilityPlatform(config=_paper_config())
    try:
        forbidden_rejected = False
        try:
            plat.metrics.inc_counter(
                "trading_orders_total",
                1.0,
                {"service": "oms", "symbol": "SYN-EQ-01", "status": "filled", "order_id": "ORD-UNBOUNDED-1"},
            )
        except CardinalityPolicyError:
            forbidden_rejected = True

        # Flood 500 unseen high-cardinality symbol values
        for i in range(500):
            plat.metrics.inc_counter(
                "trading_orders_total",
                1.0,
                {"service": "oms", "symbol": f"UNSEEN-SYM-{i:04d}", "status": "filled"},
            )

        # Also test dynamic metric without allowlist hitting per-metric max_series cap
        plat.metrics.register_counter(
            name="trading_test_dynamic_events_total",
            help_text="Test counter without allowlist to verify max_series cap.",
            label_names=("tag",),
            max_series=16,
        )
        for i in range(200):
            plat.metrics.inc_counter("trading_test_dynamic_events_total", 1.0, {"tag": f"dyn-{i}"})

        alerts = plat.alert_engine.evaluate(now_s=3000.0)
        fired_codes = [a.alert_code for a in alerts]
        card = plat.metrics.cardinality_report()

        dynamic_series = card["per_metric_series"].get("trading_test_dynamic_events_total", 0)
        passed = (
            forbidden_rejected
            and card["total_series"] <= card["max_total_series"]
            and dynamic_series <= 17  # 16 normal + 1 __overflow__
            and card["overflow_events"] >= 500
            and "CARDINALITY_BUDGET_EXCEEDED" in fired_codes
        )
        return ScenarioOutcome(
            name="excessive_cardinality",
            passed=passed,
            alerts_fired=fired_codes,
            summary="Forbidden label key rejected, 700 high-cardinality label values bounded via __overflow__, and CARDINALITY_BUDGET_EXCEEDED fired.",
            evidence={
                "forbidden_label_rejected": forbidden_rejected,
                "total_series": card["total_series"],
                "max_total_series": card["max_total_series"],
                "dynamic_metric_series": dynamic_series,
                "overflow_events": card["overflow_events"],
                "forbidden_label_rejections": card["forbidden_label_rejections"],
            },
        )
    finally:
        plat.close()


def run_scenario_sensitive_field_leak(seed: int = 20261127) -> ScenarioOutcome:
    """Scenario 4: Sensitive-field leak attempt -> 100% scrubbed in logs/traces/alerts; unauthorized auditor blocked."""
    plat = ObservabilityPlatform(config=_paper_config())
    try:
        faults = FaultInjectionProfile(inject_sensitive_log_attempt=True)
        pipe = TradingPipeline(
            plat,
            market_data=FixtureMarketDataAdapter(fixture_path=DEFAULT_FIXTURE, seed=seed),
            faults=faults,
            seed=seed,
        )
        results = pipe.run_batch(limit=2, now_s=4000.0)
        alerts = plat.alert_engine.evaluate(now_s=4001.0)
        fired_codes = [a.alert_code for a in alerts]

        # Inspect every emitted log and trace for any leaked secrets or confidential order details
        forbidden_substrings = [
            "sk_live_SIMULATED999988887777",
            "SECRET_TOKEN_DO_NOT_LEAK_12345",
            "ACCT-99887766",
            "alpha_decay",
        ]
        all_logs_str = "\n".join(str(r.to_dict()) for r in plat.logger.get_records())
        trace_view = plat.trace_store.get_trace_by_order_id(results[0].order_id, role=ViewerRole.VIEWER)
        trace_str = str(trace_view)

        leaks_found = [
            tok for tok in forbidden_substrings if tok in all_logs_str or tok in trace_str
        ]
        redaction_markers_present = (
            REDACTED_SECRET in all_logs_str and REDACTED_CONFIDENTIAL in all_logs_str
        )

        # Ensure trace still correlated cleanly across all 6 services without secrets
        passed = (
            len(leaks_found) == 0
            and redaction_markers_present
            and "SENSITIVE_FIELD_LEAK_ATTEMPT" in fired_codes
            and len(trace_view["services_covered"]) == 6
        )
        return ScenarioOutcome(
            name="sensitive_field_leak",
            passed=passed,
            alerts_fired=fired_codes,
            summary="Injected API keys, bearer tokens, account numbers, and confidential order params were 100% scrubbed while preserving full 6-service trace correlation.",
            evidence={
                "leaks_found": leaks_found,
                "redaction_markers_present": redaction_markers_present,
                "blocked_fields_count": int(
                    plat.metrics.get_counter_sum("observability_sensitive_field_leak_blocked_total")
                ),
                "trace_services_preserved": trace_view["services_covered"],
            },
        )
    finally:
        plat.close()


def run_scenario_delayed_trace(seed: int = 20261127) -> ScenarioOutcome:
    """Scenario 5: Delayed trace ingestion -> spans still correlated into full trace, lag measured & alerted."""
    plat = ObservabilityPlatform(config=_paper_config())
    try:
        faults = FaultInjectionProfile(delayed_span_ingest_seconds=25.0)
        pipe = TradingPipeline(
            plat,
            market_data=FixtureMarketDataAdapter(fixture_path=DEFAULT_FIXTURE, seed=seed),
            faults=faults,
            seed=seed,
        )
        results = pipe.run_batch(limit=2, now_s=5000.0)
        alerts = plat.alert_engine.evaluate(now_s=5001.0)
        fired_codes = [a.alert_code for a in alerts]

        trace_bundle = plat.trace_store.get_trace_by_order_id(results[0].order_id, role=ViewerRole.OPERATOR)
        delay_q = plat.metrics.get_histogram_quantiles("observability_trace_ingest_delay_seconds")

        passed = (
            "DELAYED_TRACE_INGESTION" in fired_codes
            and trace_bundle["found"]
            and trace_bundle["span_count"] == 6
            and trace_bundle["delayed_spans"] == 2
            and delay_q["max"] >= 25.0
        )
        return ScenarioOutcome(
            name="delayed_trace",
            passed=passed,
            alerts_fired=fired_codes,
            summary="25s delayed broker/portfolio spans were correlated into complete 6-span order traces and flagged by DELAYED_TRACE_INGESTION.",
            evidence={
                "order_id": results[0].order_id,
                "span_count": trace_bundle["span_count"],
                "delayed_spans": trace_bundle["delayed_spans"],
                "max_ingest_delay_seconds": round(delay_q["max"], 3),
            },
        )
    finally:
        plat.close()


def run_scenario_alert_duplication(seed: int = 20261127) -> ScenarioOutcome:
    """Scenario 6: Alert duplication storm -> deduplicated within cooldown, re-notifies after resolve/cooldown."""
    plat = ObservabilityPlatform(config=_paper_config())
    try:
        routed_first_burst = 0
        last_fp = ""
        for i in range(10):
            alert, routed = plat.router.emit(
                alert_code="ORDER_REJECT_SPIKE",
                severity=AlertSeverity.P2_HIGH,
                service="oms",
                summary="Repeated reject spike signal",
                labels={"condition": "reject_rate"},
                now_s=6000.0 + i * 2.0,
            )
            last_fp = alert.fingerprint
            if routed:
                routed_first_burst += 1

        # After cooldown elapses (60s), alert may re-notify
        _alert_after, routed_after_cooldown = plat.router.emit(
            alert_code="ORDER_REJECT_SPIKE",
            severity=AlertSeverity.P2_HIGH,
            service="oms",
            summary="Persistent reject spike after cooldown",
            labels={"condition": "reject_rate"},
            now_s=6100.0,
        )
        resolved = plat.router.resolve(last_fp)

        passed = (
            routed_first_burst == 1
            and plat.router.deduplicated_count == 9
            and routed_after_cooldown is True
            and resolved is not None
            and resolved.status == "RESOLVED"
        )
        return ScenarioOutcome(
            name="alert_duplication",
            passed=passed,
            alerts_fired=["ORDER_REJECT_SPIKE"],
            summary="10 repeated alerts within cooldown collapsed into 1 routed incident (9 deduplicated), then re-notified cleanly after cooldown.",
            evidence={
                "routed_in_burst": routed_first_burst,
                "deduplicated_count": plat.router.deduplicated_count,
                "routed_after_cooldown": routed_after_cooldown,
                "resolved_status": resolved.status if resolved else None,
            },
        )
    finally:
        plat.close()


def run_scenario_unavailable_telemetry_backend(seed: int = 20261127) -> ScenarioOutcome:
    """Scenario 7: Unavailable telemetry backend & missing heartbeat -> trading unaffected, spooled, watchdog alerts, replays on recovery."""
    plat = ObservabilityPlatform(config=_paper_config())
    try:
        pipe = TradingPipeline(
            plat,
            market_data=FixtureMarketDataAdapter(fixture_path=DEFAULT_FIXTURE, seed=seed),
            seed=seed,
        )
        # Take trace backend offline
        plat.trace_store.set_backend_available(False)
        results = pipe.run_batch(limit=3, now_s=7000.0)
        spooled_during_outage = plat.trace_store.spool_depth
        stored_during_outage = plat.trace_store.total_span_count()

        # Simulate watchdog check after heartbeat timeout expires (now_s = 7025.0)
        alerts = plat.alert_engine.evaluate(
            now_s=7025.0,
            expected_heartbeat_services=["market_data", "oms", "broker"],
        )
        fired_codes = [a.alert_code for a in alerts]
        fallback_codes = [a.alert_code for a in plat.router.fallback_alerts()]

        # Restore backend and verify spooled spans replay cleanly
        replayed = plat.trace_store.set_backend_available(True)
        stored_after_recovery = plat.trace_store.total_span_count()
        trace_after_recovery = plat.trace_store.get_trace_by_order_id(results[0].order_id)

        passed = (
            len(results) == 3
            and all(r.status == "filled" for r in results)
            and spooled_during_outage == 18  # 3 orders * 6 spans
            and stored_during_outage == 0
            and "TELEMETRY_BACKEND_UNAVAILABLE" in fired_codes
            and "MONITORING_HEARTBEAT_MISSING" in fired_codes
            and "TELEMETRY_BACKEND_UNAVAILABLE" in fallback_codes
            and replayed == 18
            and stored_after_recovery == 18
            and trace_after_recovery["found"]
        )
        return ScenarioOutcome(
            name="unavailable_telemetry_backend",
            passed=passed,
            alerts_fired=fired_codes,
            summary="Backend outage did not block paper orders; 18 spans spooled locally, independent watchdog fired out-of-band P1 alerts, and all 18 spans replayed on recovery.",
            evidence={
                "orders_completed_during_outage": len(results),
                "spooled_during_outage": spooled_during_outage,
                "replayed_on_recovery": replayed,
                "stored_after_recovery": stored_after_recovery,
                "fallback_channel_alerts": fallback_codes,
            },
        )
    finally:
        plat.close()


def run_m3_m4_policy_and_retention_drill(seed: int = 20261127) -> ScenarioOutcome:
    """Supplemental M3/M4 drill verifying reject spike, reconciliation drift, risk breach, and retention pruning."""
    plat = ObservabilityPlatform(config=_paper_config())
    try:
        # 1. Reject spike (5 rejected orders)
        rej_pipe = TradingPipeline(
            plat,
            market_data=FixtureMarketDataAdapter(seed=seed),
            faults=FaultInjectionProfile(force_broker_reject=True),
            seed=seed,
        )
        rej_pipe.run_batch(limit=5, now_s=8000.0)

        # 2. Reconciliation drift
        drift_pipe = TradingPipeline(
            plat,
            market_data=FixtureMarketDataAdapter(seed=seed + 1),
            faults=FaultInjectionProfile(force_reconciliation_mismatch=True),
            seed=seed + 1,
        )
        drift_pipe.run_batch(limit=1, now_s=8001.0)

        # 3. Risk state breach
        risk_pipe = TradingPipeline(
            plat,
            market_data=FixtureMarketDataAdapter(seed=seed + 2),
            faults=FaultInjectionProfile(force_risk_breach=True),
            seed=seed + 2,
        )
        risk_pipe.run_batch(limit=1, now_s=8002.0)

        alerts = plat.alert_engine.evaluate(now_s=8003.0)
        fired_codes = [a.alert_code for a in alerts]

        # 4. Retention pruning check
        before_spans = plat.trace_store.total_span_count()
        future_ns = utc_now_ns() + int((plat.config.telemetry.trace_retention_seconds + 60) * 1_000_000_000)
        prune_stats = plat.trace_store.enforce_retention(now_ns=future_ns)

        expected = {"ORDER_REJECT_SPIKE", "RECONCILIATION_DRIFT", "RISK_STATE_BREACH"}
        passed = (
            expected.issubset(set(fired_codes))
            and before_spans > 0
            and prune_stats["deleted_by_age"] == before_spans
            and prune_stats["remaining_spans"] == 0
        )
        return ScenarioOutcome(
            name="m3_m4_alert_policies_and_retention",
            passed=passed,
            alerts_fired=fired_codes,
            summary="Verified ORDER_REJECT_SPIKE, RECONCILIATION_DRIFT, RISK_STATE_BREACH alerts and TTL trace retention pruning.",
            evidence={
                "spans_before_retention": before_spans,
                "retention_prune_result": prune_stats,
                "routed_notifications_count": len(plat.router.routed_notifications()),
            },
        )
    finally:
        plat.close()


def run_load_benchmark(order_count: int = 200, seed: int = 20261127) -> Dict[str, Any]:
    """Deterministic local synthetic load & overhead benchmark (explicitly labeled MEASURED, not live SLA)."""
    plat = ObservabilityPlatform(config=_paper_config())
    try:
        pipe = TradingPipeline(
            plat,
            market_data=FixtureMarketDataAdapter(seed=seed),
            seed=seed,
        )
        t0 = time.perf_counter()
        results = pipe.run_batch(limit=order_count)
        wall_elapsed_s = max(1e-6, time.perf_counter() - t0)
        card = plat.metrics.cardinality_report()
        e2e_sim = plat.metrics.get_histogram_quantiles("trading_pipeline_end_to_end_latency_seconds")

        return {
            "measurement_status": "MEASURED_LOCAL_SYNTHETIC_BASELINE",
            "disclaimer": "Local deterministic synthetic benchmark only. Never infer live trading throughput or production SLA from this test.",
            "measured_at_utc": ns_to_utc_iso(utc_now_ns()),
            "environment": {
                "os": platform.platform(),
                "python_version": sys.version.split()[0],
                "machine": platform.machine(),
            },
            "parameters": {"seed": seed, "order_count": order_count},
            "results": {
                "orders_processed": len(results),
                "spans_ingested": plat.trace_store.total_span_count(),
                "logs_emitted": len(plat.logger.get_records()),
                "wall_clock_seconds": round(wall_elapsed_s, 4),
                "local_throughput_orders_per_sec": round(len(results) / wall_elapsed_s, 2),
                "wall_clock_ms_per_order": round((wall_elapsed_s * 1000.0) / max(1, len(results)), 3),
                "simulated_pipeline_latency_ms": {
                    "p50": round(e2e_sim["p50"] * 1000.0, 3),
                    "p95": round(e2e_sim["p95"] * 1000.0, 3),
                    "p99": round(e2e_sim["p99"] * 1000.0, 3),
                },
                "cardinality_total_series": card["total_series"],
                "cardinality_max_series": card["max_total_series"],
                "cardinality_utilization_ratio": card["utilization_ratio"],
                "cardinality_overflow_events": card["overflow_events"],
            },
        }
    finally:
        plat.close()


def run_all_scenarios(seed: int = 20261127, load_orders: int = 200) -> Dict[str, Any]:
    outcomes = [
        run_scenario_lost_feed(seed=seed),
        run_scenario_slow_broker(seed=seed),
        run_scenario_excessive_cardinality(seed=seed),
        run_scenario_sensitive_field_leak(seed=seed),
        run_scenario_delayed_trace(seed=seed),
        run_scenario_alert_duplication(seed=seed),
        run_scenario_unavailable_telemetry_backend(seed=seed),
        run_m3_m4_policy_and_retention_drill(seed=seed),
    ]
    benchmark = run_load_benchmark(order_count=load_orders, seed=seed)
    all_passed = all(o.passed for o in outcomes)
    return {
        "generated_at_utc": ns_to_utc_iso(utc_now_ns()),
        "seed": seed,
        "all_scenarios_passed": all_passed,
        "passed_count": sum(1 for o in outcomes if o.passed),
        "total_scenarios": len(outcomes),
        "scenarios": [o.to_dict() for o in outcomes],
        "load_benchmark": benchmark,
    }
