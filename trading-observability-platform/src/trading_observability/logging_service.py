"""Structured JSON logger with mandatory sensitive-field redaction and correlation IDs."""

from __future__ import annotations

import json
import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from trading_observability.metrics import BoundedMetricsRegistry
from trading_observability.redaction import SensitiveFieldScrubber
from trading_observability.schema import CorrelationContext, LogRecord, ns_to_utc_iso, utc_now_ns


class StructuredLogger:
    """Thread-safe structured JSON logger that scrubs every log line and attribute before emission."""

    def __init__(
        self,
        scrubber: Optional[SensitiveFieldScrubber] = None,
        metrics: Optional[BoundedMetricsRegistry] = None,
        jsonl_path: Optional[Path] = None,
        max_memory_records: int = 5000,
        on_leak_event: Optional[Callable[[str, List[str]], None]] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._metrics = metrics
        self._jsonl_path = Path(jsonl_path) if jsonl_path else None
        if self._jsonl_path:
            self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._records: Deque[LogRecord] = deque(maxlen=max_memory_records)
        self._leak_events: List[Dict[str, Any]] = []
        self._on_leak_event = on_leak_event

        def _handle_leak(service: str, fields: List[str]) -> None:
            if self._metrics is not None:
                self._metrics.inc_counter(
                    "observability_sensitive_field_leak_blocked_total",
                    amount=float(len(fields)),
                    labels={"service": service},
                )
            self._leak_events.append(
                {
                    "timestamp_utc": ns_to_utc_iso(utc_now_ns()),
                    "service": service,
                    "redacted_fields": list(fields),
                }
            )
            if self._on_leak_event is not None:
                self._on_leak_event(service, fields)

        self._scrubber = scrubber or SensitiveFieldScrubber(on_leak_detected=_handle_leak)

    @property
    def scrubber(self) -> SensitiveFieldScrubber:
        return self._scrubber

    def log(
        self,
        level: str,
        service: str,
        message: str,
        ctx: Optional[CorrelationContext] = None,
        attributes: Optional[Dict[str, Any]] = None,
        timestamp_ns: Optional[int] = None,
    ) -> LogRecord:
        scrub = self._scrubber.scrub_payload(service=service, message=message, attributes=attributes)
        record = LogRecord(
            timestamp_utc=ns_to_utc_iso(timestamp_ns if timestamp_ns is not None else utc_now_ns()),
            level=level.upper(),
            service=service,
            message=scrub.sanitized_text,
            trace_id=ctx.trace_id if ctx else None,
            span_id=ctx.span_id if ctx else None,
            correlation_id=ctx.correlation_id if ctx else None,
            order_id=ctx.order_id if ctx else None,
            attributes=scrub.sanitized_attributes,
            redacted_fields=scrub.redacted_fields,
        )
        with self._lock:
            self._records.append(record)
            if self._jsonl_path:
                with self._jsonl_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
        return record

    def info(
        self,
        service: str,
        message: str,
        ctx: Optional[CorrelationContext] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> LogRecord:
        return self.log("INFO", service, message, ctx=ctx, attributes=attributes)

    def warning(
        self,
        service: str,
        message: str,
        ctx: Optional[CorrelationContext] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> LogRecord:
        return self.log("WARNING", service, message, ctx=ctx, attributes=attributes)

    def error(
        self,
        service: str,
        message: str,
        ctx: Optional[CorrelationContext] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> LogRecord:
        return self.log("ERROR", service, message, ctx=ctx, attributes=attributes)

    def get_records(
        self,
        service: Optional[str] = None,
        order_id: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> List[LogRecord]:
        with self._lock:
            out: List[LogRecord] = []
            for rec in self._records:
                if service and rec.service != service:
                    continue
                if order_id and rec.order_id != order_id:
                    continue
                if trace_id and rec.trace_id != trace_id:
                    continue
                out.append(rec)
            return out

    def get_leak_events(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._leak_events)
