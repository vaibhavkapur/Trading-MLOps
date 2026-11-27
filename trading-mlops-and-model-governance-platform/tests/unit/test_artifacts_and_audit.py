"""Unit tests for signed artifact integrity checks and tamper-evident audit hash chain."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from trading_mlops.artifacts import LocalArtifactStore
from trading_mlops.audit import AuditLog
from trading_mlops.config import SigningConfig
from trading_mlops.contracts import Actor, ActorRole
from trading_mlops.errors import ArtifactIntegrityError, AuditIntegrityError, ErrorCode


class TestArtifactsAndAudit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_signed_bundle_roundtrip_and_tamper_detection(self) -> None:
        store = LocalArtifactStore(self.root / "artifacts", SigningConfig())
        bundle_dir, bundle_sha, _ = store.write_bundle(
            artifact_id="art_test_v1",
            manifest={"artifact_id": "art_test_v1", "model_version": "1.0.0"},
            weights={"weights": [1.0, -0.5], "bias": 0.1},
            evaluation_manifest={"splits": {"test": {"net_sharpe": 1.2}}},
        )
        loaded = store.verify_and_load_bundle("art_test_v1", bundle_sha)
        self.assertEqual(loaded["bundle_sha256"], bundle_sha)

        # Mutate a single weight -> must raise ARTIFACT_TAMPERING_DETECTED
        w_path = bundle_dir / "weights.json"
        w = json.loads(w_path.read_text(encoding="utf-8"))
        w["bias"] = 42.0
        w_path.write_text(json.dumps(w), encoding="utf-8")
        with self.assertRaises(ArtifactIntegrityError) as ctx:
            store.verify_and_load_bundle("art_test_v1", bundle_sha)
        self.assertEqual(ctx.exception.error_code, ErrorCode.ARTIFACT_TAMPERING_DETECTED)

    def test_rejects_untrusted_pickle_and_path_traversal(self) -> None:
        store = LocalArtifactStore(self.root / "artifacts", SigningConfig())
        bundle_dir, bundle_sha, _ = store.write_bundle(
            artifact_id="art_pkl_test",
            manifest={"artifact_id": "art_pkl_test"},
            weights={"weights": [0.1]},
            evaluation_manifest={"splits": {}},
        )
        (bundle_dir / "malicious.pkl").write_bytes(b"not-safe")
        with self.assertRaises(ArtifactIntegrityError) as ctx1:
            store.verify_and_load_bundle("art_pkl_test", bundle_sha)
        self.assertEqual(ctx1.exception.error_code, ErrorCode.UNTRUSTED_ARTIFACT_FORMAT)

        with self.assertRaises(ArtifactIntegrityError) as ctx2:
            store.verify_and_load_bundle("../escape", bundle_sha)
        self.assertEqual(ctx2.exception.error_code, ErrorCode.PATH_TRAVERSAL_BLOCKED)

    def test_signature_fails_with_different_signing_key(self) -> None:
        cfg = SigningConfig(key_env_var="TEST_MLOPS_KEY_UNIT")
        os.environ["TEST_MLOPS_KEY_UNIT"] = "secret-key-A"
        try:
            store = LocalArtifactStore(self.root / "artifacts", cfg)
            _, bundle_sha, _ = store.write_bundle(
                artifact_id="art_sig_test",
                manifest={"artifact_id": "art_sig_test"},
                weights={"weights": [0.2]},
                evaluation_manifest={},
            )
            os.environ["TEST_MLOPS_KEY_UNIT"] = "secret-key-B"
            with self.assertRaises(ArtifactIntegrityError) as ctx:
                store.verify_and_load_bundle("art_sig_test", bundle_sha)
            self.assertEqual(ctx.exception.error_code, ErrorCode.ARTIFACT_TAMPERING_DETECTED)
        finally:
            os.environ.pop("TEST_MLOPS_KEY_UNIT", None)

    def test_audit_log_hash_chain_detects_tampering(self) -> None:
        log = AuditLog(self.root / "audit.jsonl")
        actor = Actor("alice.quant", ActorRole.RESEARCHER)
        log.record("DATASET_REGISTERED", actor, "dataset", "ds1", "SUCCESS", {"rows": 100})
        log.record("MODEL_PROMOTED", actor, "deployment", "dep1", "SUCCESS", {"stage": "SHADOW"})
        status = log.verify_chain()
        self.assertTrue(status["verified"])
        self.assertEqual(status["event_count"], 2)

        # Tamper with event 1 details in place
        lines = (self.root / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        ev1 = json.loads(lines[0])
        ev1["details"]["rows"] = 999999
        lines[0] = json.dumps(ev1)
        (self.root / "audit.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

        with self.assertRaises(AuditIntegrityError) as ctx:
            log.verify_chain()
        self.assertEqual(ctx.exception.error_code, ErrorCode.AUDIT_CHAIN_TAMPERED)


if __name__ == "__main__":
    unittest.main()
