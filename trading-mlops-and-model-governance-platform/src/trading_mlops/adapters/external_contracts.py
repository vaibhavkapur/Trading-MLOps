"""Clean adapter boundaries for PostgreSQL, S3, MLflow, and sibling platform contracts (#16, #18, #24)."""

from __future__ import annotations

from typing import Any, Dict, List, Protocol

from trading_mlops.contracts import FeatureSetContract, utc_now_iso
from trading_mlops.errors import ErrorCode, MlopsError


class ExternalTrackerProtocol(Protocol):
    """Protocol isolating MLflow or external experiment trackers from core MLOps logic."""

    def log_run(self, run_payload: Dict[str, Any]) -> str:
        ...


class InferenceEngineManifestAdapter:
    """Exports a verified artifact bundle into Project #16 (Low-Latency ML Inference Engine) manifest contract."""

    @staticmethod
    def to_inference_manifest(
        artifact_record: Dict[str, Any],
        verified_bundle: Dict[str, Any],
        feature_set: FeatureSetContract,
    ) -> Dict[str, Any]:
        weights = verified_bundle["weights"]
        return {
            "contract_version": "inference_manifest_v1",
            "model_id": artifact_record["model_name"],
            "model_version": artifact_record["model_version"],
            "artifact_id": artifact_record["artifact_id"],
            "artifact_sha256": verified_bundle["bundle_sha256"],
            "feature_set_version_id": feature_set.version_id,
            "output_name": weights["target_column"],
            "output_unit": weights["target_unit"],
            "currency": weights["currency"],
            "order_execution_enabled": False,
            "features": [
                {
                    "name": f.name,
                    "version": f.version,
                    "version_id": f.version_id,
                    "dtype": f.dtype,
                    "unit": f.unit,
                    "mean": weights["feature_means"][f.name],
                    "std": weights["feature_stds"][f.name],
                    "min_bound": f.min_bound,
                    "max_bound": f.max_bound,
                }
                for f in feature_set.features
            ],
            "linear_weights": {
                "weights": weights["weights"],
                "bias": weights["bias"],
                "signal_multiplier": weights.get("signal_multiplier", 1.0),
            },
            "exported_at_utc": utc_now_iso(),
        }


class ObservabilityEventAdapter:
    """Formats audit and drift events into structured telemetry compatible with Project #24 (Trading Observability)."""

    @staticmethod
    def from_drift_report(drift_report: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "schema": "trading_observability_metric_event_v1",
            "source_service": "trading-mlops-and-model-governance-platform",
            "timestamp_utc": drift_report["created_at_utc"],
            "environment": drift_report["environment"],
            "artifact_id": drift_report["artifact_id"],
            "severity": drift_report["overall_severity"],
            "metrics": {
                "sample_size": drift_report["sample_size"],
                "max_feature_psi": max(
                    (f["psi"] for f in drift_report["feature_drift"].values()), default=0.0
                ),
                "prediction_mean_z_shift": drift_report["prediction_drift"]["mean_z_shift"],
                "alert_count": len(drift_report["alerts"]),
            },
            "alerts": drift_report["alerts"],
            "safe_fallback": drift_report["recommended_safe_fallback"],
        }


class MLflowTrackingStubAdapter:
    """Optional external tracker boundary (offline-safe in-memory sink unless MLflow backend is configured)."""

    def __init__(self, tracking_uri: str = "offline://stub") -> None:
        self.tracking_uri = tracking_uri
        self.logged_runs: List[Dict[str, Any]] = []

    def log_run(self, run_payload: Dict[str, Any]) -> str:
        self.logged_runs.append(dict(run_payload))
        return f"mlflow-stub://{run_payload['run_id']}"


class PostgresRegistryStubAdapter:
    """Documented PostgreSQL adapter boundary; raises clear error if invoked without DSN/psycopg."""

    def __init__(self, dsn_env_var: str = "TRADING_MLOPS_POSTGRES_DSN") -> None:
        self.dsn_env_var = dsn_env_var

    def connect(self) -> None:
        raise MlopsError(
            ErrorCode.ADAPTER_ERROR,
            f"PostgreSQL adapter is not configured in local sandbox mode; set {self.dsn_env_var} and install optional '[postgres]' extra.",
            {"dsn_env_var": self.dsn_env_var},
        )


class S3ArtifactStoreStubAdapter:
    """Documented S3/object-storage adapter boundary for non-local sandbox environments."""

    def __init__(self, bucket_env_var: str = "TRADING_MLOPS_ARTIFACT_BUCKET") -> None:
        self.bucket_env_var = bucket_env_var

    def verify_bucket_access(self) -> None:
        raise MlopsError(
            ErrorCode.ADAPTER_ERROR,
            f"S3 artifact adapter is not configured in local sandbox mode; set {self.bucket_env_var} and supply credentials outside code.",
            {"bucket_env_var": self.bucket_env_var},
        )
