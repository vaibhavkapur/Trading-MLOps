"""Structured error codes and exception hierarchy for the Trading MLOps platform."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional


class ErrorCode(str, Enum):
    """Machine-readable error codes for every documented failure mode."""

    INVALID_CONTRACT = "INVALID_CONTRACT"
    TIMESTAMP_CONTRACT_ERROR = "TIMESTAMP_CONTRACT_ERROR"
    DATA_LEAKAGE_DETECTED = "DATA_LEAKAGE_DETECTED"
    BLIND_RETRAINING_POLICY_VIOLATION = "BLIND_RETRAINING_POLICY_VIOLATION"
    INCOMPATIBLE_FEATURE_VERSION = "INCOMPATIBLE_FEATURE_VERSION"
    ARTIFACT_TAMPERING_DETECTED = "ARTIFACT_TAMPERING_DETECTED"
    UNTRUSTED_ARTIFACT_FORMAT = "UNTRUSTED_ARTIFACT_FORMAT"
    PATH_TRAVERSAL_BLOCKED = "PATH_TRAVERSAL_BLOCKED"
    VALIDATION_GATE_FAILED = "VALIDATION_GATE_FAILED"
    INCOMPLETE_LINEAGE = "INCOMPLETE_LINEAGE"
    UNAUTHORIZED_PROMOTION = "UNAUTHORIZED_PROMOTION"
    SELF_APPROVAL_FORBIDDEN = "SELF_APPROVAL_FORBIDDEN"
    INVALID_STAGE_TRANSITION = "INVALID_STAGE_TRANSITION"
    APPROVAL_REQUIREMENT_NOT_MET = "APPROVAL_REQUIREMENT_NOT_MET"
    EXECUTION_DISABLED_GUARD = "EXECUTION_DISABLED_GUARD"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    IMMUTABLE_VERSION_CONFLICT = "IMMUTABLE_VERSION_CONFLICT"
    NOT_FOUND = "NOT_FOUND"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    AUDIT_CHAIN_TAMPERED = "AUDIT_CHAIN_TAMPERED"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    ADAPTER_ERROR = "ADAPTER_ERROR"


class MlopsError(Exception):
    """Base structured error with code and machine-readable details."""

    def __init__(
        self,
        error_code: ErrorCode,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(f"[{error_code.value}] {message}")
        self.error_code = error_code
        self.message = message
        self.details: Dict[str, Any] = dict(details or {})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error_code": self.error_code.value,
            "message": self.message,
            "details": self.details,
        }


class ContractError(MlopsError):
    """Raised when a domain contract invariant is violated."""


class TimestampContractError(MlopsError):
    """Raised on naive timestamps or availability_ts_utc < event_ts_utc."""


class DataLeakageError(MlopsError):
    """Raised when point-in-time or split leakage is detected."""


class FeatureCompatibilityError(MlopsError):
    """Raised when training and target serving feature versions are incompatible."""


class ArtifactIntegrityError(MlopsError):
    """Raised on SHA-256 hash mismatch, invalid HMAC signature, or unsafe format."""


class ValidationGateError(MlopsError):
    """Raised when promotion is attempted without a passing validation report."""


class GovernanceError(MlopsError):
    """Raised on unauthorized promotion, self-approval, missing sign-off, or stage skip."""


class IdempotencyConflictError(MlopsError):
    """Raised when an idempotency key is reused with conflicting specifications."""


class RollbackError(MlopsError):
    """Raised when rollback cannot restore a verified known-good version."""


class AuditIntegrityError(MlopsError):
    """Raised when the tamper-evident audit hash chain fails verification."""
