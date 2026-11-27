"""Canonical domain contracts, UTC timestamp parsing, SemVer checks, and deterministic hashing."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from trading_mlops.errors import (
    ContractError,
    ErrorCode,
    FeatureCompatibilityError,
    TimestampContractError,
)

SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$")
FEATURE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format with explicit Z suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc_timestamp(value: str, field_name: str = "timestamp") -> datetime:
    """Parse an ISO-8601 UTC timestamp, rejecting naive or non-UTC timestamps."""
    if not isinstance(value, str) or not value.strip():
        raise TimestampContractError(
            ErrorCode.TIMESTAMP_CONTRACT_ERROR,
            f"Field '{field_name}' must be a non-empty ISO-8601 UTC string.",
            {"field": field_name, "value": value},
        )
    raw = value.strip()
    if raw.endswith("Z"):
        normalized = raw[:-1] + "+00:00"
    else:
        normalized = raw
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise TimestampContractError(
            ErrorCode.TIMESTAMP_CONTRACT_ERROR,
            f"Field '{field_name}' is not a valid ISO-8601 timestamp: {value!r}",
            {"field": field_name, "value": value},
        ) from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise TimestampContractError(
            ErrorCode.TIMESTAMP_CONTRACT_ERROR,
            f"Field '{field_name}' must be timezone-aware UTC (naive timestamp rejected).",
            {"field": field_name, "value": value},
        )
    if dt.utcoffset().total_seconds() != 0:
        raise TimestampContractError(
            ErrorCode.TIMESTAMP_CONTRACT_ERROR,
            f"Field '{field_name}' must be in UTC offset +00:00 / Z.",
            {"field": field_name, "value": value, "offset_seconds": dt.utcoffset().total_seconds()},
        )
    return dt.astimezone(timezone.utc)


def parse_semver(version: str, field_name: str = "version") -> Tuple[int, int, int]:
    """Parse strict MAJOR.MINOR.PATCH semantic version."""
    if not isinstance(version, str) or not SEMVER_RE.match(version):
        raise ContractError(
            ErrorCode.INVALID_CONTRACT,
            f"Field '{field_name}' must be strict SemVer MAJOR.MINOR.PATCH, got {version!r}.",
            {"field": field_name, "value": version},
        )
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def validate_safe_id(value: str, field_name: str) -> str:
    """Validate stable identifier without path traversal or shell characters."""
    if not isinstance(value, str) or not SAFE_ID_RE.match(value) or ".." in value:
        raise ContractError(
            ErrorCode.INVALID_CONTRACT,
            f"Field '{field_name}' must be a valid safe identifier, got {value!r}.",
            {"field": field_name, "value": value},
        )
    return value


def canonical_json_bytes(payload: Any) -> bytes:
    """Deterministic UTF-8 JSON serialization with sorted keys and compact separators."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def sha256_canonical(payload: Any) -> str:
    """Return SHA-256 hex digest of canonical JSON representation."""
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Return SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


class ActorRole(str, Enum):
    """Separated governance roles with least-privilege permissions."""

    RESEARCHER = "RESEARCHER"
    VALIDATOR = "VALIDATOR"
    RISK_APPROVER = "RISK_APPROVER"
    RELEASE_OPERATOR = "RELEASE_OPERATOR"
    COMPLIANCE_OFFICER = "COMPLIANCE_OFFICER"
    GOVERNANCE_AUDITOR = "GOVERNANCE_AUDITOR"


class ModelStage(str, Enum):
    """Controlled deployment lifecycle stages."""

    REGISTERED = "REGISTERED"
    VALIDATED = "VALIDATED"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE_GATED = "LIVE_GATED"
    ROLLED_BACK = "ROLLED_BACK"
    REJECTED = "REJECTED"
    ARCHIVED = "ARCHIVED"


class RunStatus(str, Enum):
    """Experiment run status (both failed and succeeded runs are retained)."""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REJECTED_LEAKAGE = "REJECTED_LEAKAGE"
    REJECTED_POLICY = "REJECTED_POLICY"


