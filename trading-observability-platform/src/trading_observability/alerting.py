"""Alert policy evaluation, deduplication, severity routing, runbook linking, and independent monitoring watchdog."""

from __future__ import annotations

import hashlib
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from trading_observability.config import AlertingConfig, TelemetryConfig
from trading_observability.metrics import BoundedMetricsRegistry
from trading_observability.redaction import SensitiveFieldScrubber
from trading_observability.schema import AlertEvent, AlertSeverity, ns_to_utc_iso, utc_now_ns
from trading_observability.tracing import TraceStore

RUNBOOK_BASE = "docs/runbooks.md"

RUNBOOK_ANCHORS: Dict[str, str] = {
    "STALE_MARKET_DATA": f"{RUNBOOK_BASE}#stale-market-data",
    "OMS_QUEUE_LAG": f"{RUNBOOK_BASE}#oms-queue-lag",
    "ORDER_REJECT_SPIKE": f"{RUNBOOK_BASE}#order-reject-spike",
    "RECONCILIATION_DRIFT": f"{RUNBOOK_BASE}#reconciliation-drift",
    "RISK_STATE_BREACH": f"{RUNBOOK_BASE}#risk-state-breach",
    "BROKER_LATENCY_DEGRADED": f"{RUNBOOK_BASE}#broker-latency-degraded",
    "CARDINALITY_BUDGET_EXCEEDED": f"{RUNBOOK_BASE}#cardinality-budget-exceeded",
    "SENSITIVE_FIELD_LEAK_ATTEMPT": f"{RUNBOOK_BASE}#sensitive-field-leak-attempt",
    "DELAYED_TRACE_INGESTION": f"{RUNBOOK_BASE}#delayed-trace-ingestion",
    "TELEMETRY_BACKEND_UNAVAILABLE": f"{RUNBOOK_BASE}#telemetry-backend-unavailable",
    "MONITORING_HEARTBEAT_MISSING": f"{RUNBOOK_BASE}#monitoring-heartbeat-missing",
}


def compute_alert_fingerprint(alert_code: str, service: str, labels: Dict[str, str]) -> str:
    canonical = "|".join(
        [alert_code, service] + [f"{k}={v}" for k, v in sorted(labels.items())]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass
class RoutedNotification:
    alert: AlertEvent
    route: str
    delivered_at_utc: str


class AlertRouter:
    """Deduplicating, redacting alert router with cooldown suppression and multi-route delivery."""

    def __init__(
        self,
        config: AlertingConfig,
        metrics: Optional[BoundedMetricsRegistry] = None,
        scrubber: Optional[SensitiveFieldScrubber] = None,
        clock_s: Optional[Callable[[], float]] = None,
    ) -> None:
        self._config = config
        self._metrics = metrics
        self._scrubber = scrubber or SensitiveFieldScrubber()
        self._clock_s = clock_s or (lambda: utc_now_ns() / 1_000_000_000.0)
        self._lock = threading.RLock()
        self._active_by_fingerprint: Dict[str, Tuple[AlertEvent, float]] = {}
        self._routed_notifications: List[RoutedNotification] = []
        self._deduplicated_count = 0
        self._fallback_notifications: List[AlertEvent] = []

    @property
    def deduplicated_count(self) -> int:
        return self._deduplicated_count

    def emit(
        self,
        alert_code: str,
        severity: AlertSeverity,
        service: str,
        summary: str,
        labels: Optional[Dict[str, str]] = None,
        annotations: Optional[Dict[str, Any]] = None,
        now_s: Optional[float] = None,
        use_fallback_channel: bool = False,
    ) -> Tuple[AlertEvent, bool]:
        """Emit an alert; returns (AlertEvent, was_routed). Deduplicates within cooldown window."""
        now = now_s if now_s is not None else self._clock_s()
        clean_labels = {str(k): str(v) for k, v in (labels or {}).items()}
        scrub_summary = self._scrubber.scrub_payload(service, summary, annotations or {})
        fp = compute_alert_fingerprint(alert_code, service, clean_labels)

        with self._lock:
            existing = self._active_by_fingerprint.get(fp)
            if existing is not None:
                prev_alert, last_sent_s = existing
                if (now - last_sent_s) < self._config.dedup_cooldown_seconds:
                    prev_alert.occurrence_count += 1
                    self._deduplicated_count += 1
                    if self._metrics is not None:
                        self._metrics.inc_counter(
                            "trading_alerts_deduplicated_total",
                            1.0,
                            {"severity": severity.value},
                        )
                    return prev_alert, False

            routes = list(self._config.routes.get(severity.value, ("incident_log",)))
            if use_fallback_channel and "out_of_band_watchdog" not in routes:
                routes.append("out_of_band_watchdog")

            ts_utc = ns_to_utc_iso(int(now * 1_000_000_000))
            alert = AlertEvent(
                alert_id=f"ALT-{uuid.uuid4().hex[:10].upper()}",
                alert_code=alert_code,
                fingerprint=fp,
                severity=severity,
                service=service,
                summary=scrub_summary.sanitized_text,
                runbook_url=RUNBOOK_ANCHORS.get(alert_code, RUNBOOK_BASE),
                fired_at_utc=ts_utc,
                status="FIRING",
                occurrence_count=1,
                routes=routes,
                labels=clean_labels,
                annotations=scrub_summary.sanitized_attributes,
            )
            self._active_by_fingerprint[fp] = (alert, now)
            for route in routes:
                self._routed_notifications.append(
                    RoutedNotification(alert=alert, route=route, delivered_at_utc=ts_utc)
                )
            if use_fallback_channel:
                self._fallback_notifications.append(alert)
            if self._metrics is not None:
                self._metrics.inc_counter(
                    "trading_alerts_fired_total",
                    1.0,
                    {"severity": severity.value, "status": "firing"},
                )
            return alert, True

    def resolve(self, fingerprint: str) -> Optional[AlertEvent]:
        with self._lock:
            entry = self._active_by_fingerprint.pop(fingerprint, None)
            if entry is None:
                return None
            alert, _ = entry
            alert.status = "RESOLVED"
            if self._metrics is not None:
                self._metrics.inc_counter(
                    "trading_alerts_fired_total",
                    1.0,
                    {"severity": alert.severity.value, "status": "resolved"},
                )
            return alert

    def active_alerts(self) -> List[AlertEvent]:
        with self._lock:
            return [a for a, _ in self._active_by_fingerprint.values()]

    def routed_notifications(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "route": n.route,
                    "delivered_at_utc": n.delivered_at_utc,
                    "alert": n.alert.to_dict(),
                }
                for n in self._routed_notifications
            ]

    def fallback_alerts(self) -> List[AlertEvent]:
        with self._lock:
            return list(self._fallback_notifications)


