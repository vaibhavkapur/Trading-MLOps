"""Failure scenario verification suite covering all 7 required scenarios from plan.md Section 5."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from trading_mlops.config import PlatformConfig
from trading_mlops.contracts import (
    Actor,
    ActorRole,
    DatasetSnapshot,
    FeatureSetContract,
    FeatureSpec,
    ModelStage,
    RunStatus,
    TrainingJobSpec,
)
from trading_mlops.errors import ErrorCode, MlopsError
from trading_mlops.platform import MlopsPlatform

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO_ROOT / "data" / "fixtures"
CONFIG_PATH = REPO_ROOT / "config" / "platform_config.json"


class TestRequiredFailureScenarios(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / "state"
        self.cfg = PlatformConfig.load(CONFIG_PATH)
        self.manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))["fixtures"]
        self.platform = MlopsPlatform(state_dir=self.state_dir, config=self.cfg)

        self.researcher = Actor("alice.quant", ActorRole.RESEARCHER)
        self.validator = Actor("bob.validator", ActorRole.VALIDATOR)
        self.risk = Actor("carol.risk", ActorRole.RISK_APPROVER)
        self.operator = Actor("dave.release", ActorRole.RELEASE_OPERATOR)

        self.ds_v1 = self._reg_ds("market_features_v1.json")
        self.ds_v2 = self._reg_ds("market_features_v2.json")
        self.ds_leaked = self._reg_ds("market_features_leaked.json")

        self.fs_v1 = FeatureSetContract(
            feature_set_id="eq_micro_alpha_fs",
            version="1.0.0",
            owner="quant-fe",
            features=(
                FeatureSpec("order_book_imbalance", "1.0.0", "float64", "ratio"),
                FeatureSpec("momentum_24h_bps", "1.0.0", "float64", "bps", "USD"),
                FeatureSpec("realized_vol_24h_bps", "1.0.0", "float64", "bps", "USD"),
                FeatureSpec("bid_ask_spread_bps", "1.0.0", "float64", "bps", "USD"),
            ),
        )
        self.fs_v2 = FeatureSetContract(
            feature_set_id="eq_micro_alpha_fs",
            version="2.0.0",
            owner="quant-fe",
            features=(
                FeatureSpec("order_book_imbalance", "2.0.0", "float64", "ratio"),
                FeatureSpec("momentum_24h_bps", "1.0.0", "float64", "bps", "USD"),
                FeatureSpec("realized_vol_24h_bps", "1.0.0", "float64", "bps", "USD"),
                FeatureSpec("bid_ask_spread_bps", "1.0.0", "float64", "bps", "USD"),
            ),
        )
        self.platform.register_feature_set(self.fs_v1, self.researcher)
        self.platform.register_feature_set(self.fs_v2, self.researcher)
        self.platform.bind_environment_feature_contract("shadow", self.fs_v1.version_id, self.operator)
        self.platform.bind_environment_feature_contract("paper", self.fs_v1.version_id, self.operator)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _reg_ds(self, fname: str) -> DatasetSnapshot:
        m = self.manifest[fname]
        ds = DatasetSnapshot(
            dataset_id=m["dataset_id"],
            version=m["version"],
            source_uri=f"file://data/fixtures/{fname}",
            content_sha256=m["content_sha256"],
            schema_hash=m["schema_hash"],
            row_count=m["row_count"],
            start_event_ts_utc=m["start_event_ts_utc"],
            end_event_ts_utc=m["end_event_ts_utc"],
            entitlement=m["entitlement"],
            license_name=m["license_name"],
            asset_class=m["asset_class"],
            currency=m["currency"],
            is_synthetic=True,
            registered_by=self.researcher.actor_id,
        )
        return self.platform.register_dataset(ds, self.researcher)

    def _train_clean(self, key: str, model_version: str, ds: DatasetSnapshot, fs: FeatureSetContract, **hp):
        spec = TrainingJobSpec(
            idempotency_key=key,
            model_name="alpha_ridge_eq",
            model_version=model_version,
            strategy_id="strat_scn",
            dataset_snapshot_key=ds.snapshot_key,
            feature_set_version_id=fs.version_id,
            code_commit="deadbeef1234",
            seed=42,
            train_ratio=0.6,
            val_ratio=0.2,
            test_ratio=0.2,
            embargo_seconds=3600,
            hyperparams=hp or {"l2_penalty": 2.0, "signal_threshold_bps": 2.0},
        )
        fname = "market_features_v1.json" if ds == self.ds_v1 else "market_features_v2.json"
        return self.platform.run_training_job(spec, FIXTURES / fname, self.researcher)

    def test_scenario_1_data_leakage(self) -> None:
        spec = TrainingJobSpec(
            idempotency_key="scn-leakage",
            model_name="alpha_leak",
            model_version="1.0.0",
            strategy_id="strat_scn",
            dataset_snapshot_key=self.ds_leaked.snapshot_key,
            feature_set_version_id=self.fs_v1.version_id,
            code_commit="deadbeef1234",
            seed=42,
            train_ratio=0.6,
            val_ratio=0.2,
            test_ratio=0.2,
            embargo_seconds=3600,
            hyperparams={"l2_penalty": 2.0},
        )
        with self.assertRaises(MlopsError) as ctx:
            self.platform.run_training_job(spec, FIXTURES / "market_features_leaked.json", self.researcher)
        self.assertEqual(ctx.exception.error_code, ErrorCode.DATA_LEAKAGE_DETECTED)
        # Failed experiment must be preserved in registry
        failed = self.platform.registry.get_run_by_idempotency_key("scn-leakage")
        self.assertIsNotNone(failed)
        assert failed is not None
        self.assertEqual(failed.status, RunStatus.REJECTED_LEAKAGE)

    def test_scenario_2_artifact_tampering(self) -> None:
        _, art, _ = self._train_clean("scn-tamper", "1.0.0", self.ds_v1, self.fs_v1)
        assert art is not None
        rep, _ = self.platform.validate_artifact(art["artifact_id"], self.validator)
        self.assertTrue(rep.passed)
        self.platform.approve_promotion(art["artifact_id"], ModelStage.SHADOW, self.risk, "Approved")

        # Tamper with artifact weights on disk after approval
        w_file = Path(art["bundle_path"]) / "weights.json"
        payload = json.loads(w_file.read_text(encoding="utf-8"))
        payload["weights"][0] += 100.0
        w_file.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(MlopsError) as ctx:
            self.platform.promote_artifact(art["artifact_id"], ModelStage.SHADOW, "shadow", self.operator)
        self.assertEqual(ctx.exception.error_code, ErrorCode.ARTIFACT_TAMPERING_DETECTED)

    def test_scenario_3_failed_validation(self) -> None:
        _, art, _ = self._train_clean(
            "scn-fail-val",
            "1.0.0",
            self.ds_v1,
            self.fs_v1,
            l2_penalty=2.0,
            signal_multiplier=-1.0,
        )
        assert art is not None
        rep, _ = self.platform.validate_artifact(art["artifact_id"], self.validator)
        self.assertFalse(rep.passed)
        with self.assertRaises(MlopsError) as ctx:
            self.platform.approve_promotion(art["artifact_id"], ModelStage.SHADOW, self.risk, "Try approve")
        self.assertEqual(ctx.exception.error_code, ErrorCode.VALIDATION_GATE_FAILED)

    def test_scenario_4_unauthorized_promotion(self) -> None:
        _, art, _ = self._train_clean("scn-unauth", "1.0.0", self.ds_v1, self.fs_v1)
        assert art is not None
        self.platform.validate_artifact(art["artifact_id"], self.validator)

        # Self-approval attempt by creator posing with RISK_APPROVER role
        self_approver = Actor(self.researcher.actor_id, ActorRole.RISK_APPROVER)
        with self.assertRaises(MlopsError) as ctx_self:
            self.platform.approve_promotion(art["artifact_id"], ModelStage.SHADOW, self_approver, "Self approve")
        self.assertEqual(ctx_self.exception.error_code, ErrorCode.SELF_APPROVAL_FORBIDDEN)

        # Researcher attempting to execute deployment directly
        self.platform.approve_promotion(art["artifact_id"], ModelStage.SHADOW, self.risk, "Independent approval")
        with self.assertRaises(MlopsError) as ctx_role:
            self.platform.promote_artifact(art["artifact_id"], ModelStage.SHADOW, "shadow", self.researcher)
        self.assertEqual(ctx_role.exception.error_code, ErrorCode.UNAUTHORIZED_PROMOTION)

    def test_scenario_5_incompatible_feature_version(self) -> None:
        _, art, _ = self._train_clean("scn-incompat-fs", "1.0.0", self.ds_v1, self.fs_v2)
        assert art is not None
        rep, _ = self.platform.validate_artifact(art["artifact_id"], self.validator)
        self.assertTrue(rep.passed)
        self.platform.approve_promotion(art["artifact_id"], ModelStage.SHADOW, self.risk, "Approved")

        with self.assertRaises(MlopsError) as ctx:
            self.platform.promote_artifact(art["artifact_id"], ModelStage.SHADOW, "shadow", self.operator)
        self.assertEqual(ctx.exception.error_code, ErrorCode.INCOMPATIBLE_FEATURE_VERSION)

    def test_scenario_6_rollback(self) -> None:
        _, art1, _ = self._train_clean("scn-rb-v1", "1.0.0", self.ds_v1, self.fs_v1)
        _, art2, _ = self._train_clean("scn-rb-v2", "2.0.0", self.ds_v2, self.fs_v1)
        assert art1 is not None and art2 is not None

        for art in (art1, art2):
            self.platform.validate_artifact(art["artifact_id"], self.validator)
            self.platform.approve_promotion(art["artifact_id"], ModelStage.SHADOW, self.risk, "Shadow ok")
            self.platform.promote_artifact(art["artifact_id"], ModelStage.SHADOW, "shadow", self.operator)
            self.platform.approve_promotion(art["artifact_id"], ModelStage.PAPER, self.risk, "Paper ok")
            self.platform.promote_artifact(art["artifact_id"], ModelStage.PAPER, "paper", self.operator)

        active_before = self.platform.registry.get_active_deployment("paper", "alpha_ridge_eq")
        assert active_before is not None
        self.assertEqual(active_before.model_version, "2.0.0")

        rb_rec, restored = self.platform.rollback_deployment("paper", "alpha_ridge_eq", self.operator, "Drift incident")
        self.assertTrue(rb_rec.verified_integrity)
        self.assertEqual(restored.artifact_id, art1["artifact_id"])
        self.assertEqual(restored.model_version, "1.0.0")
        self.assertFalse(restored.order_execution_enabled)

        active_after = self.platform.registry.get_active_deployment("paper", "alpha_ridge_eq")
        assert active_after is not None
        self.assertEqual(active_after.artifact_id, art1["artifact_id"])

    def test_scenario_7_repeated_job(self) -> None:
        run1, art1, replay1 = self._train_clean("scn-repeat-1", "1.0.0", self.ds_v1, self.fs_v1)
        run2, art2, replay2 = self._train_clean("scn-repeat-1", "1.0.0", self.ds_v1, self.fs_v1)
        self.assertFalse(replay1)
        self.assertTrue(replay2)
        self.assertEqual(run1.run_id, run2.run_id)
        assert art1 is not None and art2 is not None
        self.assertEqual(art1["bundle_sha256"], art2["bundle_sha256"])

        # Same idempotency key with mutated hyperparams must raise IDEMPOTENCY_CONFLICT
        with self.assertRaises(MlopsError) as ctx:
            self._train_clean("scn-repeat-1", "1.0.0", self.ds_v1, self.fs_v1, l2_penalty=99.0)
        self.assertEqual(ctx.exception.error_code, ErrorCode.IDEMPOTENCY_CONFLICT)

    def test_scenario_8_blind_retraining_policy_rejected(self) -> None:
        spec = TrainingJobSpec(
            idempotency_key="scn-blind-retrain",
            model_name="alpha_blind",
            model_version="1.0.0",
            strategy_id="strat_scn",
            dataset_snapshot_key=self.ds_v1.snapshot_key,
            feature_set_version_id=self.fs_v1.version_id,
            code_commit="deadbeef1234",
            seed=42,
            train_ratio=0.6,
            val_ratio=0.2,
            test_ratio=0.2,
            embargo_seconds=3600,
            hyperparams={"l2_penalty": 2.0},
            training_window_policy="recent_returns_only",
        )
        with self.assertRaises(MlopsError) as ctx:
            self.platform.run_training_job(spec, FIXTURES / "market_features_v1.json", self.researcher)
        self.assertEqual(ctx.exception.error_code, ErrorCode.BLIND_RETRAINING_POLICY_VIOLATION)


if __name__ == "__main__":
    unittest.main()
