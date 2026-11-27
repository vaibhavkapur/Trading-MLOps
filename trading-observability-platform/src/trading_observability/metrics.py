"""Bounded-cardinality Prometheus-compatible metrics registry."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

from trading_observability.config import TelemetryConfig

OVERFLOW_LABEL_VALUE = "__overflow__"
DEFAULT_LATENCY_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class CardinalityPolicyError(ValueError):
    """Raised when a forbidden high-cardinality label key is registered or emitted."""


@dataclass
class MetricDefinition:
    name: str
    metric_type: str  # counter | gauge | histogram
    help_text: str
    unit: str
    label_names: Tuple[str, ...]
    allowed_values: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    max_series: int = 64
    buckets: Tuple[float, ...] = DEFAULT_LATENCY_BUCKETS


@dataclass
class HistogramState:
    bucket_counts: Dict[float, int]
    total_count: int = 0
    total_sum: float = 0.0
    samples: List[float] = field(default_factory=list)


class BoundedMetricsRegistry:
    """Thread-safe metrics registry with strict cardinality enforcement and Prometheus exposition."""

    def __init__(self, config: TelemetryConfig) -> None:
        self._config = config
        self._lock = threading.RLock()
        self._definitions: Dict[str, MetricDefinition] = {}
        self._counters: Dict[str, Dict[Tuple[Tuple[str, str], ...], float]] = {}
        self._gauges: Dict[str, Dict[Tuple[Tuple[str, str], ...], float]] = {}
        self._histograms: Dict[str, Dict[Tuple[Tuple[str, str], ...], HistogramState]] = {}
        self._overflow_events: int = 0
        self._forbidden_label_rejections: int = 0
        self._register_meta_metrics()

    def _register_meta_metrics(self) -> None:
        self.register_counter(
            name="observability_cardinality_overflow_total",
            help_text="Total label values folded into __overflow__ due to cardinality bounds.",
            unit="events",
            label_names=("metric", "reason"),
            max_series=64,
        )
        self.register_counter(
            name="observability_forbidden_label_rejected_total",
            help_text="Total metric emissions rejected for using forbidden high-cardinality label keys.",
            unit="events",
            label_names=("metric",),
            max_series=64,
        )
        self.register_gauge(
            name="observability_active_series_total",
            help_text="Current total number of active time series in the bounded registry.",
            unit="series",
            label_names=(),
            max_series=1,
        )
        self.register_counter(
            name="observability_sensitive_field_leak_blocked_total",
            help_text="Count of sensitive or confidential fields scrubbed before telemetry emission.",
            unit="fields",
            label_names=("service",),
            allowed_values={"service": self._config.allowed_services},
            max_series=16,
        )
        self.register_histogram(
            name="observability_trace_ingest_delay_seconds",
            help_text="Delay between span end timestamp and trace store ingestion timestamp.",
            unit="seconds",
            label_names=("service",),
            allowed_values={"service": self._config.allowed_services},
            max_series=16,
        )
        self.register_gauge(
            name="observability_backend_healthy",
            help_text="1 if telemetry backend is healthy, 0 if unavailable.",
            unit="boolean",
            label_names=("backend",),
            max_series=8,
        )
        self.register_gauge(
            name="observability_spool_depth",
            help_text="Number of telemetry events buffered in local resilient spool.",
            unit="events",
            label_names=("backend",),
            max_series=8,
        )
        self.register_gauge(
            name="observability_watchdog_heartbeat_age_seconds",
            help_text="Age in seconds since last collector heartbeat observed by watchdog.",
            unit="seconds",
            label_names=("service",),
            allowed_values={"service": self._config.allowed_services},
            max_series=16,
        )

    def _validate_label_keys(self, metric_name: str, label_names: Iterable[str]) -> Tuple[str, ...]:
        keys = tuple(sorted(set(label_names)))
        for k in keys:
            if k.lower() in self._config.forbidden_metric_labels:
                raise CardinalityPolicyError(
                    f"Forbidden high-cardinality metric label '{k}' on metric '{metric_name}'"
                )
        return keys

    def register_counter(
        self,
        name: str,
        help_text: str,
        unit: str = "count",
        label_names: Iterable[str] = (),
        allowed_values: Optional[Dict[str, FrozenSet[str]]] = None,
        max_series: Optional[int] = None,
    ) -> None:
        with self._lock:
            keys = self._validate_label_keys(name, label_names)
            self._definitions[name] = MetricDefinition(
                name=name,
                metric_type="counter",
                help_text=help_text,
                unit=unit,
                label_names=keys,
                allowed_values=dict(allowed_values or {}),
                max_series=min(max_series or self._config.max_series_per_metric, self._config.max_series_per_metric),
            )
            self._counters.setdefault(name, {})

    def register_gauge(
        self,
        name: str,
        help_text: str,
        unit: str = "value",
        label_names: Iterable[str] = (),
        allowed_values: Optional[Dict[str, FrozenSet[str]]] = None,
        max_series: Optional[int] = None,
    ) -> None:
        with self._lock:
            keys = self._validate_label_keys(name, label_names)
            self._definitions[name] = MetricDefinition(
                name=name,
                metric_type="gauge",
                help_text=help_text,
                unit=unit,
                label_names=keys,
                allowed_values=dict(allowed_values or {}),
                max_series=min(max_series or self._config.max_series_per_metric, self._config.max_series_per_metric),
            )
            self._gauges.setdefault(name, {})

    def register_histogram(
        self,
        name: str,
        help_text: str,
        unit: str = "seconds",
        label_names: Iterable[str] = (),
        allowed_values: Optional[Dict[str, FrozenSet[str]]] = None,
        max_series: Optional[int] = None,
        buckets: Tuple[float, ...] = DEFAULT_LATENCY_BUCKETS,
    ) -> None:
        with self._lock:
            keys = self._validate_label_keys(name, label_names)
            self._definitions[name] = MetricDefinition(
                name=name,
                metric_type="histogram",
                help_text=help_text,
                unit=unit,
                label_names=keys,
                allowed_values=dict(allowed_values or {}),
                max_series=min(max_series or self._config.max_series_per_metric, self._config.max_series_per_metric),
                buckets=tuple(sorted(buckets)),
            )
            self._histograms.setdefault(name, {})

    def total_series_count(self) -> int:
        with self._lock:
            return (
                sum(len(s) for s in self._counters.values())
                + sum(len(s) for s in self._gauges.values())
                + sum(len(s) for s in self._histograms.values())
            )

    def cardinality_utilization(self) -> float:
        return self.total_series_count() / max(1, self._config.max_total_series)

    def _record_overflow(self, metric_name: str, reason: str) -> None:
        self._overflow_events += 1
        if metric_name == "observability_cardinality_overflow_total":
            return
        store = self._counters.setdefault("observability_cardinality_overflow_total", {})
        key = (("metric", metric_name), ("reason", reason))
        if key not in store and len(store) >= 64:
            key = (("metric", OVERFLOW_LABEL_VALUE), ("reason", reason))
        store[key] = store.get(key, 0.0) + 1.0

    def _normalize_labels(
        self,
        defn: MetricDefinition,
        labels: Optional[Dict[str, str]],
        existing_series: Dict[Tuple[Tuple[str, str], ...], object],
    ) -> Tuple[Tuple[str, str], ...]:
        raw = labels or {}
        # Reject forbidden label keys at runtime as defense-in-depth
        for k in raw:
            if k.lower() in self._config.forbidden_metric_labels:
                self._forbidden_label_rejections += 1
                rej_store = self._counters.setdefault("observability_forbidden_label_rejected_total", {})
                rkey = (("metric", defn.name),)
                if rkey not in rej_store and len(rej_store) >= 64:
                    rkey = (("metric", OVERFLOW_LABEL_VALUE),)
                rej_store[rkey] = rej_store.get(rkey, 0.0) + 1.0
                raise CardinalityPolicyError(
                    f"Runtime emission rejected: forbidden high-cardinality label '{k}' on metric '{defn.name}'"
                )

        normalized: List[Tuple[str, str]] = []
        overflowed_allowlist = False
        for key in defn.label_names:
            val = str(raw.get(key, "unknown"))
            allowed = defn.allowed_values.get(key)
            if allowed and val not in allowed:
                val = OVERFLOW_LABEL_VALUE
                overflowed_allowlist = True
            normalized.append((key, val))

        label_tuple = tuple(normalized)
        if overflowed_allowlist:
            self._record_overflow(defn.name, "allowlist_violation")

        if label_tuple in existing_series:
            return label_tuple

        # Enforce per-metric and global series cap by folding into __overflow__
        if (
            len(existing_series) >= defn.max_series
            or self.total_series_count() >= self._config.max_total_series
        ):
            overflow_tuple = tuple((k, OVERFLOW_LABEL_VALUE) for k in defn.label_names)
            self._record_overflow(defn.name, "series_cap_reached")
            return overflow_tuple

        return label_tuple

    def _update_active_series_gauge(self) -> None:
        self._gauges["observability_active_series_total"][()] = float(self.total_series_count())

    def inc_counter(self, name: str, amount: float = 1.0, labels: Optional[Dict[str, str]] = None) -> None:
        if amount < 0:
            raise ValueError("Counter increment must be non-negative")
        with self._lock:
            defn = self._definitions[name]
            if defn.metric_type != "counter":
                raise TypeError(f"Metric '{name}' is not a counter")
            store = self._counters[name]
            key = self._normalize_labels(defn, labels, store)
            store[key] = store.get(key, 0.0) + amount
            self._update_active_series_gauge()

    def set_gauge(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        with self._lock:
            defn = self._definitions[name]
            if defn.metric_type != "gauge":
                raise TypeError(f"Metric '{name}' is not a gauge")
            store = self._gauges[name]
            key = self._normalize_labels(defn, labels, store)
            store[key] = float(value)
            self._update_active_series_gauge()

    def observe_histogram(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        with self._lock:
            defn = self._definitions[name]
            if defn.metric_type != "histogram":
                raise TypeError(f"Metric '{name}' is not a histogram")
            store = self._histograms[name]
            key = self._normalize_labels(defn, labels, store)
            state = store.get(key)
            if state is None:
                state = HistogramState(bucket_counts={b: 0 for b in defn.buckets})
                store[key] = state
            state.total_count += 1
            state.total_sum += float(value)
            # Keep bounded recent sample window for exact local percentile reporting
            if len(state.samples) >= 5000:
                state.samples.pop(0)
            state.samples.append(float(value))
            for b in defn.buckets:
                if value <= b:
                    state.bucket_counts[b] += 1
            self._update_active_series_gauge()

    def get_counter_sum(self, name: str, label_filter: Optional[Dict[str, str]] = None) -> float:
        with self._lock:
            store = self._counters.get(name, {})
            total = 0.0
            for k, v in store.items():
                if self._matches(k, label_filter):
                    total += v
            return total

    def get_gauge_max(self, name: str, label_filter: Optional[Dict[str, str]] = None) -> Optional[float]:
        with self._lock:
            store = self._gauges.get(name, {})
            vals = [v for k, v in store.items() if self._matches(k, label_filter)]
            return max(vals) if vals else None

    def get_gauge_min(self, name: str, label_filter: Optional[Dict[str, str]] = None) -> Optional[float]:
        with self._lock:
            store = self._gauges.get(name, {})
            vals = [v for k, v in store.items() if self._matches(k, label_filter)]
            return min(vals) if vals else None

    def get_histogram_quantiles(
        self, name: str, label_filter: Optional[Dict[str, str]] = None
    ) -> Dict[str, float]:
        with self._lock:
            store = self._histograms.get(name, {})
            samples: List[float] = []
            for k, st in store.items():
                if self._matches(k, label_filter):
                    samples.extend(st.samples)
            if not samples:
                return {"count": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
            ordered = sorted(samples)
            return {
                "count": float(len(ordered)),
                "p50": _percentile(ordered, 0.50),
                "p95": _percentile(ordered, 0.95),
                "p99": _percentile(ordered, 0.99),
                "max": ordered[-1],
            }

    @staticmethod
    def _matches(series_key: Tuple[Tuple[str, str], ...], label_filter: Optional[Dict[str, str]]) -> bool:
        if not label_filter:
            return True
        mapping = dict(series_key)
        return all(mapping.get(k) == v for k, v in label_filter.items())

    def cardinality_report(self) -> Dict[str, object]:
        with self._lock:
            per_metric: Dict[str, int] = {}
            for name in self._definitions:
                if name in self._counters:
                    per_metric[name] = len(self._counters[name])
                elif name in self._gauges:
                    per_metric[name] = len(self._gauges[name])
                elif name in self._histograms:
                    per_metric[name] = len(self._histograms[name])
            total = self.total_series_count()
            return {
                "total_series": total,
                "max_total_series": self._config.max_total_series,
                "max_series_per_metric": self._config.max_series_per_metric,
                "utilization_ratio": round(total / max(1, self._config.max_total_series), 4),
                "overflow_events": self._overflow_events,
                "forbidden_label_rejections": self._forbidden_label_rejections,
                "per_metric_series": per_metric,
            }

    def export_prometheus(self) -> str:
        """Render all registered metrics in Prometheus text exposition format (0.0.4)."""
        with self._lock:
            lines: List[str] = []
            for name in sorted(self._definitions):
                defn = self._definitions[name]
                lines.append(f"# HELP {name} {defn.help_text}")
                lines.append(f"# TYPE {name} {defn.metric_type}")
                if defn.metric_type == "counter":
                    for k, val in sorted(self._counters[name].items()):
                        lines.append(f"{name}{_format_labels(k)} {val}")
                elif defn.metric_type == "gauge":
                    for k, val in sorted(self._gauges[name].items()):
                        lines.append(f"{name}{_format_labels(k)} {val}")
                elif defn.metric_type == "histogram":
                    for k, state in sorted(self._histograms[name].items()):
                        for b in defn.buckets:
                            bk = k + (("le", str(b)),)
                            lines.append(f"{name}_bucket{_format_labels(bk)} {state.bucket_counts[b]}")
                        inf_k = k + (("le", "+Inf"),)
                        lines.append(f"{name}_bucket{_format_labels(inf_k)} {state.total_count}")
                        lines.append(f"{name}_sum{_format_labels(k)} {state.total_sum}")
                        lines.append(f"{name}_count{_format_labels(k)} {state.total_count}")
            return "\n".join(lines) + "\n"

    def export_json(self) -> Dict[str, object]:
        with self._lock:
            return {
                "cardinality": self.cardinality_report(),
                "counters": {
                    name: {str(dict(k)): v for k, v in store.items()}
                    for name, store in self._counters.items()
                },
                "gauges": {
                    name: {str(dict(k)): v for k, v in store.items()}
                    for name, store in self._gauges.items()
                },
                "histograms": {
                    name: self.get_histogram_quantiles(name)
                    for name in self._histograms
                },
            }


def _format_labels(pairs: Tuple[Tuple[str, str], ...]) -> str:
    if not pairs:
        return ""
    inner = ",".join(f'{k}="{_escape_prom_val(v)}"' for k, v in pairs)
    return "{" + inner + "}"


def _escape_prom_val(val: str) -> str:
    return val.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _percentile(ordered: List[float], q: float) -> float:
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    idx = (len(ordered) - 1) * q
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (idx - lo)


def register_trading_metrics(registry: BoundedMetricsRegistry, config: TelemetryConfig) -> None:
    """Register canonical trading domain metrics with bounded label allowlists."""
    registry.register_counter(
        name="trading_market_data_ticks_total",
        help_text="Total market data ticks processed by venue and status.",
        unit="ticks",
        label_names=("venue", "symbol", "status"),
        allowed_values={
            "venue": config.allowed_venues,
            "symbol": config.allowed_symbols,
            "status": config.allowed_statuses,
        },
        max_series=config.max_series_per_metric,
    )
    registry.register_gauge(
        name="trading_market_data_age_seconds",
        help_text="Age in seconds of most recent market data tick per venue.",
        unit="seconds",
        label_names=("venue",),
        allowed_values={"venue": config.allowed_venues},
        max_series=16,
    )
    registry.register_counter(
        name="trading_market_data_sequence_gaps_total",
        help_text="Detected market data sequence gaps per venue.",
        unit="gaps",
        label_names=("venue",),
        allowed_values={"venue": config.allowed_venues},
        max_series=16,
    )
    registry.register_counter(
        name="trading_orders_total",
        help_text="Total paper orders processed by service, symbol, and terminal status.",
        unit="orders",
        label_names=("service", "symbol", "status"),
        allowed_values={
            "service": config.allowed_services,
            "symbol": config.allowed_symbols,
            "status": config.allowed_statuses,
        },
        max_series=config.max_series_per_metric,
    )
    registry.register_histogram(
        name="trading_oms_queue_lag_seconds",
        help_text="Order wait time in OMS queue before dispatch.",
        unit="seconds",
        label_names=("service",),
        allowed_values={"service": config.allowed_services},
        max_series=16,
    )
    registry.register_histogram(
        name="trading_broker_ack_latency_seconds",
        help_text="Paper broker acknowledgment and fill latency.",
        unit="seconds",
        label_names=("venue", "status"),
        allowed_values={"venue": config.allowed_venues, "status": config.allowed_statuses},
        max_series=32,
    )
    registry.register_histogram(
        name="trading_pipeline_end_to_end_latency_seconds",
        help_text="End-to-end paper trade pipeline latency from tick to portfolio update.",
        unit="seconds",
        label_names=("status",),
        allowed_values={"status": config.allowed_statuses},
        max_series=16,
    )
    registry.register_counter(
        name="trading_reconciliation_mismatches_total",
        help_text="Total reconciliation mismatches detected between broker and portfolio state.",
        unit="mismatches",
        label_names=("venue", "symbol"),
        allowed_values={"venue": config.allowed_venues, "symbol": config.allowed_symbols},
        max_series=32,
    )
    registry.register_gauge(
        name="trading_risk_state_code",
        help_text="Risk state indicator: 0=HEALTHY, 1=DEGRADED, 2=BREACHED/KILL_SWITCH.",
        unit="enum",
        label_names=("service",),
        allowed_values={"service": config.allowed_services},
        max_series=16,
    )
    registry.register_counter(
        name="trading_alerts_fired_total",
        help_text="Total alerts fired by severity and status.",
        unit="alerts",
        label_names=("severity", "status"),
        max_series=32,
    )
    registry.register_counter(
        name="trading_alerts_deduplicated_total",
        help_text="Total duplicate alert evaluations suppressed within cooldown window.",
        unit="alerts",
        label_names=("severity",),
        max_series=16,
    )
