#!/usr/bin/env python3
"""Runnable end-to-end demonstration of the Trading MLOps and Model Governance Platform (M1-M4)."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from trading_mlops.config import PlatformConfig  # noqa: E402
from trading_mlops.contracts import (  # noqa: E402
    Actor,
    ActorRole,
    DatasetSnapshot,
    FeatureSetContract,
    FeatureSpec,
    ModelStage,
    TrainingJobSpec,
)
from trading_mlops.lineage import load_dataset_records  # noqa: E402
from trading_mlops.platform import MlopsPlatform  # noqa: E402


def build_default_feature_set(version: str = "1.0.0") -> FeatureSetContract:
    return FeatureSetContract(
        feature_set_id="eq_micro_alpha_fs",
        version=version,
        owner="quant-feature-engineering",
        description="Causal 6h equity micro-alpha feature contract compatible with Project #18.",
        features=(
            FeatureSpec("order_book_imbalance", "1.0.0", "float64", "ratio", None, "ob_imb_v1", -10.0, 10.0),
            FeatureSpec("momentum_24h_bps", "1.0.0", "float64", "bps", "USD", "mom_24h_v1", -500.0, 500.0),
            FeatureSpec("realized_vol_24h_bps", "1.0.0", "float64", "bps", "USD", "rvol_24h_v1", 0.0, 500.0),
            FeatureSpec("bid_ask_spread_bps", "1.0.0", "float64", "bps", "USD", "spread_v1", 0.0, 100.0),
        ),
    )


def main() -> None:
    fixtures_dir = REPO_ROOT / "data" / "fixtures"
    manifest = json.loads((fixtures_dir / "manifest.json").read_text(encoding="utf-8"))["fixtures"]
    cfg = PlatformConfig.load(REPO_ROOT / "config" / "platform_config.json")

    state_dir = REPO_ROOT / ".tmp_run" / "demo_state"
    if state_dir.exists():
        shutil.rmtree(state_dir)

    platform = MlopsPlatform(state_dir=state_dir, config=cfg)

    researcher = Actor("alice.quant", ActorRole.RESEARCHER, "quant-research")
    validator = Actor("bob.validator", ActorRole.VALIDATOR, "model-validation")
    risk_approver = Actor("carol.risk", ActorRole.RISK_APPROVER, "risk-management")
    release_op = Actor("dave.release", ActorRole.RELEASE_OPERATOR, "trading-ops")
    auditor = Actor("erin.audit", ActorRole.GOVERNANCE_AUDITOR, "model-governance")

    # M1: Register dataset & feature contract
    v1_meta = manifest["market_features_v1.json"]
    ds_v1 = DatasetSnapshot(
        dataset_id=v1_meta["dataset_id"],
        version=v1_meta["version"],
        source_uri="file://data/fixtures/market_features_v1.json",
        content_sha256=v1_meta["content_sha256"],
        schema_hash=v1_meta["schema_hash"],
        row_count=v1_meta["row_count"],
        start_event_ts_utc=v1_meta["start_event_ts_utc"],
        end_event_ts_utc=v1_meta["end_event_ts_utc"],
        entitlement=v1_meta["entitlement"],
        license_name=v1_meta["license_name"],
        asset_class=v1_meta["asset_class"],
        currency=v1_meta["currency"],
        is_synthetic=True,
        registered_by=researcher.actor_id,
    )
    platform.register_dataset(ds_v1, researcher)
    fs_v1 = build_default_feature_set("1.0.0")
    platform.register_feature_set(fs_v1, researcher)
    platform.bind_environment_feature_contract("shadow", fs_v1.version_id, release_op)
    platform.bind_environment_feature_contract("paper", fs_v1.version_id, release_op)

    # M2: Reproducible training + signed artifact
    spec_v1 = TrainingJobSpec(
        idempotency_key="demo-alpha-v1-seed42",
        model_name="alpha_ridge_eq",
        model_version="1.0.0",
        strategy_id="strat_eq_alpha_01",
        dataset_snapshot_key=ds_v1.snapshot_key,
        feature_set_version_id=fs_v1.version_id,
        code_commit="a1b2c3d4e5f6",
        seed=42,
        train_ratio=0.60,
        val_ratio=0.20,
        test_ratio=0.20,
        embargo_seconds=3600,
        hyperparams={"l2_penalty": 2.0, "signal_threshold_bps": 2.0},
    )
    run_v1, art_v1, _ = platform.run_training_job(spec_v1, fixtures_dir / "market_features_v1.json", researcher)
    assert art_v1 is not None

    # M3: Multi-metric validation gate, Model Card, independent approval, SHADOW -> PAPER promotion
    val_report, card = platform.validate_artifact(art_v1["artifact_id"], validator)
    assert val_report.passed, f"Expected v1 validation pass, failed: {val_report.failed_checks()}"

    platform.approve_promotion(art_v1["artifact_id"], ModelStage.SHADOW, risk_approver, "OOS net Sharpe and drawdown within limits.")
    shadow_dep = platform.promote_artifact(art_v1["artifact_id"], ModelStage.SHADOW, "shadow", release_op, notes="Initial shadow run")

    platform.approve_promotion(art_v1["artifact_id"], ModelStage.PAPER, risk_approver, "Shadow stability reviewed; cleared for paper sandbox (execution disabled).")
    paper_dep = platform.promote_artifact(art_v1["artifact_id"], ModelStage.PAPER, "paper", release_op, enable_order_execution=False, notes="Paper deployment")

    # M4: Drift check on shifted data + governance audit review
    drifted_records, _ = load_dataset_records(fixtures_dir / "market_features_drifted.json")
    drift_report, obs_event = platform.monitor_deployment("paper", "alpha_ridge_eq", drifted_records, auditor)
    review = platform.governance_review()

    summary = {
        "m1_dataset_snapshot": ds_v1.snapshot_key,
        "m1_feature_set": fs_v1.version_id,
        "m2_run_id": run_v1.run_id,
        "m2_artifact_id": art_v1["artifact_id"],
        "m2_bundle_sha256": art_v1["bundle_sha256"],
        "m2_test_net_sharpe": run_v1.metrics["net_sharpe"],
        "m3_validation_passed": val_report.passed,
        "m3_model_card_id": card.card_id,
        "m3_shadow_deployment_id": shadow_dep.deployment_id,
        "m3_paper_deployment_id": paper_dep.deployment_id,
        "m3_order_execution_enabled": paper_dep.order_execution_enabled,
        "m4_drift_severity": drift_report["overall_severity"],
        "m4_drift_alerts": len(drift_report["alerts"]),
        "m4_recommended_safe_fallback": drift_report["recommended_safe_fallback"],
        "m4_observability_schema": obs_event["schema"],
        "m4_audit_chain_verified": review["audit_chain"]["verified"],
        "m4_all_deployed_lineage_complete": review["all_deployed_lineage_complete"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
