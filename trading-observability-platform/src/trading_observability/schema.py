"""Canonical telemetry schema: correlation IDs, W3C TraceContext, events, spans, logs, alerts, and errors."""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from trading_observability import SCHEMA_VERSION

TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def utc_now_ns() -> int:
    return time.time_ns()


def ns_to_utc_iso(ts_ns: int) -> str:
    dt = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc_iso_to_ns(iso_str: str) -> int:
    s = iso_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"Timestamp must include explicit UTC timezone: {iso_str}")
    dt_utc = dt.astimezone(timezone.utc)
    return int(dt_utc.timestamp() * 1_000_000_000)


def generate_trace_id(seed_material: Optional[str] = None) -> str:
    if seed_material is not None:
        return hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:32]
    return uuid.uuid4().hex


def generate_span_id(seed_material: Optional[str] = None) -> str:
    if seed_material is not None:
        return hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:16]
    return uuid.uuid4().hex[:16]


class ServiceName(str, Enum):
    MARKET_DATA = "market_data"
    RESEARCH = "research"
    RISK = "risk"
    OMS = "oms"
    BROKER = "broker"
    PORTFOLIO = "portfolio"
    OBSERVABILITY = "observability"


class AlertSeverity(str, Enum):
    P1_CRITICAL = "P1_CRITICAL"
    P2_HIGH = "P2_HIGH"
    P3_WARNING = "P3_WARNING"


class ViewerRole(str, Enum):
    VIEWER = "VIEWER"
    OPERATOR = "OPERATOR"
    AUDITOR_AUTHORIZED = "AUDITOR_AUTHORIZED"


@dataclass(frozen=True)
class StructuredError:
    error_code: str
    message: str
    service: str
    retryable: bool = False
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp_utc: str = field(default_factory=lambda: ns_to_utc_iso(utc_now_ns()))

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CorrelationContext:
    """Canonical correlation envelope propagated across all trading services."""

    trace_id: str
    span_id: str
    correlation_id: str
    service: str
    environment: str = "local-paper-sandbox"
    parent_span_id: Optional[str] = None
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None
    feed_batch_id: Optional[str] = None
    strategy_id: Optional[str] = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not TRACE_ID_RE.match(self.trace_id):
            raise ValueError(f"Invalid W3C trace_id (must be 32 lowercase hex chars): {self.trace_id}")
        if not SPAN_ID_RE.match(self.span_id):
            raise ValueError(f"Invalid W3C span_id (must be 16 lowercase hex chars): {self.span_id}")
        if self.parent_span_id is not None and not SPAN_ID_RE.match(self.parent_span_id):
            raise ValueError(f"Invalid W3C parent_span_id: {self.parent_span_id}")
        if not self.correlation_id or not self.correlation_id.strip():
            raise ValueError("correlation_id must be non-empty")

    def to_traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-01"

    def to_headers(self) -> Dict[str, str]:
        headers = {
            "traceparent": self.to_traceparent(),
            "x-correlation-id": self.correlation_id,
            "x-telemetry-schema": self.schema_version,
        }
        if self.order_id:
            headers["x-order-id"] = self.order_id
        if self.feed_batch_id:
            headers["x-feed-batch-id"] = self.feed_batch_id
        if self.strategy_id:
            headers["x-strategy-id"] = self.strategy_id
        return headers

    @classmethod
    def from_headers(
        cls,
        headers: Dict[str, str],
        service: str,
        new_span_id: Optional[str] = None,
        environment: str = "local-paper-sandbox",
    ) -> "CorrelationContext":
        tp = headers.get("traceparent", "")
        match = TRACEPARENT_RE.match(tp)
        if not match:
            raise ValueError(f"Malformed W3C traceparent header: {tp!r}")
        trace_id, parent_span_id, _flags = match.groups()
        correlation_id = headers.get("x-correlation-id") or f"corr-{trace_id[:12]}"
        return cls(
            trace_id=trace_id,
            span_id=new_span_id or generate_span_id(),
            parent_span_id=parent_span_id,
            correlation_id=correlation_id,
            service=service,
            environment=environment,
            order_id=headers.get("x-order-id"),
            feed_batch_id=headers.get("x-feed-batch-id"),
            strategy_id=headers.get("x-strategy-id"),
            schema_version=headers.get("x-telemetry-schema", SCHEMA_VERSION),
        )

    def child(
        self,
        service: str,
        span_id: Optional[str] = None,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
        feed_batch_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
    ) -> "CorrelationContext":
        return CorrelationContext(
            trace_id=self.trace_id,
            span_id=span_id or generate_span_id(),
            parent_span_id=self.span_id,
            correlation_id=self.correlation_id,
            service=service,
            environment=self.environment,
            order_id=order_id if order_id is not None else self.order_id,
            client_order_id=client_order_id if client_order_id is not None else self.client_order_id,
            feed_batch_id=feed_batch_id if feed_batch_id is not None else self.feed_batch_id,
            strategy_id=strategy_id if strategy_id is not None else self.strategy_id,
            schema_version=self.schema_version,
        )


