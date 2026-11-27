"""Content-addressed, SHA-256 hash-checked, HMAC-signed safe artifact bundle store."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, Dict, Tuple

from trading_mlops.config import SigningConfig
from trading_mlops.contracts import canonical_json_bytes, sha256_bytes, sha256_canonical
from trading_mlops.errors import ArtifactIntegrityError, ErrorCode

FORBIDDEN_EXTENSIONS = {".pkl", ".pickle", ".joblib", ".pt", ".pth", ".dill", ".cloudpickle"}


class LocalArtifactStore:
    """Local object-storage adapter implementing deterministic, signed artifact bundles."""

    def __init__(self, root_dir: Path, signing_config: SigningConfig) -> None:
        self.root_dir = root_dir.resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.signing_config = signing_config

    def _safe_bundle_dir(self, artifact_id: str) -> Path:
        if ".." in artifact_id or "/" in artifact_id or "\\" in artifact_id:
            raise ArtifactIntegrityError(
                ErrorCode.PATH_TRAVERSAL_BLOCKED,
                f"Unsafe artifact identifier rejected: {artifact_id!r}.",
                {"artifact_id": artifact_id},
            )
        target = (self.root_dir / artifact_id).resolve()
        if self.root_dir not in target.parents:
            raise ArtifactIntegrityError(
                ErrorCode.PATH_TRAVERSAL_BLOCKED,
                f"Artifact path escapes object store root: {target}.",
                {"artifact_id": artifact_id},
            )
        return target

    def _sign_digest(self, bundle_sha256: str) -> str:
        secret = self.signing_config.resolve_signing_secret()
        return hmac.new(secret, bundle_sha256.encode("utf-8"), hashlib.sha256).hexdigest()

    def write_bundle(
        self,
        artifact_id: str,
        manifest: Dict[str, Any],
        weights: Dict[str, Any],
        evaluation_manifest: Dict[str, Any],
    ) -> Tuple[Path, str, str]:
        """Write deterministic safe JSON bundle files, hash manifest, and HMAC signature."""
        bundle_dir = self._safe_bundle_dir(artifact_id)
        bundle_dir.mkdir(parents=True, exist_ok=True)

        weights_bytes = canonical_json_bytes(weights)
        eval_bytes = canonical_json_bytes(evaluation_manifest)
        weights_sha = sha256_bytes(weights_bytes)
        eval_sha = sha256_bytes(eval_bytes)

        full_manifest = dict(manifest)
        full_manifest["format"] = "safe_json_v1"
        full_manifest["file_checksums"] = {
            "weights.json": weights_sha,
            "evaluation_manifest.json": eval_sha,
        }
        manifest_bytes = canonical_json_bytes(full_manifest)
        manifest_sha = sha256_bytes(manifest_bytes)

        bundle_sha256 = sha256_canonical(
            {
                "manifest.json": manifest_sha,
                "weights.json": weights_sha,
                "evaluation_manifest.json": eval_sha,
            }
        )
        signature_hex = self._sign_digest(bundle_sha256)
        signature_payload = {
            "algorithm": self.signing_config.algorithm,
            "key_id": self.signing_config.key_id,
            "bundle_sha256": bundle_sha256,
            "manifest_sha256": manifest_sha,
            "signature": signature_hex,
        }
        signature_bytes = canonical_json_bytes(signature_payload)

        (bundle_dir / "weights.json").write_bytes(weights_bytes)
        (bundle_dir / "evaluation_manifest.json").write_bytes(eval_bytes)
        (bundle_dir / "manifest.json").write_bytes(manifest_bytes)
        (bundle_dir / "signature.json").write_bytes(signature_bytes)

        return bundle_dir, bundle_sha256, signature_hex

    def verify_and_load_bundle(
        self,
        artifact_id: str,
        expected_bundle_sha256: str,
    ) -> Dict[str, Any]:
        """Verify file types, SHA-256 checksums, bundle digest, and HMAC signature before loading."""
        bundle_dir = self._safe_bundle_dir(artifact_id)
        if not bundle_dir.exists() or not bundle_dir.is_dir():
            raise ArtifactIntegrityError(
                ErrorCode.ARTIFACT_TAMPERING_DETECTED,
                f"Artifact bundle directory missing for '{artifact_id}'.",
                {"artifact_id": artifact_id, "bundle_dir": str(bundle_dir)},
            )

        # Reject any untrusted serialized file placed in the bundle directory
        for child in bundle_dir.iterdir():
            if child.suffix.lower() in FORBIDDEN_EXTENSIONS:
                raise ArtifactIntegrityError(
                    ErrorCode.UNTRUSTED_ARTIFACT_FORMAT,
                    f"Forbidden untrusted serialized format found in bundle: '{child.name}'.",
                    {"artifact_id": artifact_id, "file": child.name},
                )

        required_files = ["manifest.json", "weights.json", "evaluation_manifest.json", "signature.json"]
        raw_bytes: Dict[str, bytes] = {}
        for fname in required_files:
            fpath = bundle_dir / fname
            if not fpath.exists():
                raise ArtifactIntegrityError(
                    ErrorCode.ARTIFACT_TAMPERING_DETECTED,
                    f"Required bundle file '{fname}' missing for artifact '{artifact_id}'.",
                    {"artifact_id": artifact_id, "missing_file": fname},
                )
            raw_bytes[fname] = fpath.read_bytes()

        try:
            manifest = json.loads(raw_bytes["manifest.json"].decode("utf-8"))
            weights = json.loads(raw_bytes["weights.json"].decode("utf-8"))
            evaluation_manifest = json.loads(raw_bytes["evaluation_manifest.json"].decode("utf-8"))
            signature_doc = json.loads(raw_bytes["signature.json"].decode("utf-8"))
        except Exception as exc:
            raise ArtifactIntegrityError(
                ErrorCode.ARTIFACT_TAMPERING_DETECTED,
                f"Corrupted JSON payload in artifact bundle '{artifact_id}'.",
                {"artifact_id": artifact_id, "error": str(exc)},
            ) from exc

        if manifest.get("format") != "safe_json_v1":
            raise ArtifactIntegrityError(
                ErrorCode.UNTRUSTED_ARTIFACT_FORMAT,
                f"Unsupported artifact bundle format: {manifest.get('format')!r}.",
                {"artifact_id": artifact_id, "format": manifest.get("format")},
            )

        checksums = manifest.get("file_checksums", {})
        actual_weights_sha = sha256_bytes(raw_bytes["weights.json"])
        actual_eval_sha = sha256_bytes(raw_bytes["evaluation_manifest.json"])
        actual_manifest_sha = sha256_bytes(raw_bytes["manifest.json"])

        if checksums.get("weights.json") != actual_weights_sha:
            raise ArtifactIntegrityError(
                ErrorCode.ARTIFACT_TAMPERING_DETECTED,
                f"SHA-256 mismatch on weights.json for artifact '{artifact_id}'.",
                {
                    "artifact_id": artifact_id,
                    "file": "weights.json",
                    "expected": checksums.get("weights.json"),
                    "observed": actual_weights_sha,
                },
            )
        if checksums.get("evaluation_manifest.json") != actual_eval_sha:
            raise ArtifactIntegrityError(
                ErrorCode.ARTIFACT_TAMPERING_DETECTED,
                f"SHA-256 mismatch on evaluation_manifest.json for artifact '{artifact_id}'.",
                {
                    "artifact_id": artifact_id,
                    "file": "evaluation_manifest.json",
                    "expected": checksums.get("evaluation_manifest.json"),
                    "observed": actual_eval_sha,
                },
            )

        recomputed_bundle_sha = sha256_canonical(
            {
                "manifest.json": actual_manifest_sha,
                "weights.json": actual_weights_sha,
                "evaluation_manifest.json": actual_eval_sha,
            }
        )
        if recomputed_bundle_sha != expected_bundle_sha256:
            raise ArtifactIntegrityError(
                ErrorCode.ARTIFACT_TAMPERING_DETECTED,
                f"Bundle SHA-256 mismatch for artifact '{artifact_id}' against registry record.",
                {
                    "artifact_id": artifact_id,
                    "expected_bundle_sha256": expected_bundle_sha256,
                    "observed_bundle_sha256": recomputed_bundle_sha,
                },
            )

        if signature_doc.get("bundle_sha256") != recomputed_bundle_sha:
            raise ArtifactIntegrityError(
                ErrorCode.ARTIFACT_TAMPERING_DETECTED,
                f"Signature digest does not match bundle SHA-256 for artifact '{artifact_id}'.",
                {"artifact_id": artifact_id},
            )

        expected_sig = self._sign_digest(recomputed_bundle_sha)
        observed_sig = str(signature_doc.get("signature", ""))
        if not hmac.compare_digest(expected_sig, observed_sig):
            raise ArtifactIntegrityError(
                ErrorCode.ARTIFACT_TAMPERING_DETECTED,
                f"Invalid HMAC-SHA256 signature for artifact '{artifact_id}'.",
                {"artifact_id": artifact_id, "key_id": signature_doc.get("key_id")},
            )

        return {
            "artifact_id": artifact_id,
            "bundle_sha256": recomputed_bundle_sha,
            "manifest": manifest,
            "weights": weights,
            "evaluation_manifest": evaluation_manifest,
            "signature": signature_doc,
        }