class AlertSeverity(str, Enum):
    """Monitoring and governance alert severity levels."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class Actor:
    """Authenticated principal executing a platform action."""

    actor_id: str
    role: ActorRole
    team: str = "trading-engineering"

    def __post_init__(self) -> None:
        validate_safe_id(self.actor_id, "actor_id")
        if not isinstance(self.role, ActorRole):
            raise ContractError(
                ErrorCode.INVALID_CONTRACT,
                f"Invalid actor role: {self.role!r}",
                {"role": str(self.role)},
            )

    def to_dict(self) -> Dict[str, Any]:
        return {"actor_id": self.actor_id, "role": self.role.value, "team": self.team}


@dataclass(frozen=True)
class FeatureSpec:
    """Single feature contract compatible with Project #18 (Financial Feature Store)."""

    name: str
    version: str
    dtype: str
    unit: str
    currency: Optional[str] = None
    transform_hash: str = "deterministic-v1"
    min_bound: Optional[float] = None
    max_bound: Optional[float] = None

    def __post_init__(self) -> None:
        if not FEATURE_NAME_RE.match(self.name):
            raise ContractError(
                ErrorCode.INVALID_CONTRACT,
                f"Invalid feature name {self.name!r}; expected ^[a-z][a-z0-9_]{{1,63}}$.",
                {"feature_name": self.name},
            )
        parse_semver(self.version, f"feature.{self.name}.version")
        if self.dtype not in {"float64", "int64", "bool"}:
            raise ContractError(
                ErrorCode.INVALID_CONTRACT,
                f"Unsupported feature dtype {self.dtype!r}.",
                {"feature_name": self.name, "dtype": self.dtype},
            )

    @property
    def version_id(self) -> str:
        """Content-addressed feature ID matching Financial Feature Store convention."""
        digest = sha256_canonical(
            {
                "name": self.name,
                "version": self.version,
                "dtype": self.dtype,
                "unit": self.unit,
                "currency": self.currency,
                "transform_hash": self.transform_hash,
            }
        )[:16]
        return f"{self.name}@{self.version}+{digest}"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["version_id"] = self.version_id
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FeatureSpec":
        return cls(
            name=data["name"],
            version=data["version"],
            dtype=data["dtype"],
            unit=data["unit"],
            currency=data.get("currency"),
            transform_hash=data.get("transform_hash", "deterministic-v1"),
            min_bound=data.get("min_bound"),
            max_bound=data.get("max_bound"),
        )


