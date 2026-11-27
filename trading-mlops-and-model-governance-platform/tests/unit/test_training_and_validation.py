"""Unit tests for deterministic training, multi-metric validation gate, model cards, and drift metrics."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from trading_mlops.config import MonitoringConfig, ValidationGateConfig
from trading_mlops.contracts import (
    Actor,
    ActorRole,
    FeatureSetContract,
    FeatureSpec,
    TrainingJobSpec,
)
from trading_mlops.errors import ErrorCode, GovernanceError
from trading_mlops.lineage import load_dataset_records, validate_and_split_chronologically
from trading_mlops.config import LineageConfig
from trading_mlops.monitoring import evaluate_drift_and_alerts
from trading_mlops.training import train_and_evaluate
from trading_mlops.validation import run_validation_gate

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO_ROOT / "data" / "fixtures"


def _default_fs() -> FeatureSetContract:
    return FeatureSetContract(
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


class TestTrainingValidationMonitoring(unittest.TestCase):
    def test_fixed_seed_reproducibility_and_cost_impact(self) -> None:
        fs = _default_fs()
        records, _ = load_dataset_records(FIXTURES / "market_features_v1.json")
        spec = TrainingJobSpec(
            idempotency_key="unit-repro-1",
            model_name="alpha_ridge_eq",
            model_version="1.0.0",
            strategy_id="strat_1",
            dataset_snapshot_key="syn_alpha_bars_v1@1.0.0+0000000000000000",
            feature_set_version_id=fs.version_id,
            code_commit="1234567890ab",
            seed=42,
            train_ratio=0.6,
            val_ratio=0.2,
            test_ratio=0.2,
            embargo_seconds=3600,
            hyperparams={"l2_penalty": 2.0, "signal_threshold_bps": 2.0},
            transaction_cost_bps=5.0,
        )
        split = validate_and_split_chronologically(records, fs, spec, LineageConfig())
        w1, e1 = train_and_evaluate(split.train, split.val, split.test, fs, spec)
        w2, e2 = train_and_evaluate(split.train, split.val, split.test, fs, spec)

        self.assertEqual(json.dumps(w1, sort_keys=True), json.dumps(w2, sort_keys=True))
        self.assertEqual(json.dumps(e1, sort_keys=True), json.dumps(e2, sort_keys=True))
        # Realistic transaction costs must strictly reduce net return compared to gross return when turnover > 0
        test_m = e1["splits"]["test"]
        self.assertGreater(test_m["avg_turnover"], 0.0)
        self.assertLess(test_m["mean_net_return_bps"], test_m["mean_gross_return_bps"])

    def test_validation_gate_enforces_multi_metric_and_role(self) -> None:
        fs = _default_fs()
        records, _ = load_dataset_records(FIXTURES / "market_features_v1.json")
        spec = TrainingJobSpec(
            idempotency_key="unit-val-1",
            model_name="alpha_ridge_eq",
            model_version="1.0.0",
            strategy_id="strat_1",
            dataset_snapshot_key="syn_alpha_bars_v1@1.0.0+0000000000000000",
            feature_set_version_id=fs.version_id,
            code_commit="1234567890ab",
            seed=42,
            train_ratio=0.6,
            val_ratio=0.2,
            test_ratio=0.2,
            embargo_seconds=3600,
            hyperparams={"l2_penalty": 2.0, "signal_threshold_bps": 2.0},
        )
        split = validate_and_split_chronologically(records, fs, spec, LineageConfig())
        weights, eval_manifest = train_and_evaluate(split.train, split.val, split.test, fs, spec)
        art = {"artifact_id": "art_1", "run_id": "run_1", "model_name": "alpha_ridge_eq", "model_version": "1.0.0"}
        bundle = {"bundle_sha256": "a" * 64, "weights": weights, "evaluation_manifest": eval_manifest}

        # Researcher cannot execute validation gate
        researcher = Actor("alice.quant", ActorRole.RESEARCHER)
        with self.assertRaises(GovernanceError) as ctx:
            run_validation_gate(art, bundle, researcher, ValidationGateConfig())
        self.assertEqual(ctx.exception.error_code, ErrorCode.UNAUTHORIZED_PROMOTION)

        # Strict drawdown threshold fails gate even when Sharpe is positive (anti-single-metric)
        validator = Actor("bob.validator", ActorRole.VALIDATOR)
        strict_cfg = ValidationGateConfig(max_drawdown_pct=0.00001)
        report = run_validation_gate(art, bundle, validator, strict_cfg)
        self.assertFalse(report.passed)
        failed_names = {c.check_name for c in report.failed_checks()}
        self.assertIn("max_drawdown_pct", failed_names)

    def test_drift_monitoring_detects_clean_vs_drifted_batches(self) -> None:
        fs = _default_fs()
        clean_records, _ = load_dataset_records(FIXTURES / "market_features_v1.json")
        drifted_records, _ = load_dataset_records(FIXTURES / "market_features_drifted.json")
        spec = TrainingJobSpec(
            idempotency_key="unit-drift-1",
            model_name="alpha_ridge_eq",
            model_version="1.0.0",
            strategy_id="strat_1",
            dataset_snapshot_key="syn_alpha_bars_v1@1.0.0+0000000000000000",
            feature_set_version_id=fs.version_id,
            code_commit="1234567890ab",
            seed=42,
            train_ratio=0.6,
            val_ratio=0.2,
            test_ratio=0.2,
            embargo_seconds=3600,
            hyperparams={"l2_penalty": 2.0},
        )
        split = validate_and_split_chronologically(clean_records, fs, spec, LineageConfig())
        weights, _ = train_and_evaluate(split.train, split.val, split.test, fs, spec)

        drifted_report = evaluate_drift_and_alerts(
            environment="paper",
            artifact_id="art_1",
            weights_payload=weights,
            recent_records=drifted_records,
            cfg=MonitoringConfig(),
        )
        self.assertEqual(drifted_report["overall_severity"], "CRITICAL")
        self.assertGreater(len(drifted_report["alerts"]), 0)
        self.assertFalse(drifted_report["automatic_retrain_permitted"])
        self.assertFalse(drifted_report["automatic_live_promotion_permitted"])


if __name__ == "__main__":
    unittest.main()
