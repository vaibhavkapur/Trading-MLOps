"""Integration tests covering full M1-M4 lifecycle, adapters, and CLI."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from trading_mlops.adapters import (
    PostgresRegistryStubAdapter,
    S3ArtifactStoreStubAdapter,
)
from trading_mlops.cli import main as cli_main
from trading_mlops.config import PlatformConfig
from trading_mlops.contracts import (
    Actor,
    ActorRole,
    DatasetSnapshot,
    FeatureSetContract,
    FeatureSpec,
    ModelStage,
    TrainingJobSpec,
)
from trading_mlops.errors import ErrorCode, MlopsError
from trading_mlops.lineage import load_dataset_records
from trading_mlops.platform import MlopsPlatform

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO_ROOT / "data" / "fixtures"
CONFIG_PATH = REPO_ROOT / "config" / "platform_config.json"


class TestEndToEndLifecycleAndAdapters(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / "state"
        self.cfg = PlatformConfig.load(CONFIG_PATH)
        self.manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))["fixtures"]
        self.platform = MlopsPlatform(state_dir=self.state_dir, config=self.cfg)
        self.researcher = Actor("alice.quant", ActorRole.RESEARCHER)
        self.validator = Actor("bob.validator", ActorRole.VALIDATOR)
        self.risk = Actor("carol.risk", ActorRole.RISK_APPROVER)
        self.compliance = Actor("frank.compliance", ActorRole.COMPLIANCE_OFFICER)
        self.operator = Actor("dave.release", ActorRole.RELEASE_OPERATOR)
        self.auditor = Actor("erin.audit", ActorRole.GOVERNANCE_AUDITOR)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _register_base(self):
        v1 = self.manifest["market_features_v1.json"]
        ds = DatasetSnapshot(
            dataset_id=v1["dataset_id"],
            version=v1["version"],
            source_uri="file://data/fixtures/market_features_v1.json",
            content_sha256=v1["content_sha256"],
            schema_hash=v1["schema_hash"],
            row_count=v1["row_count"],
            start_event_ts_utc=v1["start_event_ts_utc"],
            end_event_ts_utc=v1["end_event_ts_utc"],
            entitlement=v1["entitlement"],
            license_name=v1["license_name"],
            asset_class=v1["asset_class"],
            currency=v1["currency"],
            is_synthetic=True,
            registered_by=self.researcher.actor_id,
        )
        self.platform.register_dataset(ds, self.researcher)
        fs = FeatureSetContract(
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
        self.platform.register_feature_set(fs, self.researcher)
        for env in ("shadow", "paper", "live_gated"):
            self.platform.bind_environment_feature_contract(env, fs.version_id, self.operator)
        return ds, fs

    def test_full_lifecycle_stage_skip_blocked_and_cli(self) -> None:
        ds, fs = self._register_base()
        spec = TrainingJobSpec(
            idempotency_key="int-v1-1",
            model_name="alpha_ridge_eq",
            model_version="1.0.0",
            strategy_id="strat_int",
            dataset_snapshot_key=ds.snapshot_key,
            feature_set_version_id=fs.version_id,
            code_commit="feedface1234",
            seed=42,
            train_ratio=0.6,
            val_ratio=0.2,
            test_ratio=0.2,
            embargo_seconds=3600,
            hyperparams={"l2_penalty": 2.0, "signal_threshold_bps": 2.0},
        )
        run, art, replay = self.platform.run_training_job(spec, FIXTURES / "market_features_v1.json", self.researcher)
        self.assertFalse(replay)
        assert art is not None
        self.assertEqual(len(self.platform.external_tracker.logged_runs), 1)

        report, card = self.platform.validate_artifact(art["artifact_id"], self.validator)
        self.assertTrue(report.passed)
        self.assertTrue((self.state_dir / "model_cards" / f"{art['artifact_id']}.md").exists())
        self.assertEqual(card.validation_report_id, report.report_id)

        # Stage skip directly from VALIDATED -> PAPER must be blocked
        self.platform.approve_promotion(art["artifact_id"], ModelStage.PAPER, self.risk, "Paper approval")
        with self.assertRaises(MlopsError) as ctx_skip:
            self.platform.promote_artifact(art["artifact_id"], ModelStage.PAPER, "paper", self.operator)
        self.assertEqual(ctx_skip.exception.error_code, ErrorCode.INVALID_STAGE_TRANSITION)

        # Proper SHADOW -> PAPER -> LIVE_GATED (requires dual approval)
        self.platform.approve_promotion(art["artifact_id"], ModelStage.SHADOW, self.risk, "Shadow approval")
        dep_shadow = self.platform.promote_artifact(art["artifact_id"], ModelStage.SHADOW, "shadow", self.operator)
        self.assertFalse(dep_shadow.order_execution_enabled)

        dep_paper = self.platform.promote_artifact(art["artifact_id"], ModelStage.PAPER, "paper", self.operator)
        self.assertEqual(dep_paper.stage, ModelStage.PAPER)

        # LIVE_GATED with only RISK_APPROVER must fail (needs COMPLIANCE_OFFICER too)
        self.platform.approve_promotion(art["artifact_id"], ModelStage.LIVE_GATED, self.risk, "Risk signoff")
        with self.assertRaises(MlopsError) as ctx_dual:
            self.platform.promote_artifact(art["artifact_id"], ModelStage.LIVE_GATED, "live_gated", self.operator)
        self.assertEqual(ctx_dual.exception.error_code, ErrorCode.APPROVAL_REQUIREMENT_NOT_MET)

        self.platform.approve_promotion(art["artifact_id"], ModelStage.LIVE_GATED, self.compliance, "Compliance signoff")
        dep_live = self.platform.promote_artifact(art["artifact_id"], ModelStage.LIVE_GATED, "live_gated", self.operator)
        self.assertEqual(len(dep_live.approval_ids), 2)
        self.assertFalse(dep_live.order_execution_enabled)

        # Monitor paper deployment and verify observability export
        drifted, _ = load_dataset_records(FIXTURES / "market_features_drifted.json")
        drift_rep, obs_ev = self.platform.monitor_deployment("paper", "alpha_ridge_eq", drifted, self.auditor)
        self.assertEqual(drift_rep["overall_severity"], "CRITICAL")
        self.assertEqual(obs_ev["schema"], "trading_observability_metric_event_v1")

        # Verify CLI commands against state_dir
        for cmd in (
            ["--state-dir", str(self.state_dir), "--config", str(CONFIG_PATH), "verify-audit"],
            ["--state-dir", str(self.state_dir), "--config", str(CONFIG_PATH), "governance-review"],
            ["--state-dir", str(self.state_dir), "--config", str(CONFIG_PATH), "inspect-lineage", art["artifact_id"]],
            ["--state-dir", str(self.state_dir), "--config", str(CONFIG_PATH), "export-inference-manifest", art["artifact_id"]],
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli_main(cmd)
            self.assertEqual(rc, 0)
            parsed = json.loads(buf.getvalue())
            self.assertIsInstance(parsed, dict)

    def test_optional_external_adapter_stubs_fail_cleanly(self) -> None:
        with self.assertRaises(MlopsError) as c1:
            PostgresRegistryStubAdapter().connect()
        self.assertEqual(c1.exception.error_code, ErrorCode.ADAPTER_ERROR)

        with self.assertRaises(MlopsError) as c2:
            S3ArtifactStoreStubAdapter().verify_bucket_access()
        self.assertEqual(c2.exception.error_code, ErrorCode.ADAPTER_ERROR)


if __name__ == "__main__":
    unittest.main()