@dataclass(frozen=True)
class FeatureSetContract:
    """Versioned collection of features bound to training and serving."""

    feature_set_id: str
    version: str
    owner: str
    features: Tuple[FeatureSpec, ...]
    description: str = ""

    def __post_init__(self) -> None:
        validate_safe_id(self.feature_set_id, "feature_set_id")
        parse_semver(self.version, "feature_set.version")
        if not self.features:
            raise ContractError(
                ErrorCode.INVALID_CONTRACT,
                "FeatureSetContract must contain at least one feature.",
                {"feature_set_id": self.feature_set_id},
            )
        names = [f.name for f in self.features]
        if len(names) != len(set(names)):
            raise ContractError(
                ErrorCode.INVALID_CONTRACT,
                "FeatureSetContract contains duplicate feature names.",
                {"feature_set_id": self.feature_set_id, "names": names},
            )

    @property
    def schema_hash(self) -> str:
        return sha256_canonical(
            {
                "feature_set_id": self.feature_set_id,
                "version": self.version,
                "features": [f.to_dict() for f in self.features],
            }
        )

    @property
    def version_id(self) -> str:
        return f"{self.feature_set_id}@{self.version}+{self.schema_hash[:16]}"

    def feature_names(self) -> List[str]:
        return [f.name for f in self.features]

    def check_serving_compatibility(self, serving_contract: "FeatureSetContract") -> None:
        """Ensure model feature set is strictly compatible with serving environment contract."""
        if self.feature_set_id != serving_contract.feature_set_id:
            raise FeatureCompatibilityError(
                ErrorCode.INCOMPATIBLE_FEATURE_VERSION,
                f"Feature set mismatch: model expects '{self.feature_set_id}', serving provides '{serving_contract.feature_set_id}'.",
                {
                    "model_feature_set": self.version_id,
                    "serving_feature_set": serving_contract.version_id,
                },
            )
        model_major, model_minor, _ = parse_semver(self.version)
        srv_major, srv_minor, _ = parse_semver(serving_contract.version)
        if model_major != srv_major:
            raise FeatureCompatibilityError(
                ErrorCode.INCOMPATIBLE_FEATURE_VERSION,
                f"Breaking MAJOR feature set mismatch: model v{self.version} vs serving v{serving_contract.version}.",
                {
                    "model_version": self.version,
                    "serving_version": serving_contract.version,
                    "model_version_id": self.version_id,
                    "serving_version_id": serving_contract.version_id,
                },
            )
        if srv_minor < model_minor:
            raise FeatureCompatibilityError(
                ErrorCode.INCOMPATIBLE_FEATURE_VERSION,
                f"Serving feature set minor version v{serving_contract.version} is older than model requirement v{self.version}.",
                {
                    "model_version": self.version,
                    "serving_version": serving_contract.version,
                },
            )
        serving_by_name = {f.name: f for f in serving_contract.features}
        for spec in self.features:
            srv_spec = serving_by_name.get(spec.name)
            if srv_spec is None:
                raise FeatureCompatibilityError(
                    ErrorCode.INCOMPATIBLE_FEATURE_VERSION,
                    f"Required feature '{spec.name}' is missing in serving contract '{serving_contract.version_id}'.",
                    {"missing_feature": spec.name, "serving_version_id": serving_contract.version_id},
                )
            f_m_maj, f_m_min, _ = parse_semver(spec.version)
            f_s_maj, f_s_min, _ = parse_semver(srv_spec.version)
            if f_m_maj != f_s_maj or f_s_min < f_m_min:
                raise FeatureCompatibilityError(
                    ErrorCode.INCOMPATIBLE_FEATURE_VERSION,
                    f"Feature '{spec.name}' version incompatible: model requires {spec.version}, serving provides {srv_spec.version}.",
                    {
                        "feature": spec.name,
                        "model_feature_version": spec.version,
                        "serving_feature_version": srv_spec.version,
                    },
                )
            if spec.dtype != srv_spec.dtype or spec.unit != srv_spec.unit or spec.currency != srv_spec.currency:
                raise FeatureCompatibilityError(
                    ErrorCode.INCOMPATIBLE_FEATURE_VERSION,
                    f"Feature '{spec.name}' dtype/unit/currency mismatch between model and serving.",
                    {
                        "feature": spec.name,
                        "model_spec": spec.to_dict(),
                        "serving_spec": srv_spec.to_dict(),
                    },
                )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_set_id": self.feature_set_id,
            "version": self.version,
            "owner": self.owner,
            "description": self.description,
            "features": [f.to_dict() for f in self.features],
            "schema_hash": self.schema_hash,
            "version_id": self.version_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FeatureSetContract":
        return cls(
            feature_set_id=data["feature_set_id"],
            version=data["version"],
            owner=data["owner"],
            description=data.get("description", ""),
            features=tuple(FeatureSpec.from_dict(f) for f in data["features"]),
        )


