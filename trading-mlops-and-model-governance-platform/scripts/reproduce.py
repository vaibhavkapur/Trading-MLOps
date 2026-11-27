#!/usr/bin/env python3
"""Reproduce M1-M4 milestone validations, deployment/recovery drill, and all 7 failure scenarios with retained evidence."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict

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
from trading_mlops.errors import ErrorCode, MlopsError  # noqa: E402
from trading_mlops.lineage import load_dataset_records  # noqa: E402
from trading_mlops.platform import MlopsPlatform  # noqa: E402


def _make_dataset(meta: Dict[str, Any], filename: str, actor_id: str) -> DatasetSnapshot:
    return DatasetSnapshot(
        dataset_id=meta["dataset_id"],
        version=meta["version"],
        source_uri=f"file://data/fixtures/{filename}",
        content_sha256=meta["content_sha256"],
        schema_hash=meta["schema_hash"],
        row_count=meta["row_count"],
        start_event_ts_utc=meta["start_event_ts_utc"],
        end_event_ts_utc=meta["end_event_ts_utc"],
        entitlement=meta["entitlement"],
        license_name=meta["license_name"],
        asset_class=meta["asset_class"],
        currency=meta["currency"],
        is_synthetic=meta["is_synthetic"],
        registered_by=actor_id,
    )


def _fs_v1() -> FeatureSetContract:
    return FeatureSetContract(
        feature_set_id="eq_micro_alpha_fs",
        version="1.0.0",
        owner="quant-feature-engineering",
        description="Causal 6h equity micro-alpha feature contract v1.",
        features=(
            FeatureSpec("order_book_imbalance", "1.0.0", "float64", "ratio", None, "ob_imb_v1", -10.0, 10.0),
            FeatureSpec("momentum_24h_bps", "1.0.0", "float64", "bps", "USD", "mom_24h_v1", -500.0, 500.0),
            FeatureSpec("realized_vol_24h_bps", "1.0.0", "float64", "bps", "USD", "rvol_24h_v1", 0.0, 500.0),
            FeatureSpec("bid_ask_spread_bps", "1.0.0", "float64", "bps", "USD", "spread_v1", 0.0, 100.0),
        ),
    )


def _fs_v2_breaking() -> FeatureSetContract:
    return FeatureSetContract(
        feature_set_id="eq_micro_alpha_fs",
        version="2.0.0",
        owner="quant-feature-engineering",
        description="Breaking MAJOR v2 feature contract.",
        features=(
            FeatureSpec("order_book_imbalance", "2.0.0", "float64", "ratio", None, "ob_imb_v2", -10.0, 10.0),
            FeatureSpec("momentum_24h_bps", "1.0.0", "float64", "bps", "USD", "mom_24h_v1", -500.0, 500.0),
            FeatureSpec("realized_vol_24h_bps", "1.0.0", "float64", "bps", "USD", "rvol_24h_v1", 0.0, 500.0),
            FeatureSpec("bid_ask_spread_bps", "1.0.0", "float64", "bps", "USD", "spread_v1", 0.0, 100.0),
        ),
    )


def main() -> None:
    fixtures_dir = REPO_ROOT / "data" / "fixtures"
    reports_dir = REPO_ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((fixtures_dir / "manifest.json").read_text(encoding="utf-8"))["fixtures"]
    cfg = PlatformConfig.load(REPO_ROOT / "config" / "platform_config.json")

    state_dir = REPO_ROOT / ".tmp_run" / "reproduce_state"
    if state_dir.exists():
        shutil.rmtree(state_dir)

    platform = MlopsPlatform(state_dir=state_dir, config=cfg)

    researcher = Actor("alice.quant", ActorRole.RESEARCHER, "quant-research")
    validator = Actor("bob.validator", ActorRole.VALIDATOR, "model-validation")
    risk_approver = Actor("carol.risk", ActorRole.RISK_APPROVER, "risk-management")
    compliance = Actor("frank.compliance", ActorRole.COMPLIANCE_OFFICER, "compliance")
    release_op = Actor("dave.release", ActorRole.RELEASE_OPERATOR, "trading-ops")
    auditor = Actor("erin.audit", ActorRole.GOVERNANCE_AUDITOR, "model-governance")

    # =========================================================================
    # M1: Foundation, Registry & Schema Contracts
    # =========================================================================
    ds_v1 = platform.register_dataset(_make_dataset(manifest["market_features_v1.json"], "market_features_v1.json", researcher.actor_id), researcher)
    ds_v2 = platform.register_dataset(_make_dataset(manifest["market_features_v2.json"], "market_features_v2.json", researcher.actor_id), researcher)
    ds_leaked = platform.register_dataset(_make_dataset(manifest["market_features_leaked.json"], "market_features_leaked.json", researcher.actor_id), researcher)

    fs_v1 = platform.register_feature_set(_fs_v1(), researcher)
    fs_v2_breaking = platform.register_feature_set(_fs_v2_breaking(), researcher)
    platform.bind_environment_feature_contract("shadow", fs_v1.version_id, release_op)
    platform.bind_environment_feature_contract("paper", fs_v1.version_id, release_op)
    platform.bind_environment_feature_contract("live_gated", fs_v1.version_id, release_op)

    m1_report = {
        "milestone": "M1",
        "status": "PASSED",
        "config_version": cfg.config_version,
        "order_execution_enabled_default": cfg.order_execution_enabled,
        "registered_datasets": [ds_v1.snapshot_key, ds_v2.snapshot_key, ds_leaked.snapshot_key],
        "registered_feature_sets": [fs_v1.version_id, fs_v2_breaking.version_id],
        "roles_defined": [r.value for r in ActorRole],
        "stages_defined": [s.value for s in ModelStage],
        "audit_chain": platform.audit_log.verify_chain(),
    }
    (reports_dir / "m1_foundation_report.json").write_text(
        json.dumps(m1_report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    # =========================================================================
    # M2: Reproducible Training, Signed Artifacts & Deterministic Parity
    # =========================================================================
    spec_v1 = TrainingJobSpec(
        idempotency_key="prod-cand-v1-seed42",
        model_name="alpha_ridge_eq",
        model_version="1.0.0",
        strategy_id="strat_eq_alpha_01",
        dataset_snapshot_key=ds_v1.snapshot_key,
        feature_set_version_id=fs_v1.version_id,
        code_commit="c0ffee123456",
        seed=42,
        train_ratio=0.60,
        val_ratio=0.20,
        test_ratio=0.20,
        embargo_seconds=3600,
        hyperparams={"l2_penalty": 2.0, "signal_threshold_bps": 2.0},
    )
    run_v1, art_v1, replay_1 = platform.run_training_job(spec_v1, fixtures_dir / "market_features_v1.json", researcher)
    assert art_v1 is not None and not replay_1

    # Reproducibility check in separate isolated store with identical spec -> byte-identical bundle SHA-256
    alt_state = REPO_ROOT / ".tmp_run" / "repro_check_state"
    if alt_state.exists():
        shutil.rmtree(alt_state)
    alt_platform = MlopsPlatform(state_dir=alt_state, config=cfg)
    alt_platform.register_dataset(ds_v1, researcher)
    alt_platform.register_feature_set(fs_v1, researcher)
    _, art_v1_repro, _ = alt_platform.run_training_job(spec_v1, fixtures_dir / "market_features_v1.json", researcher)
    assert art_v1_repro is not None
    byte_identical_reproducibility = (
        art_v1["bundle_sha256"] == art_v1_repro["bundle_sha256"]
        and art_v1["signature"] == art_v1_repro["signature"]
    )
    assert byte_identical_reproducibility

    val_v1, card_v1 = platform.validate_artifact(art_v1["artifact_id"], validator)
    assert val_v1.passed

    # Train v2 candidate
    spec_v2 = TrainingJobSpec(
        idempotency_key="prod-cand-v2-seed43",
        model_name="alpha_ridge_eq",
        model_version="2.0.0",
        strategy_id="strat_eq_alpha_01",
        dataset_snapshot_key=ds_v2.snapshot_key,
        feature_set_version_id=fs_v1.version_id,
        code_commit="c0ffee987654",
        seed=43,
        train_ratio=0.60,
        val_ratio=0.20,
        test_ratio=0.20,
        embargo_seconds=3600,
        hyperparams={"l2_penalty": 2.5, "signal_threshold_bps": 2.0},
    )
    run_v2, art_v2, _ = platform.run_training_job(spec_v2, fixtures_dir / "market_features_v2.json", researcher)
    assert art_v2 is not None
    val_v2, card_v2 = platform.validate_artifact(art_v2["artifact_id"], validator)
    assert val_v2.passed

    m2_report = {
        "milestone": "M2",
        "status": "PASSED",
        "byte_identical_reproducibility": byte_identical_reproducibility,
        "v1_artifact_id": art_v1["artifact_id"],
        "v1_bundle_sha256": art_v1["bundle_sha256"],
        "v1_validation_passed": val_v1.passed,
        "v1_test_metrics": run_v1.metrics,
        "v1_uncertainty": val_v1.uncertainty,
        "v1_model_card_id": card_v1.card_id,
        "v2_artifact_id": art_v2["artifact_id"],
        "v2_bundle_sha256": art_v2["bundle_sha256"],
        "v2_validation_passed": val_v2.passed,
        "v2_test_metrics": run_v2.metrics,
        "v1_lineage_complete": platform.inspect_lineage(art_v1["artifact_id"])["complete"],
    }
    (reports_dir / "m2_training_validation_report.json").write_text(
        json.dumps(m2_report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    # =========================================================================
    # M3: Staged Promotion, Independent Approval, Shadow/Paper & Rollback
    # =========================================================================
    platform.approve_promotion(art_v1["artifact_id"], ModelStage.SHADOW, risk_approver, "Approved v1 for shadow.")
    dep_v1_shadow = platform.promote_artifact(art_v1["artifact_id"], ModelStage.SHADOW, "shadow", release_op)
    platform.approve_promotion(art_v1["artifact_id"], ModelStage.PAPER, risk_approver, "Approved v1 for paper.")
    dep_v1_paper = platform.promote_artifact(art_v1["artifact_id"], ModelStage.PAPER, "paper", release_op)

    # Promote v2 through SHADOW -> PAPER alongside v1 shadow comparison
    platform.approve_promotion(art_v2["artifact_id"], ModelStage.SHADOW, risk_approver, "Approved v2 for shadow.")
    dep_v2_shadow = platform.promote_artifact(art_v2["artifact_id"], ModelStage.SHADOW, "shadow", release_op)
    clean_v2_records, _ = load_dataset_records(fixtures_dir / "market_features_v2.json")
    shadow_drift, _ = platform.monitor_deployment(
        "shadow",
        "alpha_ridge_eq",
        clean_v2_records[-50:],
        auditor,
        champion_artifact_id=art_v1["artifact_id"],
    )

    platform.approve_promotion(art_v2["artifact_id"], ModelStage.PAPER, risk_approver, "Approved v2 for paper after shadow comparison.")
    dep_v2_paper = platform.promote_artifact(art_v2["artifact_id"], ModelStage.PAPER, "paper", release_op)

    # Verify LIVE_GATED requires dual independent approvals and still blocks live order execution
    platform.approve_promotion(art_v2["artifact_id"], ModelStage.LIVE_GATED, risk_approver, "Risk sign-off for gated review.")
    platform.approve_promotion(art_v2["artifact_id"], ModelStage.LIVE_GATED, compliance, "Compliance sign-off (execution remains disabled).")
    live_exec_blocked_code = None
    try:
        platform.promote_artifact(art_v2["artifact_id"], ModelStage.LIVE_GATED, "live_gated", release_op, enable_order_execution=True)
    except MlopsError as exc:
        live_exec_blocked_code = exc.error_code.value
    assert live_exec_blocked_code == ErrorCode.EXECUTION_DISABLED_GUARD.value

    dep_v2_live_gated = platform.promote_artifact(
        art_v2["artifact_id"], ModelStage.LIVE_GATED, "live_gated", release_op, enable_order_execution=False
    )

    m3_report = {
        "milestone": "M3",
        "status": "PASSED",
        "v1_shadow_deployment": dep_v1_shadow.deployment_id,
        "v1_paper_deployment": dep_v1_paper.deployment_id,
        "v2_shadow_deployment": dep_v2_shadow.deployment_id,
        "v2_shadow_comparison": shadow_drift["shadow_comparison"],
        "v2_paper_deployment": dep_v2_paper.deployment_id,
        "v2_live_gated_deployment": dep_v2_live_gated.deployment_id,
        "v2_live_gated_approvals": list(dep_v2_live_gated.approval_ids),
        "live_real_capital_execution_blocked_code": live_exec_blocked_code,
        "inference_manifest_export_sha256": platform.export_inference_manifest(art_v2["artifact_id"])["artifact_sha256"],
    }
    (reports_dir / "m3_promotion_rollback_report.json").write_text(
        json.dumps(m3_report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    # =========================================================================
    # M4: Drift Monitoring, Alerting, Governance Review & Recovery Drill
    # =========================================================================
    drifted_records, _ = load_dataset_records(fixtures_dir / "market_features_drifted.json")
    drift_report, obs_event = platform.monitor_deployment("paper", "alpha_ridge_eq", drifted_records, auditor)
    assert drift_report["overall_severity"] == "CRITICAL"

    rollback_rec, restored_dep = platform.rollback_deployment(
        environment="paper",
        model_name="alpha_ridge_eq",
        operator=release_op,
        reason="Recovery drill triggered by CRITICAL feature PSI and prediction drift alerts.",
    )
    assert restored_dep.artifact_id == art_v1["artifact_id"]
    assert restored_dep.model_version == "1.0.0"

    gov_review = platform.governance_review()
    assert gov_review["audit_chain"]["verified"] is True
    assert gov_review["all_deployed_lineage_complete"] is True
    assert gov_review["any_live_execution_enabled"] is False

    m4_report = {
        "milestone": "M4",
        "status": "PASSED",
        "drift_report_id": drift_report["drift_report_id"],
        "drift_overall_severity": drift_report["overall_severity"],
        "drift_alert_count": len(drift_report["alerts"]),
        "recommended_safe_fallback": drift_report["recommended_safe_fallback"],
        "automatic_retrain_permitted": drift_report["automatic_retrain_permitted"],
        "observability_event": obs_event,
        "recovery_drill_rollback": rollback_rec.to_dict(),
        "restored_active_version": restored_dep.model_version,
        "governance_review": gov_review,
    }
    (reports_dir / "m4_drift_and_recovery_drill_report.json").write_text(
        json.dumps(m4_report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    # =========================================================================
    # All 7 Required Failure Scenarios Evidence
    # =========================================================================
    scenarios: Dict[str, Any] = {}

    # 1. Data leakage
    leak_spec = TrainingJobSpec(
        idempotency_key="scenario-leakage-1",
        model_name="alpha_leaked_test",
        model_version="1.0.0",
        strategy_id="strat_eq_alpha_01",
        dataset_snapshot_key=ds_leaked.snapshot_key,
        feature_set_version_id=fs_v1.version_id,
        code_commit="c0ffee123456",
        seed=42,
        train_ratio=0.60,
        val_ratio=0.20,
        test_ratio=0.20,
        embargo_seconds=3600,
        hyperparams={"l2_penalty": 2.0},
    )
    try:
        platform.run_training_job(leak_spec, fixtures_dir / "market_features_leaked.json", researcher)
        raise AssertionError("Expected DataLeakageError")
    except MlopsError as exc:
        failed_runs = [r for r in platform.registry.list_experiment_runs() if r.idempotency_key == "scenario-leakage-1"]
        scenarios["data_leakage"] = {
            "passed": exc.error_code == ErrorCode.DATA_LEAKAGE_DETECTED and len(failed_runs) == 1,
            "error_code": exc.error_code.value,
            "message": exc.message,
            "failed_experiment_recorded": len(failed_runs) == 1,
            "recorded_status": failed_runs[0].status.value if failed_runs else None,
        }

    # 2. Artifact tampering
    tamper_state = REPO_ROOT / ".tmp_run" / "scenario_tamper_state"
    if tamper_state.exists():
        shutil.rmtree(tamper_state)
    tp = MlopsPlatform(state_dir=tamper_state, config=cfg)
    tp.register_dataset(ds_v1, researcher)
    tp.register_feature_set(fs_v1, researcher)
    _, t_art, _ = tp.run_training_job(spec_v1, fixtures_dir / "market_features_v1.json", researcher)
    assert t_art is not None
    tp.validate_artifact(t_art["artifact_id"], validator)
    tp.approve_promotion(t_art["artifact_id"], ModelStage.SHADOW, risk_approver, "Approved prior to tampering.")
    weights_file = Path(t_art["bundle_path"]) / "weights.json"
    tampered = json.loads(weights_file.read_text(encoding="utf-8"))
    tampered["bias"] = 9999.0
    weights_file.write_text(json.dumps(tampered), encoding="utf-8")
    try:
        tp.promote_artifact(t_art["artifact_id"], ModelStage.SHADOW, "shadow", release_op)
        raise AssertionError("Expected ArtifactIntegrityError")
    except MlopsError as exc:
        scenarios["artifact_tampering"] = {
            "passed": exc.error_code == ErrorCode.ARTIFACT_TAMPERING_DETECTED,
            "error_code": exc.error_code.value,
            "message": exc.message,
        }

    # 3. Failed validation blocks promotion
    bad_spec = TrainingJobSpec(
        idempotency_key="scenario-failed-val-1",
        model_name="alpha_inverted_bad",
        model_version="1.0.0",
        strategy_id="strat_eq_alpha_01",
        dataset_snapshot_key=ds_v1.snapshot_key,
        feature_set_version_id=fs_v1.version_id,
        code_commit="c0ffee123456",
        seed=42,
        train_ratio=0.60,
        val_ratio=0.20,
        test_ratio=0.20,
        embargo_seconds=3600,
        hyperparams={"l2_penalty": 2.0, "signal_multiplier": -1.0},  # Inverted losing signal
    )
    _, bad_art, _ = platform.run_training_job(bad_spec, fixtures_dir / "market_features_v1.json", researcher)
    assert bad_art is not None
    bad_val, _ = platform.validate_artifact(bad_art["artifact_id"], validator)
    try:
        platform.approve_promotion(bad_art["artifact_id"], ModelStage.SHADOW, risk_approver, "Attempt approve failed model")
        raise AssertionError("Expected ValidationGateError")
    except MlopsError as exc:
        scenarios["failed_validation"] = {
            "passed": (not bad_val.passed) and exc.error_code == ErrorCode.VALIDATION_GATE_FAILED,
            "validation_passed": bad_val.passed,
            "failed_checks": [c.check_name for c in bad_val.failed_checks()],
            "promotion_error_code": exc.error_code.value,
        }

    # 4. Unauthorized promotion (wrong role + self-approval)
    unauth_role_code = None
    try:
        platform.promote_artifact(art_v1["artifact_id"], ModelStage.SHADOW, "shadow", researcher)
    except MlopsError as exc:
        unauth_role_code = exc.error_code.value

    self_approver = Actor("alice.quant", ActorRole.RISK_APPROVER, "risk-management")
    self_approve_code = None
    try:
        platform.approve_promotion(art_v1["artifact_id"], ModelStage.SHADOW, self_approver, "Self approval attempt")
    except MlopsError as exc:
        self_approve_code = exc.error_code.value

    scenarios["unauthorized_promotion"] = {
        "passed": (
            unauth_role_code == ErrorCode.UNAUTHORIZED_PROMOTION.value
            and self_approve_code == ErrorCode.SELF_APPROVAL_FORBIDDEN.value
        ),
        "wrong_role_error_code": unauth_role_code,
        "self_approval_error_code": self_approve_code,
    }

    # 5. Incompatible feature version
    incompat_spec = TrainingJobSpec(
        idempotency_key="scenario-incompat-fs-1",
        model_name="alpha_fs_v2_model",
        model_version="1.0.0",
        strategy_id="strat_eq_alpha_01",
        dataset_snapshot_key=ds_v1.snapshot_key,
        feature_set_version_id=fs_v2_breaking.version_id,
        code_commit="c0ffee123456",
        seed=42,
        train_ratio=0.60,
        val_ratio=0.20,
        test_ratio=0.20,
        embargo_seconds=3600,
        hyperparams={"l2_penalty": 2.0, "signal_threshold_bps": 2.0},
    )
    _, incompat_art, _ = platform.run_training_job(incompat_spec, fixtures_dir / "market_features_v1.json", researcher)
    assert incompat_art is not None
    incompat_val, _ = platform.validate_artifact(incompat_art["artifact_id"], validator)
    assert incompat_val.passed
    platform.approve_promotion(incompat_art["artifact_id"], ModelStage.SHADOW, risk_approver, "Risk approved; awaiting feature compat check.")
    try:
        platform.promote_artifact(incompat_art["artifact_id"], ModelStage.SHADOW, "shadow", release_op)
        raise AssertionError("Expected FeatureCompatibilityError")
    except MlopsError as exc:
        scenarios["incompatible_feature_version"] = {
            "passed": exc.error_code == ErrorCode.INCOMPATIBLE_FEATURE_VERSION,
            "error_code": exc.error_code.value,
            "message": exc.message,
        }

    # 6. Rollback (evidence from M4 recovery drill)
    active_after_rb = platform.registry.get_active_deployment("paper", "alpha_ridge_eq")
    scenarios["rollback"] = {
        "passed": (
            active_after_rb is not None
            and active_after_rb.artifact_id == art_v1["artifact_id"]
            and rollback_rec.verified_integrity
        ),
        "rollback_id": rollback_rec.rollback_id,
        "from_artifact_id": rollback_rec.from_artifact_id,
        "restored_artifact_id": rollback_rec.restored_artifact_id,
        "restored_model_version": rollback_rec.restored_model_version,
    }

    # 7. Repeated job (idempotent replay + conflict rejection)
    replay_run, replay_art, was_replay = platform.run_training_job(spec_v1, fixtures_dir / "market_features_v1.json", researcher)
    conflict_spec = TrainingJobSpec(
        idempotency_key=spec_v1.idempotency_key,  # Same key, different seed!
        model_name="alpha_ridge_eq",
        model_version="1.0.0",
        strategy_id="strat_eq_alpha_01",
        dataset_snapshot_key=ds_v1.snapshot_key,
        feature_set_version_id=fs_v1.version_id,
        code_commit="c0ffee123456",
        seed=999,
        train_ratio=0.60,
        val_ratio=0.20,
        test_ratio=0.20,
        embargo_seconds=3600,
        hyperparams={"l2_penalty": 2.0, "signal_threshold_bps": 2.0},
    )
    conflict_code = None
    try:
        platform.run_training_job(conflict_spec, fixtures_dir / "market_features_v1.json", researcher)
    except MlopsError as exc:
        conflict_code = exc.error_code.value

    scenarios["repeated_job"] = {
        "passed": (
            was_replay
            and replay_run.run_id == run_v1.run_id
            and replay_art is not None
            and replay_art["bundle_sha256"] == art_v1["bundle_sha256"]
            and conflict_code == ErrorCode.IDEMPOTENCY_CONFLICT.value
        ),
        "idempotent_replay_detected": was_replay,
        "same_run_id": replay_run.run_id == run_v1.run_id,
        "same_bundle_sha256": replay_art["bundle_sha256"] == art_v1["bundle_sha256"] if replay_art else False,
        "conflicting_spec_error_code": conflict_code,
    }

    assert all(s["passed"] for s in scenarios.values()), f"Scenario failed: {scenarios}"
    (reports_dir / "scenarios_evidence.json").write_text(
        json.dumps(
            {
                "all_scenarios_passed": True,
                "scenario_count": len(scenarios),
                "scenarios": scenarios,
                "final_audit_chain": platform.audit_log.verify_chain(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    # Write concise human-readable Markdown summary
    md_lines = [
        "# Validation and Acceptance Evidence Summary",
        "",
        "| Milestone / Scenario | Status | Key Evidence |",
        "|---|---|---|",
        f"| M1 Foundation & Registry | PASSED | {len(m1_report['registered_datasets'])} datasets, {len(m1_report['registered_feature_sets'])} feature sets, `order_execution_enabled=false` |",
        f"| M2 Training & Signed Artifacts | PASSED | Byte-identical reproducibility=`{m2_report['byte_identical_reproducibility']}`, v1 net Sharpe=`{m2_report['v1_test_metrics']['net_sharpe']}` (n={m2_report['v1_test_metrics']['sample_size']}) |",
        f"| M3 Staged Promotion & RBAC | PASSED | SHADOW->PAPER->LIVE_GATED (dual sign-off), live execution blocked=`{m3_report['live_real_capital_execution_blocked_code']}` |",
        f"| M4 Drift & Recovery Drill | PASSED | Drift severity=`{m4_report['drift_overall_severity']}` ({m4_report['drift_alert_count']} alerts), rollback restored `v{m4_report['restored_active_version']}`, audit chain verified |",
    ]
    for name, info in scenarios.items():
        md_lines.append(f"| Scenario: `{name}` | {'PASSED' if info['passed'] else 'FAILED'} | `{json.dumps(info, sort_keys=True)}` |")
    (reports_dir / "validation_summary.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print("Reproduced M1-M4 and all 7 failure scenarios successfully. Reports saved in reports/.")


if __name__ == "__main__":
    main()