@dataclass
class SpanRecord:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    correlation_id: str
    service: str
    operation: str
    start_time_ns: int
    end_time_ns: int
    status: str = "ok"
    order_id: Optional[str] = None
    feed_batch_id: Optional[str] = None
    attributes: Dict[str, Any] = field(default_factory=dict)
    error: Optional[Dict[str, Any]] = None
    ingested_at_ns: Optional[int] = None
    is_delayed: bool = False
    schema_version: str = SCHEMA_VERSION

    @property
    def duration_ms(self) -> float:
        return max(0.0, (self.end_time_ns - self.start_time_ns) / 1_000_000.0)

    @property
    def ingest_delay_seconds(self) -> float:
        if self.ingested_at_ns is None:
            return 0.0
        return max(0.0, (self.ingested_at_ns - self.end_time_ns) / 1_000_000_000.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "correlation_id": self.correlation_id,
            "service": self.service,
            "operation": self.operation,
            "order_id": self.order_id,
            "feed_batch_id": self.feed_batch_id,
            "start_time_ns": self.start_time_ns,
            "end_time_ns": self.end_time_ns,
            "start_time_utc": ns_to_utc_iso(self.start_time_ns),
            "end_time_utc": ns_to_utc_iso(self.end_time_ns),
            "duration_ms": round(self.duration_ms, 4),
            "ingested_at_ns": self.ingested_at_ns,
            "ingest_delay_seconds": round(self.ingest_delay_seconds, 4),
            "is_delayed": self.is_delayed,
            "status": self.status,
            "attributes": dict(self.attributes),
            "error": self.error,
        }

    def to_otlp_span(self) -> Dict[str, Any]:
        """Export span in OpenTelemetry OTLP/JSON span format."""
        otlp_attrs = [
            {"key": str(k), "value": {"stringValue": str(v)}}
            for k, v in sorted(self.attributes.items())
        ]
        otlp_attrs.append({"key": "correlation_id", "value": {"stringValue": self.correlation_id}})
        if self.order_id:
            otlp_attrs.append({"key": "order_id", "value": {"stringValue": self.order_id}})
        if self.feed_batch_id:
            otlp_attrs.append({"key": "feed_batch_id", "value": {"stringValue": self.feed_batch_id}})
        status_code = 1 if self.status == "ok" else 2
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id or "",
            "name": self.operation,
            "kind": 1,
            "startTimeUnixNano": str(self.start_time_ns),
            "endTimeUnixNano": str(self.end_time_ns),
            "attributes": otlp_attrs,
            "status": {"code": status_code, "message": self.status},
        }


@dataclass
class LogRecord:
    timestamp_utc: str
    level: str
    service: str
    message: str
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    correlation_id: Optional[str] = None
    order_id: Optional[str] = None
    attributes: Dict[str, Any] = field(default_factory=dict)
    redacted_fields: List[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "timestamp_utc": self.timestamp_utc,
            "level": self.level,
            "service": self.service,
            "message": self.message,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "correlation_id": self.correlation_id,
            "order_id": self.order_id,
            "attributes": self.attributes,
            "redacted_fields": self.redacted_fields,
        }


@dataclass
class AlertEvent:
    alert_id: str
    alert_code: str
    fingerprint: str
    severity: AlertSeverity
    service: str
    summary: str
    runbook_url: str
    fired_at_utc: str
    status: str = "FIRING"
    occurrence_count: int = 1
    routes: List[str] = field(default_factory=list)
    labels: Dict[str, str] = field(default_factory=dict)
    annotations: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "alert_id": self.alert_id,
            "alert_code": self.alert_code,
            "fingerprint": self.fingerprint,
            "severity": self.severity.value,
            "service": self.service,
            "summary": self.summary,
            "runbook_url": self.runbook_url,
            "fired_at_utc": self.fired_at_utc,
            "status": self.status,
            "occurrence_count": self.occurrence_count,
            "routes": list(self.routes),
            "labels": dict(self.labels),
            "annotations": dict(self.annotations),
        }