@dataclass(frozen=True)
class DatasetSnapshot:
    """Immutable versioned dataset manifest with provenance and entitlements."""

    dataset_id: str
    version: str
    source_uri: str
    content_sha256: str
    schema_hash: str
    row_count: int
    start_event_ts_utc: str
    end_event_ts_utc: str
    entitlement: str
    license_name: str
    asset_class: str
    currency: str
    is_synthetic: bool
    registered_at_utc: str = field(default_factory=utc_now_iso)
    registered_by: str = "system"

    def __post_init__(self) -> None:
        validate_safe_id(self.dataset_id, "dataset_id")
        parse_semver(self.version, "dataset.version")
        start_dt = parse_utc_timestamp(self.start_event_ts_utc, "start_event_ts_utc")
        end_dt = parse_utc_timestamp(self.end_event_ts_utc, "end_event_ts_utc")
        parse_utc_timestamp(self.registered_at_utc, "registered_at_utc")
        if end_dt < start_dt:
            raise TimestampContractError(
                ErrorCode.TIMESTAMP_CONTRACT_ERROR,
                "Dataset end_event_ts_utc cannot precede start_event_ts_utc.",
                {"start": self.start_event_ts_utc, "end": self.end_event_ts_utc},
            )
        if self.row_count <= 0:
            raise ContractError(
                ErrorCode.INVALID_CONTRACT,
                "Dataset row_count must be positive.",
                {"row_count": self.row_count},
            )
        if not self.entitlement or not self.license_name:
            raise ContractError(
                ErrorCode.INVALID_CONTRACT,
                "Dataset entitlement and license_name are mandatory.",
                {"dataset_id": self.dataset_id},
            )

    @property
    def snapshot_key(self) -> str:
        return f"{self.dataset_id}@{self.version}+{self.content_sha256[:16]}"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["snapshot_key"] = self.snapshot_key
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DatasetSnapshot":
        return cls(
            dataset_id=data["dataset_id"],
            version=data["version"],
            source_uri=data["source_uri"],
            content_sha256=data["content_sha256"],
            schema_hash=data["schema_hash"],
            row_count=int(data["row_count"]),
            start_event_ts_utc=data["start_event_ts_utc"],
            end_event_ts_utc=data["end_event_ts_utc"],
            entitlement=data["entitlement"],
            license_name=data["license_name"],
            asset_class=data["asset_class"],
            currency=data["currency"],
            is_synthetic=bool(data["is_synthetic"]),
            registered_at_utc=data["registered_at_utc"],
            registered_by=data["registered_by"],
        )


@dataclass(frozen=True)
class TrainingJobSpec:
    """Canonical specification for a reproducible training job."""

    idempotency_key: str
    model_name: str
    model_version: str
    strategy_id: str
    dataset_snapshot_key: str
    feature_set_version_id: str
    code_commit: str
    seed: int
    train_ratio: float
    val_ratio: float
    test_ratio: float
    embargo_seconds: int
    hyperparams: Dict[str, Any]
    target_column: str = "forward_return_bps"
    target_unit: str = "bps"
    currency: str = "USD"
    transaction_cost_bps: float = 5.0
    training_window_policy: str = "multi_regime_chronological"

    def __post_init__(self) -> None:
        validate_safe_id(self.idempotency_key, "idempotency_key")
        validate_safe_id(self.model_name, "model_name")
        parse_semver(self.model_version, "model_version")
        validate_safe_id(self.strategy_id, "strategy_id")
        if not self.code_commit or len(self.code_commit) < 7:
            raise ContractError(
                ErrorCode.INVALID_CONTRACT,
                "code_commit must be a pinned commit identifier (>= 7 chars).",
                {"code_commit": self.code_commit},
            )
        total = round(self.train_ratio + self.val_ratio + self.test_ratio, 6)
        if abs(total - 1.0) > 1e-5 or min(self.train_ratio, self.val_ratio, self.test_ratio) <= 0:
            raise ContractError(
                ErrorCode.INVALID_CONTRACT,
                "train_ratio, val_ratio, and test_ratio must be positive and sum to 1.0.",
                {
                    "train_ratio": self.train_ratio,
                    "val_ratio": self.val_ratio,
                    "test_ratio": self.test_ratio,
                },
            )
        if self.embargo_seconds < 0:
            raise ContractError(
                ErrorCode.INVALID_CONTRACT,
                "embargo_seconds must be non-negative.",
                {"embargo_seconds": self.embargo_seconds},
            )

    @property
    def spec_hash(self) -> str:
        return sha256_canonical(self.to_dict())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrainingJobSpec":
        return cls(**data)