class MonitoringWatchdog:
    """Independent dead-man's-switch detecting loss of telemetry ingestion or collector heartbeats."""

    def __init__(
        self,
        telemetry_config: TelemetryConfig,
        router: AlertRouter,
        metrics: Optional[BoundedMetricsRegistry] = None,
        clock_s: Optional[Callable[[], float]] = None,
    ) -> None:
        self._config = telemetry_config
        self._router = router
        self._metrics = metrics
        self._clock_s = clock_s or (lambda: utc_now_ns() / 1_000_000_000.0)
        self._heartbeats: Dict[str, float] = {}
        self._lock = threading.RLock()

    def record_heartbeat(self, service: str, now_s: Optional[float] = None) -> None:
        ts = now_s if now_s is not None else self._clock_s()
        with self._lock:
            self._heartbeats[service] = ts
        if self._metrics is not None:
            self._metrics.set_gauge("observability_watchdog_heartbeat_age_seconds", 0.0, {"service": service})

    def check_health(
        self,
        trace_store: TraceStore,
        expected_services: Optional[List[str]] = None,
        now_s: Optional[float] = None,
    ) -> List[AlertEvent]:
        now = now_s if now_s is not None else self._clock_s()
        fired: List[AlertEvent] = []

        if not trace_store.is_backend_available:
            alert, routed = self._router.emit(
                alert_code="TELEMETRY_BACKEND_UNAVAILABLE",
                severity=AlertSeverity.P1_CRITICAL,
                service="observability",
                summary=(
                    f"Telemetry trace backend unavailable; spool depth={trace_store.spool_depth}, "
                    f"dropped={trace_store.dropped_spool_events}"
                ),
                labels={"backend": "trace_store"},
                annotations={
                    "spool_depth": trace_store.spool_depth,
                    "dropped_spool_events": trace_store.dropped_spool_events,
                },
                now_s=now,
                use_fallback_channel=True,
            )
            if routed:
                fired.append(alert)

        with self._lock:
            services = expected_services or list(self._heartbeats.keys())
            for svc in services:
                last_hb = self._heartbeats.get(svc)
                if last_hb is None:
                    age = self._config.heartbeat_timeout_seconds + 1.0
                else:
                    age = max(0.0, now - last_hb)
                if self._metrics is not None:
                    self._metrics.set_gauge(
                        "observability_watchdog_heartbeat_age_seconds", age, {"service": svc}
                    )
                if age > self._config.heartbeat_timeout_seconds:
                    alert, routed = self._router.emit(
                        alert_code="MONITORING_HEARTBEAT_MISSING",
                        severity=AlertSeverity.P1_CRITICAL,
                        service="observability",
                        summary=f"Dead-man's-switch triggered: no heartbeat from '{svc}' for {age:.2f}s",
                        labels={"monitored_service": svc},
                        annotations={"heartbeat_age_seconds": round(age, 3)},
                        now_s=now,
                        use_fallback_channel=True,
                    )
                    if routed:
                        fired.append(alert)
        return fired


