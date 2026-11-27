"""Versioned configuration loader enforcing platform non-goals and safety invariants."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from trading_mlops.errors import ErrorCode, MlopsError


@dataclass(frozen=True)
class SigningConfig:
    algorithm: str = "HMAC-SHA256"
    key_env_var: str = "TRADING_MLOPS_SIGNING_KEY"
    key_id: str = "local-dev-hmac-v1"

    def resolve_signing_secret(self, fallback_dev_secret: str = "local-sandbox-ephemeral-signing-key") -> bytes:
        """Resolve signing key from environment; never hardcode production secrets."""
        env_val = os.environ.get(self.key_env_var)
        if env_val:
            return env_val.encode("utf-8")
        return fallback_dev_secret.encode("utf-8")


@dataclass(frozen=True)
class LineageConfig:
    min_embargo_seconds: int = 3600
    min_training_span_days: int = 30
    max_suspicious_feature_target_corr: float = 0.985
    require_utc_timestamps: bool = True


@dataclass(frozen=True)
class ValidationGateConfig:
    min_oos_samples: int = 40
    min_net_sharpe: float = 0.25
    min_net_return_bps: float = 0.0
    max_drawdown_pct: float = 0.25
    max_daily_turnover: float = 2.5
    min_hit_rate: float = 0.51
    min_information_coefficient: float = 0.03
    max_subperiod_sharpe_gap: float = 6.0
    transaction_cost_bps: float = 5.0


@dataclass(frozen=True)
class GovernanceConfig:
    forbid_self_approval: bool = True
    stage_required_approvals: Dict[str, List[str]] = field(
        default_factory=lambda: {
            "SHADOW": ["RISK_APPROVER"],
            "PAPER": ["RISK_APPROVER"],
            "LIVE_GATED": ["RISK_APPROVER", "COMPLIANCE_OFFICER"],
        }
    )
    allow_live_execution_in_mvp: bool = False


@dataclass(frozen=True)
class MonitoringConfig:
    psi_warning_threshold: float = 0.15
    psi_critical_threshold: float = 0.25
    mean_shift_z_warning: float = 1.5
    mean_shift_z_critical: float = 2.5
    prediction_mean_shift_warning: float = 1.5
    prediction_mean_shift_critical: float = 2.5
    shadow_divergence_warning: float = 0.20
    shadow_divergence_critical: float = 0.50
    min_monitoring_samples: int = 20


@dataclass(frozen=True)
class PlatformConfig:
    config_version: str = "1.0.0"
    environment: str = "local_sandbox"
    order_execution_enabled: bool = False
    allow_automatic_live_promotion: bool = False
    allow_blind_recent_return_retraining: bool = False
    signing: SigningConfig = field(default_factory=SigningConfig)
    lineage: LineageConfig = field(default_factory=LineageConfig)
    validation_gate: ValidationGateConfig = field(default_factory=ValidationGateConfig)
    governance: GovernanceConfig = field(default_factory=GovernanceConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)

    def __post_init__(self) -> None:
        if self.allow_automatic_live_promotion:
            raise MlopsError(
                ErrorCode.CONFIGURATION_ERROR,
                "Explicit non-goal violated: automatic live model promotion is forbidden.",
                {"allow_automatic_live_promotion": True},
            )
        if self.allow_blind_recent_return_retraining:
            raise MlopsError(
                ErrorCode.CONFIGURATION_ERROR,
                "Explicit non-goal violated: blind retraining on recent returns is forbidden.",
                {"allow_blind_recent_return_retraining": True},
            )
        if not self.governance.forbid_self_approval:
            raise MlopsError(
                ErrorCode.CONFIGURATION_ERROR,
                "Separation of duties cannot be disabled (forbid_self_approval must be true).",
                {"forbid_self_approval": False},
            )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlatformConfig":
        return cls(
            config_version=str(data.get("config_version", "1.0.0")),
            environment=str(data.get("environment", "local_sandbox")),
            order_execution_enabled=bool(data.get("order_execution_enabled", False)),
            allow_automatic_live_promotion=bool(data.get("allow_automatic_live_promotion", False)),
            allow_blind_recent_return_retraining=bool(
                data.get("allow_blind_recent_return_retraining", False)
            ),
            signing=SigningConfig(**data.get("signing", {})),
            lineage=LineageConfig(**data.get("lineage", {})),
            validation_gate=ValidationGateConfig(**data.get("validation_gate", {})),
            governance=GovernanceConfig(**data.get("governance", {})),
            monitoring=MonitoringConfig(**data.get("monitoring", {})),
        )

    @classmethod
    def load(cls, path: Path) -> "PlatformConfig":
        if not path.exists():
            raise MlopsError(
                ErrorCode.CONFIGURATION_ERROR,
                f"Configuration file not found: {path}",
                {"path": str(path)},
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(raw)