@dataclass(frozen=True)
class ExperimentRun:
    """Tracked experiment run record (captures both succeeded and failed attempts)."""

    run_id: str
    idempotency_key: str
    spec_hash: str
    model_name: str
    model_version: str
    strategy_id: str
    dataset_snapshot_key: str
    feature_set_version_id: str
    code_commit: str
    seed: int
    created_by: str
    created_at_utc: str
    status: RunStatus
    metrics: Dict[str, Any]
    split_summary: Dict[str, Any]
    artifact_id: Optional[str] = None
    failure_code: Optional[str] = None
    failure_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExperimentRun":
        payload = dict(data)
        payload["status"] = RunStatus(payload["status"])
        return cls(**payload)


@dataclass(frozen=True)
class ValidationCheckResult:
    """Single check inside a multi-metric validation gate report."""

    check_name: str
    passed: bool
    observed: Any
    threshold: Any
    unit: str
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ValidationCheckResult":
        return cls(**data)


@dataclass(frozen=True)
class ValidationReport:
    """Immutable validation gate evidence linked to run and artifact."""

    report_id: str
    artifact_id: str
    run_id: str
    model_name: str
    model_version: str
    validator_id: str
    created_at_utc: str
    passed: bool
    checks: Tuple[ValidationCheckResult, ...]
    sample_size: int
    environment_info: Dict[str, Any]
    uncertainty: Dict[str, Any]
    report_sha256: str = ""

    def __post_init__(self) -> None:
        if not self.report_sha256:
            object.__setattr__(self, "report_sha256", self.compute_hash())

    def compute_hash(self) -> str:
        return sha256_canonical(
            {
                "report_id": self.report_id,
                "artifact_id": self.artifact_id,
                "run_id": self.run_id,
                "model_name": self.model_name,
                "model_version": self.model_version,
                "validator_id": self.validator_id,
                "created_at_utc": self.created_at_utc,
                "passed": self.passed,
                "checks": [c.to_dict() for c in self.checks],
                "sample_size": self.sample_size,
                "environment_info": self.environment_info,
                "uncertainty": self.uncertainty,
            }
        )

    def failed_checks(self) -> List[ValidationCheckResult]:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "report_id": self.report_id,
            "artifact_id": self.artifact_id,
            "run_id": self.run_id,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "validator_id": self.validator_id,
            "created_at_utc": self.created_at_utc,
            "passed": self.passed,
            "checks": [c.to_dict() for c in self.checks],
            "sample_size": self.sample_size,
            "environment_info": self.environment_info,
            "uncertainty": self.uncertainty,
            "report_sha256": self.report_sha256,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ValidationReport":
        return cls(
            report_id=data["report_id"],
            artifact_id=data["artifact_id"],
            run_id=data["run_id"],
            model_name=data["model_name"],
            model_version=data["model_version"],
            validator_id=data["validator_id"],
            created_at_utc=data["created_at_utc"],
            passed=bool(data["passed"]),
            checks=tuple(ValidationCheckResult.from_dict(c) for c in data["checks"]),
            sample_size=int(data["sample_size"]),
            environment_info=dict(data["environment_info"]),
            uncertainty=dict(data["uncertainty"]),
            report_sha256=data.get("report_sha256", ""),
        )