class AlertPolicyEngine:
    """Evaluates trading & observability alert rules against bounded metrics, trace store, and leak counters."""

    def __init__(
        self,
        alert_config: AlertingConfig,
        telemetry_config: TelemetryConfig,
        metrics: BoundedMetricsRegistry,
        trace_store: TraceStore,
        router: AlertRouter,
        watchdog: MonitoringWatchdog,
    ) -> None:
        self._alert_cfg = alert_config
        self._tel_cfg = telemetry_config
        self._metrics = metrics
        self._trace_store = trace_store
        self._router = router
        self._watchdog = watchdog

    def evaluate(
        self,
        now_s: Optional[float] = None,
        expected_heartbeat_services: Optional[List[str]] = None,
    ) -> List[AlertEvent]:
        newly_routed: List[AlertEvent] = []

        # 1. Stale market data or sequence gaps
        max_age = self._metrics.get_gauge_max("trading_market_data_age_seconds")
        seq_gaps = self._metrics.get_counter_sum("trading_market_data_sequence_gaps_total")
        if (max_age is not None and max_age > self._alert_cfg.stale_market_data_seconds) or seq_gaps > 0:
            alert, routed = self._router.emit(
                alert_code="STALE_MARKET_DATA",
                severity=AlertSeverity.P1_CRITICAL,
                service="market_data",
                summary=f"Market data degraded: max_age={max_age or 0.0:.2f}s, seq_gaps={int(seq_gaps)}",
                labels={"condition": "feed_health"},
                annotations={"max_age_seconds": max_age or 0.0, "sequence_gaps": int(seq_gaps)},
                now_s=now_s,
            )
            if routed:
                newly_routed.append(alert)

        # 2. OMS queue lag
        oms_q = self._metrics.get_histogram_quantiles("trading_oms_queue_lag_seconds")
        if oms_q["count"] > 0 and (oms_q["p95"] * 1000.0) > self._alert_cfg.oms_queue_lag_ms:
            alert, routed = self._router.emit(
                alert_code="OMS_QUEUE_LAG",
                severity=AlertSeverity.P2_HIGH,
                service="oms",
                summary=f"OMS queue lag p95={oms_q['p95'] * 1000.0:.1f}ms exceeds {self._alert_cfg.oms_queue_lag_ms:.1f}ms",
                labels={"metric": "trading_oms_queue_lag_seconds"},
                annotations={"p95_ms": round(oms_q["p95"] * 1000.0, 2), "p99_ms": round(oms_q["p99"] * 1000.0, 2)},
                now_s=now_s,
            )
            if routed:
                newly_routed.append(alert)

        # 3. Broker latency degraded
        broker_q = self._metrics.get_histogram_quantiles("trading_broker_ack_latency_seconds")
        if broker_q["count"] > 0 and (broker_q["p95"] * 1000.0) > self._alert_cfg.broker_latency_ms:
            alert, routed = self._router.emit(
                alert_code="BROKER_LATENCY_DEGRADED",
                severity=AlertSeverity.P2_HIGH,
                service="broker",
                summary=(
                    f"Broker ack latency p95={broker_q['p95'] * 1000.0:.1f}ms "
                    f"exceeds {self._alert_cfg.broker_latency_ms:.1f}ms"
                ),
                labels={"metric": "trading_broker_ack_latency_seconds"},
                annotations={"p95_ms": round(broker_q["p95"] * 1000.0, 2), "max_ms": round(broker_q["max"] * 1000.0, 2)},
                now_s=now_s,
            )
            if routed:
                newly_routed.append(alert)

        # 4. Order reject spike
        total_orders = self._metrics.get_counter_sum("trading_orders_total", {"service": "oms"})
        rejected_orders = self._metrics.get_counter_sum(
            "trading_orders_total", {"service": "oms", "status": "rejected"}
        )
        if total_orders >= self._alert_cfg.min_orders_for_reject_rate:
            reject_ratio = rejected_orders / total_orders
            if reject_ratio >= self._alert_cfg.order_reject_rate_ratio:
                alert, routed = self._router.emit(
                    alert_code="ORDER_REJECT_SPIKE",
                    severity=AlertSeverity.P2_HIGH,
                    service="oms",
                    summary=f"Order reject ratio {reject_ratio:.1%} ({int(rejected_orders)}/{int(total_orders)})",
                    labels={"condition": "reject_rate"},
                    annotations={"reject_ratio": round(reject_ratio, 4), "rejected": int(rejected_orders)},
                    now_s=now_s,
                )
                if routed:
                    newly_routed.append(alert)

        # 5. Reconciliation drift
        mismatches = self._metrics.get_counter_sum("trading_reconciliation_mismatches_total")
        if mismatches > 0:
            alert, routed = self._router.emit(
                alert_code="RECONCILIATION_DRIFT",
                severity=AlertSeverity.P1_CRITICAL,
                service="portfolio",
                summary=f"Broker-portfolio reconciliation drift detected: {int(mismatches)} mismatch(es)",
                labels={"condition": "reconciliation_mismatch"},
                annotations={"mismatches": int(mismatches)},
                now_s=now_s,
            )
            if routed:
                newly_routed.append(alert)

        # 6. Risk state breach
        risk_state = self._metrics.get_gauge_max("trading_risk_state_code")
        if risk_state is not None and risk_state >= 1.0:
            sev = AlertSeverity.P1_CRITICAL if risk_state >= 2.0 else AlertSeverity.P2_HIGH
            state_label = "BREACHED_KILL_SWITCH" if risk_state >= 2.0 else "DEGRADED"
            alert, routed = self._router.emit(
                alert_code="RISK_STATE_BREACH",
                severity=sev,
                service="risk",
                summary=f"Risk engine state is {state_label} (code={int(risk_state)})",
                labels={"risk_state": state_label},
                annotations={"risk_state_code": int(risk_state)},
                now_s=now_s,
            )
            if routed:
                newly_routed.append(alert)

        # 7. Cardinality budget pressure or overflow
        card = self._metrics.cardinality_report()
        if (
            card["utilization_ratio"] >= self._alert_cfg.cardinality_utilization_warn_ratio
            or card["overflow_events"] > 0
            or card["forbidden_label_rejections"] > 0
        ):
            alert, routed = self._router.emit(
                alert_code="CARDINALITY_BUDGET_EXCEEDED",
                severity=AlertSeverity.P2_HIGH,
                service="observability",
                summary=(
                    f"Metrics cardinality guard triggered: utilization={card['utilization_ratio']:.1%}, "
                    f"overflows={card['overflow_events']}, forbidden_rejections={card['forbidden_label_rejections']}"
                ),
                labels={"condition": "cardinality_guard"},
                annotations={
                    "total_series": card["total_series"],
                    "max_total_series": card["max_total_series"],
                    "overflow_events": card["overflow_events"],
                    "forbidden_label_rejections": card["forbidden_label_rejections"],
                },
                now_s=now_s,
            )
            if routed:
                newly_routed.append(alert)

        # 8. Sensitive-field leak attempt intercepted
        blocked_leaks = self._metrics.get_counter_sum("observability_sensitive_field_leak_blocked_total")
        if blocked_leaks > 0:
            alert, routed = self._router.emit(
                alert_code="SENSITIVE_FIELD_LEAK_ATTEMPT",
                severity=AlertSeverity.P2_HIGH,
                service="observability",
                summary=f"Redaction firewall intercepted {int(blocked_leaks)} sensitive/confidential field(s)",
                labels={"condition": "redaction_firewall"},
                annotations={"blocked_fields_count": int(blocked_leaks)},
                now_s=now_s,
            )
            if routed:
                newly_routed.append(alert)

        # 9. Delayed trace ingestion
        delay_q = self._metrics.get_histogram_quantiles("observability_trace_ingest_delay_seconds")
        if self._trace_store.delayed_spans_count > 0 or (
            delay_q["count"] > 0 and delay_q["max"] > self._tel_cfg.trace_ingest_delay_threshold_seconds
        ):
            alert, routed = self._router.emit(
                alert_code="DELAYED_TRACE_INGESTION",
                severity=AlertSeverity.P3_WARNING,
                service="observability",
                summary=(
                    f"Delayed trace spans detected: count={self._trace_store.delayed_spans_count}, "
                    f"max_lag={delay_q['max']:.2f}s"
                ),
                labels={"condition": "trace_ingest_lag"},
                annotations={
                    "delayed_spans": self._trace_store.delayed_spans_count,
                    "max_ingest_delay_seconds": round(delay_q["max"], 3),
                },
                now_s=now_s,
            )
            if routed:
                newly_routed.append(alert)

        # 10. Independent watchdog (telemetry backend outage & missing heartbeats)
        watchdog_alerts = self._watchdog.check_health(
            trace_store=self._trace_store,
            expected_services=expected_heartbeat_services,
            now_s=now_s,
        )
        newly_routed.extend(watchdog_alerts)
        return newly_routed
