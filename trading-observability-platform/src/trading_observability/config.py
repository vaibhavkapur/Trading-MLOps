"""Versioned configuration loader with fail-safe defaults and zero embedded secrets."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "platform.json"
DEFAULT_SLIS_PATH = Path(__file__).resolve().parents[2] / "config" / "slis.json"


@dataclass(frozen=True)
class ExecutionPolicyConfig:
    mode: str = "DISABLED"
    paper_sandbox_policy_id: str = ""
    paper_sandbox_approved_by: str = ""
    paper_sandbox_review_date_utc: str = ""
    allow_live_execution: bool = False

    def validate_for_paper_sandbox(self) -> None:
        if self.allow_live_execution or self.mode.upper() == "LIVE":
            raise PermissionError(
                "LIVE_EXECUTION_FORBIDDEN: Real-capital execution is strictly prohibited in this platform."
            )
        if self.mode.upper() != "PAPER_SANDBOX":
            raise PermissionError(
                f"EXECUTION_DISABLED: Execution mode is '{self.mode}'. "
                "Explicit PAPER_SANDBOX policy approval is required before submitting paper orders."
            )
        if not self.paper_sandbox_policy_id or not self.paper_sandbox_approved_by:
            raise PermissionError(
                "POLICY_METADATA_MISSING: PAPER_SANDBOX mode requires reviewed policy_id and approver."
            )


@dataclass(frozen=True)
class TelemetryConfig:
    schema_version: str = "1.0.0"
    service_namespace: str = "saat.trading"
    max_series_per_metric: int = 64
    max_total_series: int = 512
    allowed_services: FrozenSet[str] = field(
        default_factory=lambda: frozenset(
            {"market_data", "research", "risk", "oms", "broker", "portfolio", "observability"}
        )
    )
    allowed_venues: FrozenSet[str] = field(
        default_factory=lambda: frozenset({"SYNTH_EXCH_A", "SYNTH_EXCH_B", "PAPER_BROKER_1", "UNKNOWN"})
    )
    allowed_symbols: FrozenSet[str] = field(
        default_factory=lambda: frozenset({"SYN-EQ-01", "SYN-EQ-02", "SYN-EQ-03", "SYN-FX-01", "SYN-FUT-01"})
    )
    allowed_statuses: FrozenSet[str] = field(
        default_factory=lambda: frozenset(
            {
                "ok",
                "accepted",
                "filled",
                "rejected",
                "timeout",
                "stale",
                "degraded",
                "error",
                "breached",
                "reconciled",
                "mismatch",
                "unknown",
            }
        )
    )
    forbidden_metric_labels: FrozenSet[str] = field(
        default_factory=lambda: frozenset(
            {
                "order_id",
                "client_order_id",
                "trace_id",
                "span_id",
                "correlation_id",
                "account_id",
                "client_account",
                "api_key",
                "token",
                "user_id",
                "timestamp",
            }
        )
    )
    trace_retention_seconds: int = 86400
    trace_max_spans: int = 50000
    trace_ingest_delay_threshold_seconds: float = 5.0
    backend_spool_max_events: int = 1000
    heartbeat_timeout_seconds: float = 10.0


@dataclass(frozen=True)
class AlertingConfig:
    dedup_cooldown_seconds: float = 60.0
    stale_market_data_seconds: float = 2.0
    oms_queue_lag_ms: float = 150.0
    broker_latency_ms: float = 250.0
    order_reject_rate_ratio: float = 0.25
    min_orders_for_reject_rate: int = 4
    cardinality_utilization_warn_ratio: float = 0.8
    routes: Dict[str, Tuple[str, ...]] = field(
        default_factory=lambda: {
            "P1_CRITICAL": ("pager_primary", "trading_ops_slack", "incident_log"),
            "P2_HIGH": ("trading_ops_slack", "incident_log"),
            "P3_WARNING": ("observability_digest", "incident_log"),
        }
    )


@dataclass(frozen=True)
class SecurityConfig:
    default_viewer_role: str = "VIEWER"
    require_redaction_in_all_logs: bool = True
    authorized_auditor_tokens_env: str = "TRADING_OBS_AUDITOR_TOKEN"

    def resolve_auditor_token(self) -> Optional[str]:
        val = os.environ.get(self.authorized_auditor_tokens_env, "").strip()
        return val or None


@dataclass(frozen=True)
class PlatformConfig:
    config_version: str
    environment: str
    execution_policy: ExecutionPolicyConfig
    telemetry: TelemetryConfig
    alerting: AlertingConfig
    security: SecurityConfig

    def with_paper_sandbox_enabled(self) -> "PlatformConfig":
        """Return a copy explicitly enabling reviewed PAPER_SANDBOX mode."""
        ep = ExecutionPolicyConfig(
            mode="PAPER_SANDBOX",
            paper_sandbox_policy_id=self.execution_policy.paper_sandbox_policy_id or "POL-PAPER-OBS-001",
            paper_sandbox_approved_by=self.execution_policy.paper_sandbox_approved_by or "trading-engineering-review",
            paper_sandbox_review_date_utc=self.execution_policy.paper_sandbox_review_date_utc or "2026-11-27T00:00:00Z",
            allow_live_execution=False,
        )
        return PlatformConfig(
            config_version=self.config_version,
            environment=self.environment,
            execution_policy=ep,
            telemetry=self.telemetry,
            alerting=self.alerting,
            security=self.security,
        )


def load_platform_config(path: Optional[Path] = None) -> PlatformConfig:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    raw: Dict[str, Any] = json.loads(cfg_path.read_text(encoding="utf-8"))

    ep_raw = raw.get("execution_policy", {})
    ep = ExecutionPolicyConfig(
        mode=str(ep_raw.get("mode", "DISABLED")),
        paper_sandbox_policy_id=str(ep_raw.get("paper_sandbox_policy_id", "")),
        paper_sandbox_approved_by=str(ep_raw.get("paper_sandbox_approved_by", "")),
        paper_sandbox_review_date_utc=str(ep_raw.get("paper_sandbox_review_date_utc", "")),
        allow_live_execution=bool(ep_raw.get("allow_live_execution", False)),
    )

    t_raw = raw.get("telemetry", {})
    tel = TelemetryConfig(
        schema_version=str(t_raw.get("schema_version", "1.0.0")),
        service_namespace=str(t_raw.get("service_namespace", "saat.trading")),
        max_series_per_metric=int(t_raw.get("max_series_per_metric", 64)),
        max_total_series=int(t_raw.get("max_total_series", 512)),
        allowed_services=frozenset(t_raw.get("allowed_services", [])),
        allowed_venues=frozenset(t_raw.get("allowed_venues", [])),
        allowed_symbols=frozenset(t_raw.get("allowed_symbols", [])),
        allowed_statuses=frozenset(t_raw.get("allowed_statuses", [])),
        forbidden_metric_labels=frozenset(t_raw.get("forbidden_metric_labels", [])),
        trace_retention_seconds=int(t_raw.get("trace_retention_seconds", 86400)),
        trace_max_spans=int(t_raw.get("trace_max_spans", 50000)),
        trace_ingest_delay_threshold_seconds=float(t_raw.get("trace_ingest_delay_threshold_seconds", 5.0)),
        backend_spool_max_events=int(t_raw.get("backend_spool_max_events", 1000)),
        heartbeat_timeout_seconds=float(t_raw.get("heartbeat_timeout_seconds", 10.0)),
    )

    a_raw = raw.get("alerting", {})
    routes_raw = a_raw.get("routes", {})
    routes = {str(k): tuple(str(x) for x in v) for k, v in routes_raw.items()}
    al = AlertingConfig(
        dedup_cooldown_seconds=float(a_raw.get("dedup_cooldown_seconds", 60.0)),
        stale_market_data_seconds=float(a_raw.get("stale_market_data_seconds", 2.0)),
        oms_queue_lag_ms=float(a_raw.get("oms_queue_lag_ms", 150.0)),
        broker_latency_ms=float(a_raw.get("broker_latency_ms", 250.0)),
        order_reject_rate_ratio=float(a_raw.get("order_reject_rate_ratio", 0.25)),
        min_orders_for_reject_rate=int(a_raw.get("min_orders_for_reject_rate", 4)),
        cardinality_utilization_warn_ratio=float(a_raw.get("cardinality_utilization_warn_ratio", 0.8)),
        routes=routes,
    )

    s_raw = raw.get("security", {})
    sec = SecurityConfig(
        default_viewer_role=str(s_raw.get("default_viewer_role", "VIEWER")),
        require_redaction_in_all_logs=bool(s_raw.get("require_redaction_in_all_logs", True)),
        authorized_auditor_tokens_env=str(
            s_raw.get("authorized_auditor_tokens_env", "TRADING_OBS_AUDITOR_TOKEN")
        ),
    )

    return PlatformConfig(
        config_version=str(raw.get("config_version", "1.0.0")),
        environment=str(raw.get("environment", "local-paper-sandbox")),
        execution_policy=ep,
        telemetry=tel,
        alerting=al,
        security=sec,
    )


def load_slis_catalog(path: Optional[Path] = None) -> Dict[str, Any]:
    slis_path = Path(path) if path else DEFAULT_SLIS_PATH
    return json.loads(slis_path.read_text(encoding="utf-8"))
