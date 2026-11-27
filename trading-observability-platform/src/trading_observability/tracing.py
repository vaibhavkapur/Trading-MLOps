"""Distributed tracer, SQLite/OTLP durable trace store, delayed-trace detection, retention, and resilient backend spool."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional, Tuple

from trading_observability.config import TelemetryConfig
from trading_observability.metrics import BoundedMetricsRegistry
from trading_observability.redaction import SensitiveFieldScrubber, project_trace_for_role
from trading_observability.schema import CorrelationContext, SpanRecord, ViewerRole, utc_now_ns


class TraceStore:
    """SQLite-backed trace store supporting out-of-order/delayed span correlation, OTLP export, and retention."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        config: Optional[TelemetryConfig] = None,
        metrics: Optional[BoundedMetricsRegistry] = None,
        scrubber: Optional[SensitiveFieldScrubber] = None,
    ) -> None:
        self._config = config or TelemetryConfig()
        self._metrics = metrics
        self._scrubber = scrubber or SensitiveFieldScrubber()
        self._lock = threading.RLock()
        self._db_target = str(db_path) if db_path else ":memory:"
        if db_path:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_target, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        self._backend_available = True
        self._spool: Deque[SpanRecord] = deque(maxlen=self._config.backend_spool_max_events)
        self._dropped_spool_events = 0
        self._replayed_spool_events = 0
        self._delayed_spans_count = 0
        self._last_successful_write_ns: Optional[int] = utc_now_ns()
        if self._metrics is not None:
            self._metrics.set_gauge("observability_backend_healthy", 1.0, {"backend": "trace_store"})
            self._metrics.set_gauge("observability_spool_depth", 0.0, {"backend": "trace_store"})

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS spans (
                    span_id TEXT PRIMARY KEY,
                    trace_id TEXT NOT NULL,
                    parent_span_id TEXT,
                    correlation_id TEXT NOT NULL,
                    service TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    order_id TEXT,
                    feed_batch_id TEXT,
                    start_time_ns INTEGER NOT NULL,
                    end_time_ns INTEGER NOT NULL,
                    ingested_at_ns INTEGER NOT NULL,
                    is_delayed INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attributes_json TEXT NOT NULL,
                    error_json TEXT,
                    schema_version TEXT NOT NULL
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_spans_trace_id ON spans(trace_id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_spans_order_id ON spans(order_id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_spans_end_time ON spans(end_time_ns)")

    @property
    def is_backend_available(self) -> bool:
        return self._backend_available

    @property
    def spool_depth(self) -> int:
        with self._lock:
            return len(self._spool)

    @property
    def dropped_spool_events(self) -> int:
        return self._dropped_spool_events

    @property
    def replayed_spool_events(self) -> int:
        return self._replayed_spool_events

    @property
    def delayed_spans_count(self) -> int:
        return self._delayed_spans_count

    @property
    def last_successful_write_ns(self) -> Optional[int]:
        return self._last_successful_write_ns

    def set_backend_available(self, available: bool) -> int:
        """Toggle backend availability (fault injection / recovery). Replays spooled spans on recovery."""
        with self._lock:
            self._backend_available = available
            if self._metrics is not None:
                self._metrics.set_gauge(
                    "observability_backend_healthy", 1.0 if available else 0.0, {"backend": "trace_store"}
                )
            replayed = 0
            if available and self._spool:
                while self._spool:
                    span = self._spool.popleft()
                    self._persist_span(span, ingested_at_ns=span.ingested_at_ns or utc_now_ns())
                    replayed += 1
                self._replayed_spool_events += replayed
                if self._metrics is not None:
                    self._metrics.set_gauge("observability_spool_depth", 0.0, {"backend": "trace_store"})
            return replayed

    def ingest_span(self, span: SpanRecord, ingested_at_ns: Optional[int] = None) -> bool:
        """Scrub and persist a span. Buffers into bounded spool if backend is unavailable."""
        scrub = self._scrubber.scrub_payload(span.service, "", span.attributes)
        span.attributes = scrub.sanitized_attributes
        ingest_ts = ingested_at_ns if ingested_at_ns is not None else utc_now_ns()
        span.ingested_at_ns = ingest_ts
        delay_s = span.ingest_delay_seconds
        span.is_delayed = delay_s > self._config.trace_ingest_delay_threshold_seconds

        if self._metrics is not None:
            self._metrics.observe_histogram(
                "observability_trace_ingest_delay_seconds", delay_s, {"service": span.service}
            )
        if span.is_delayed:
            self._delayed_spans_count += 1

        with self._lock:
            if not self._backend_available:
                if len(self._spool) >= self._config.backend_spool_max_events:
                    self._dropped_spool_events += 1
                self._spool.append(span)
                if self._metrics is not None:
                    self._metrics.set_gauge(
                        "observability_spool_depth", float(len(self._spool)), {"backend": "trace_store"}
                    )
                return False

            self._persist_span(span, ingested_at_ns=ingest_ts)
            return True

    def _persist_span(self, span: SpanRecord, ingested_at_ns: int) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO spans (
                    span_id, trace_id, parent_span_id, correlation_id, service, operation,
                    order_id, feed_batch_id, start_time_ns, end_time_ns, ingested_at_ns,
                    is_delayed, status, attributes_json, error_json, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    span.span_id,
                    span.trace_id,
                    span.parent_span_id,
                    span.correlation_id,
                    span.service,
                    span.operation,
                    span.order_id,
                    span.feed_batch_id,
                    span.start_time_ns,
                    span.end_time_ns,
                    ingested_at_ns,
                    1 if span.is_delayed else 0,
                    span.status,
                    json.dumps(span.attributes, sort_keys=True),
                    json.dumps(span.error, sort_keys=True) if span.error else None,
                    span.schema_version,
                ),
            )
        self._last_successful_write_ns = utc_now_ns()

    def _row_to_span(self, row: sqlite3.Row) -> SpanRecord:
        return SpanRecord(
            trace_id=row["trace_id"],
            span_id=row["span_id"],
            parent_span_id=row["parent_span_id"],
            correlation_id=row["correlation_id"],
            service=row["service"],
            operation=row["operation"],
            order_id=row["order_id"],
            feed_batch_id=row["feed_batch_id"],
            start_time_ns=int(row["start_time_ns"]),
            end_time_ns=int(row["end_time_ns"]),
            ingested_at_ns=int(row["ingested_at_ns"]),
            is_delayed=bool(row["is_delayed"]),
            status=row["status"],
            attributes=json.loads(row["attributes_json"]),
            error=json.loads(row["error_json"]) if row["error_json"] else None,
            schema_version=row["schema_version"],
        )

    def get_trace(self, trace_id: str) -> List[SpanRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM spans WHERE trace_id = ? ORDER BY start_time_ns ASC", (trace_id,)
            ).fetchall()
            return [self._row_to_span(r) for r in rows]

    def get_trace_by_order_id(
        self,
        order_id: str,
        role: ViewerRole = ViewerRole.VIEWER,
    ) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT trace_id FROM spans WHERE order_id = ? LIMIT 1", (order_id,)
            ).fetchone()
            if not row:
                return {"order_id": order_id, "found": False, "spans": [], "viewer_role": role.value}
            trace_id = row["trace_id"]
            spans = self.get_trace(trace_id)
            services_covered = sorted({s.service for s in spans})
            delayed_count = sum(1 for s in spans if s.is_delayed)
            total_duration_ms = 0.0
            if spans:
                min_start = min(s.start_time_ns for s in spans)
                max_end = max(s.end_time_ns for s in spans)
                total_duration_ms = round((max_end - min_start) / 1_000_000.0, 4)
            bundle = {
                "order_id": order_id,
                "trace_id": trace_id,
                "found": True,
                "span_count": len(spans),
                "services_covered": services_covered,
                "delayed_spans": delayed_count,
                "total_duration_ms": total_duration_ms,
                "spans": [s.to_dict() for s in spans],
            }
            return project_trace_for_role(bundle, role=role, scrubber=self._scrubber)

    def list_recent_traces(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT trace_id,
                       MAX(order_id) AS order_id,
                       COUNT(*) AS span_count,
                       MIN(start_time_ns) AS min_start,
                       MAX(end_time_ns) AS max_end,
                       SUM(is_delayed) AS delayed_count
                FROM spans
                GROUP BY trace_id
                ORDER BY max_end DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                {
                    "trace_id": r["trace_id"],
                    "order_id": r["order_id"],
                    "span_count": int(r["span_count"]),
                    "delayed_spans": int(r["delayed_count"]),
                    "duration_ms": round((int(r["max_end"]) - int(r["min_start"])) / 1_000_000.0, 4),
                }
                for r in rows
            ]

    def total_span_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS cnt FROM spans").fetchone()
            return int(row["cnt"]) if row else 0

    def enforce_retention(
        self,
        now_ns: Optional[int] = None,
        max_age_seconds: Optional[int] = None,
        max_spans: Optional[int] = None,
    ) -> Dict[str, int]:
        """Prune spans older than retention TTL and enforce max span cap."""
        ref_ns = now_ns if now_ns is not None else utc_now_ns()
        ttl_s = max_age_seconds if max_age_seconds is not None else self._config.trace_retention_seconds
        cap = max_spans if max_spans is not None else self._config.trace_max_spans
        cutoff_ns = ref_ns - int(ttl_s * 1_000_000_000)

        with self._lock:
            with self._conn:
                cur_age = self._conn.execute("DELETE FROM spans WHERE end_time_ns < ?", (cutoff_ns,))
                deleted_by_age = cur_age.rowcount

                total_remaining = self.total_span_count()
                deleted_by_cap = 0
                if total_remaining > cap:
                    excess = total_remaining - cap
                    cur_cap = self._conn.execute(
                        """
                        DELETE FROM spans WHERE span_id IN (
                            SELECT span_id FROM spans ORDER BY end_time_ns ASC LIMIT ?
                        )
                        """,
                        (excess,),
                    )
                    deleted_by_cap = cur_cap.rowcount
            return {
                "deleted_by_age": deleted_by_age,
                "deleted_by_cap": deleted_by_cap,
                "remaining_spans": self.total_span_count(),
            }

    def export_otlp_json(self, trace_id: Optional[str] = None) -> Dict[str, Any]:
        """Export spans in OpenTelemetry OTLP/JSON ExportTraceServiceRequest format."""
        with self._lock:
            if trace_id:
                spans = self.get_trace(trace_id)
            else:
                rows = self._conn.execute("SELECT * FROM spans ORDER BY start_time_ns ASC").fetchall()
                spans = [self._row_to_span(r) for r in rows]

        by_service: Dict[str, List[Dict[str, Any]]] = {}
        for sp in spans:
            by_service.setdefault(sp.service, []).append(sp.to_otlp_span())

        resource_spans = []
        for svc, svc_spans in sorted(by_service.items()):
            resource_spans.append(
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": svc}},
                            {
                                "key": "service.namespace",
                                "value": {"stringValue": self._config.service_namespace},
                            },
                            {
                                "key": "telemetry.schema.version",
                                "value": {"stringValue": self._config.schema_version},
                            },
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "trading_observability", "version": "0.1.0"},
                            "spans": svc_spans,
                        }
                    ],
                }
            )
        return {"resourceSpans": resource_spans}

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class Tracer:
    """Ergonomic distributed tracer producing correlated, redacted SpanRecords."""

    def __init__(
        self,
        store: TraceStore,
        clock_ns: Callable[[], int] = utc_now_ns,
    ) -> None:
        self._store = store
        self._clock_ns = clock_ns

    @contextmanager
    def start_span(
        self,
        ctx: CorrelationContext,
        operation: str,
        attributes: Optional[Dict[str, Any]] = None,
        simulated_duration_ms: Optional[float] = None,
        simulated_ingest_delay_s: float = 0.0,
    ) -> Iterator[SpanRecord]:
        start_ns = self._clock_ns()
        span = SpanRecord(
            trace_id=ctx.trace_id,
            span_id=ctx.span_id,
            parent_span_id=ctx.parent_span_id,
            correlation_id=ctx.correlation_id,
            service=ctx.service,
            operation=operation,
            order_id=ctx.order_id,
            feed_batch_id=ctx.feed_batch_id,
            start_time_ns=start_ns,
            end_time_ns=start_ns,
            status="ok",
            attributes=dict(attributes or {}),
        )
        try:
            yield span
        except Exception as exc:
            span.status = "error"
            span.error = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            end_ns = self._clock_ns()
            if simulated_duration_ms is not None:
                end_ns = start_ns + int(simulated_duration_ms * 1_000_000)
            elif end_ns <= start_ns:
                end_ns = start_ns + 1_000  # minimum 1us monotonic resolution
            span.end_time_ns = end_ns
            ingest_ns = end_ns + int(simulated_ingest_delay_s * 1_000_000_000)
            self._store.ingest_span(span, ingested_at_ns=ingest_ns)
