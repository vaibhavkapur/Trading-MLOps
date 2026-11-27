"""External adapter protocols and cross-project integration contracts."""

from trading_mlops.adapters.external_contracts import (
    InferenceEngineManifestAdapter,
    MLflowTrackingStubAdapter,
    ObservabilityEventAdapter,
    PostgresRegistryStubAdapter,
    S3ArtifactStoreStubAdapter,
)

__all__ = [
    "InferenceEngineManifestAdapter",
    "MLflowTrackingStubAdapter",
    "ObservabilityEventAdapter",
    "PostgresRegistryStubAdapter",
    "S3ArtifactStoreStubAdapter",
]