@dataclass(frozen=True)
class ModelCard:
    """Governance model card documenting provenance, metrics, limits, and sign-offs."""

    card_id: str
    artifact_id: str
    model_name: str
    model_version: str
    strategy_id: str
    owner: str
    created_at_utc: str
    intended_use: str
    out_of_scope_uses: Tuple[str, ...]
    dataset_lineage: Dict[str, Any]
    feature_lineage: Dict[str, Any]
    code_commit: str
    evaluation_summary: Dict[str, Any]
    validation_report_id: str
    validation_passed: bool
    known_limitations: Tuple[str, ...]
    risk_controls: Dict[str, Any]
    card_sha256: str = ""

    def __post_init__(self) -> None:
        if not self.card_sha256:
            object.__setattr__(self, "card_sha256", self.compute_hash())

    def compute_hash(self) -> str:
        payload = self.to_dict()
        payload.pop("card_sha256", None)
        return sha256_canonical(payload)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "card_id": self.card_id,
            "artifact_id": self.artifact_id,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "strategy_id": self.strategy_id,
            "owner": self.owner,
            "created_at_utc": self.created_at_utc,
            "intended_use": self.intended_use,
            "out_of_scope_uses": list(self.out_of_scope_uses),
            "dataset_lineage": self.dataset_lineage,
            "feature_lineage": self.feature_lineage,
            "code_commit": self.code_commit,
            "evaluation_summary": self.evaluation_summary,
            "validation_report_id": self.validation_report_id,
            "validation_passed": self.validation_passed,
            "known_limitations": list(self.known_limitations),
            "risk_controls": self.risk_controls,
            "card_sha256": self.card_sha256,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModelCard":
        return cls(
            card_id=data["card_id"],
            artifact_id=data["artifact_id"],
            model_name=data["model_name"],
            model_version=data["model_version"],
            strategy_id=data["strategy_id"],
            owner=data["owner"],
            created_at_utc=data["created_at_utc"],
            intended_use=data["intended_use"],
            out_of_scope_uses=tuple(data["out_of_scope_uses"]),
            dataset_lineage=dict(data["dataset_lineage"]),
            feature_lineage=dict(data["feature_lineage"]),
            code_commit=data["code_commit"],
            evaluation_summary=dict(data["evaluation_summary"]),
            validation_report_id=data["validation_report_id"],
            validation_passed=bool(data["validation_passed"]),
            known_limitations=tuple(data["known_limitations"]),
            risk_controls=dict(data["risk_controls"]),
            card_sha256=data.get("card_sha256", ""),
        )


@dataclass(frozen=True)
class ApprovalRecord:
    """Independent governance approval record."""

    approval_id: str
    artifact_id: str
    target_stage: ModelStage
    approver_id: str
    approver_role: ActorRole
    approved_at_utc: str
    decision: str  # "APPROVED" or "REJECTED"
    rationale: str
    validation_report_id: str
    conditions: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "artifact_id": self.artifact_id,
            "target_stage": self.target_stage.value,
            "approver_id": self.approver_id,
            "approver_role": self.approver_role.value,
            "approved_at_utc": self.approved_at_utc,
            "decision": self.decision,
            "rationale": self.rationale,
            "validation_report_id": self.validation_report_id,
            "conditions": list(self.conditions),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ApprovalRecord":
        return cls(
            approval_id=data["approval_id"],
            artifact_id=data["artifact_id"],
            target_stage=ModelStage(data["target_stage"]),
            approver_id=data["approver_id"],
            approver_role=ActorRole(data["approver_role"]),
            approved_at_utc=data["approved_at_utc"],
            decision=data["decision"],
            rationale=data["rationale"],
            validation_report_id=data["validation_report_id"],
            conditions=tuple(data.get("conditions", [])),
        )


@dataclass(frozen=True)
class DeploymentRecord:
    """Controlled stage deployment linking verified artifact, approvals, and execution guard."""

    deployment_id: str
    environment: str
    stage: ModelStage
    model_name: str
    model_version: str
    artifact_id: str
    artifact_bundle_sha256: str
    dataset_snapshot_key: str
    feature_set_version_id: str
    code_commit: str
    validation_report_id: str
    approval_ids: Tuple[str, ...]
    deployed_by: str
    deployed_at_utc: str
    order_execution_enabled: bool
    is_active: bool
    previous_deployment_id: Optional[str] = None
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["stage"] = self.stage.value
        data["approval_ids"] = list(self.approval_ids)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeploymentRecord":
        payload = dict(data)
        payload["stage"] = ModelStage(payload["stage"])
        payload["approval_ids"] = tuple(payload["approval_ids"])
        return cls(**payload)


@dataclass(frozen=True)
class RollbackRecord:
    """Recorded rollback restoring a verified previous known-good deployment."""

    rollback_id: str
    environment: str
    from_deployment_id: str
    from_artifact_id: str
    restored_deployment_id: str
    restored_artifact_id: str
    restored_model_version: str
    executed_by: str
    executed_at_utc: str
    reason: str
    verified_integrity: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RollbackRecord":
        return cls(**data)
