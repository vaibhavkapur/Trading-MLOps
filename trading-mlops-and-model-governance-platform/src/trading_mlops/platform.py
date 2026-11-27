"""Unified high-level facade for the Trading MLOps and Model Governance Platform."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from trading_mlops.adapters import (
    InferenceEngineManifestAdapter,
    MLflowTrackingStubAdapter,
    ObservabilityEventAdapter,
)
from trading_mlops.artifacts import LocalArtifactStore
from trading_mlops.audit import AuditLog
from trading_mlops.config import PlatformConfig
from trading_mlops.contracts import (
    Actor,
    ActorRole,
    ApprovalRecord,
    DatasetSnapshot,
    DeploymentRecord,
    ExperimentRun,
    FeatureSetContract,
    ModelCard,
    ModelStage,
    RollbackRecord,
    RunStatus,
    TrainingJobSpec,
    ValidationReport,
    sha256_canonical,
    utc_now_iso,
)
from trading_mlops.errors import (
    DataLeakageError,
    ErrorCode,
    GovernanceError,
    IdempotencyConflictError,
    MlopsError,
)
from trading_mlops.governance import GovernanceController
from trading_mlops.lineage import (
    build_lineage_graph,
    load_dataset_records,
    validate_and_split_chronologically,
)
from trading_mlops.model_card import generate_model_card, render_model_card_markdown
from trading_mlops.monitoring import evaluate_drift_and_alerts
from trading_mlops.registry import SQLiteRegistryStore
from trading_mlops.training import train_and_evaluate
from trading_mlops.validation import run_validation_gate


class MlopsPlatform:
    """Orchestrates M1-M4 MLOps and model governance workflows with strict auditability."""

    def __init__(
        self,
        state_dir: Path,
        config: Optional[PlatformConfig] = None,
        external_tracker: Optional[MLflowTrackingStubAdapter] = None,
    ) -> None:
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config = config or PlatformConfig()
        self.registry = SQLiteRegistryStore(self.state_dir / "registry.sqlite")
        self.artifact_store = LocalArtifactStore(
            self.state_dir / "artifacts", self.config.signing
        )
        self.audit_log = AuditLog(self.state_dir / "audit_log.jsonl")
        self.governance = GovernanceController(
            registry=self.registry,
            artifact_store=self.artifact_store,
            audit_log=self.audit_log,
            config=self.config,
        )
        self.external_tracker = external_tracker or MLflowTrackingStubAdapter()

    # --- M1: Registry & Contracts ---

    def register_dataset(self, snapshot: DatasetSnapshot, actor: Actor) -> DatasetSnapshot:
        self.registry.save_dataset(snapshot)
        self.audit_log.record(
            event_type="DATASET_REGISTERED",
            actor=actor,
            entity_type="dataset",
            entity_id=snapshot.snapshot_key,
            outcome="SUCCESS",
            details={
                "content_sha256": snapshot.content_sha256,
                "entitlement": snapshot.entitlement,
                "license_name": snapshot.license_name,
                "is_synthetic": snapshot.is_synthetic,
            },
        )
        return snapshot

    def register_feature_set(self, contract: FeatureSetContract, actor: Actor) -> FeatureSetContract:
        self.registry.save_feature_set(contract)
        self.audit_log.record(
            event_type="FEATURE_SET_REGISTERED",
            actor=actor,
            entity_type="feature_set",
            entity_id=contract.version_id,
            outcome="SUCCESS",
            details={
                "feature_set_id": contract.feature_set_id,
                "version": contract.version,
                "schema_hash": contract.schema_hash,
                "features": contract.feature_names(),
            },
        )
        return contract

    def bind_environment_feature_contract(
        self, environment: str, feature_set_version_id: str, actor: Actor
    ) -> None:
        now = utc_now_iso()
        self.registry.set_environment_feature_contract(environment, feature_set_version_id, now)
        self.audit_log.record(
            event_type="ENV_FEATURE_CONTRACT_BOUND",
            actor=actor,
            entity_type="environment",
            entity_id=environment,
            outcome="SUCCESS",
            details={"feature_set_version_id": feature_set_version_id},
        )

    # --- M2: Reproducible Training & Signed Artifacts ---

    def run_training_job(
        self,
        job_spec: TrainingJobSpec,
        dataset_path: Path,
        actor: Actor,
    ) -> Tuple[ExperimentRun, Optional[Dict[str, Any]], bool]:
        """Run an idempotent, reproducible training job. Returns (run, artifact_record_or_none, was_idempotent_replay)."""
        if actor.role not in {ActorRole.RESEARCHER, ActorRole.RELEASE_OPERATOR}:
            raise GovernanceError(
                ErrorCode.UNAUTHORIZED_PROMOTION,
                f"Actor '{actor.actor_id}' with role '{actor.role.value}' cannot launch training jobs.",
                {"actor_id": actor.actor_id, "role": actor.role.value},
            )

        # Idempotency check
        existing = self.registry.get_run_by_idempotency_key(job_spec.idempotency_key)
        if existing is not None:
            if existing.spec_hash != job_spec.spec_hash:
                self.audit_log.record(
                    event_type="TRAINING_JOB_SUBMITTED",
                    actor=actor,
                    entity_type="training_job",
                    entity_id=job_spec.idempotency_key,
                    outcome="BLOCKED",
                    details={"reason": "IDEMPOTENCY_CONFLICT"},
                )
                raise IdempotencyConflictError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    f"Repeated job idempotency_key '{job_spec.idempotency_key}' has conflicting training spec.",
                    {
                        "idempotency_key": job_spec.idempotency_key,
                        "existing_spec_hash": existing.spec_hash,
                        "new_spec_hash": job_spec.spec_hash,
                    },
                )
            existing_art = (
                self.registry.get_artifact_record(existing.artifact_id)
                if existing.artifact_id
                else None
            )
            self.audit_log.record(
                event_type="TRAINING_JOB_IDEMPOTENT_REPLAY",
                actor=actor,
                entity_type="experiment_run",
                entity_id=existing.run_id,
                outcome="SUCCESS",
                details={"idempotency_key": job_spec.idempotency_key, "artifact_id": existing.artifact_id},
            )
            return existing, existing_art, True

        dataset = self.registry.get_dataset(job_spec.dataset_snapshot_key)
        feature_contract = self.registry.get_feature_set(job_spec.feature_set_version_id)
        records, _ = load_dataset_records(dataset_path, expected_sha256=dataset.content_sha256)

        now = utc_now_iso()
        run_id = "run_" + sha256_canonical(
            {"idempotency_key": job_spec.idempotency_key, "spec_hash": job_spec.spec_hash}
        )[:16]

        # Enforce point-in-time, leakage, and anti-blind-retrain policies; record failed experiments!
        try:
            split = validate_and_split_chronologically(
                records=records,
                feature_contract=feature_contract,
                job_spec=job_spec,
                lineage_cfg=self.config.lineage,
            )
        except DataLeakageError as exc:
            status = (
                RunStatus.REJECTED_POLICY
                if exc.error_code == ErrorCode.BLIND_RETRAINING_POLICY_VIOLATION
                else RunStatus.REJECTED_LEAKAGE
            )
            failed_run = ExperimentRun(
                run_id=run_id,
                idempotency_key=job_spec.idempotency_key,
                spec_hash=job_spec.spec_hash,
                model_name=job_spec.model_name,
                model_version=job_spec.model_version,
                strategy_id=job_spec.strategy_id,
                dataset_snapshot_key=job_spec.dataset_snapshot_key,
                feature_set_version_id=job_spec.feature_set_version_id,
                code_commit=job_spec.code_commit,
                seed=job_spec.seed,
                created_by=actor.actor_id,
                created_at_utc=now,
                status=status,
                metrics={},
                split_summary={},
                artifact_id=None,
                failure_code=exc.error_code.value,
                failure_reason=exc.message,
            )
            self.registry.save_experiment_run(failed_run)
            self.external_tracker.log_run(failed_run.to_dict())
            self.audit_log.record(
                event_type="TRAINING_JOB_REJECTED",
                actor=actor,
                entity_type="experiment_run",
                entity_id=run_id,
                outcome="FAILED",
                details={"error_code": exc.error_code.value, "reason": exc.message},
            )
            raise

        weights_payload, evaluation_manifest = train_and_evaluate(
            train_records=split.train,
            val_records=split.val,
            test_records=split.test,
            feature_contract=feature_contract,
            job_spec=job_spec,
        )

        # Deterministic content-addressed artifact ID
        artifact_id = f"art_{job_spec.model_name}_{job_spec.model_version.replace('.', '_')}_{job_spec.spec_hash[:10]}"
        manifest = {
            "artifact_id": artifact_id,
            "run_id": run_id,
            "model_name": job_spec.model_name,
            "model_version": job_spec.model_version,
            "strategy_id": job_spec.strategy_id,
            "dataset_snapshot_key": job_spec.dataset_snapshot_key,
            "feature_set_version_id": job_spec.feature_set_version_id,
            "code_commit": job_spec.code_commit,
            "seed": job_spec.seed,
            "spec_hash": job_spec.spec_hash,
            "split_summary": split.summary,
        }
        bundle_dir, bundle_sha256, signature_hex = self.artifact_store.write_bundle(
            artifact_id=artifact_id,
            manifest=manifest,
            weights=weights_payload,
            evaluation_manifest=evaluation_manifest,
        )

        run = ExperimentRun(
            run_id=run_id,
            idempotency_key=job_spec.idempotency_key,
            spec_hash=job_spec.spec_hash,
            model_name=job_spec.model_name,
            model_version=job_spec.model_version,
            strategy_id=job_spec.strategy_id,
            dataset_snapshot_key=job_spec.dataset_snapshot_key,
            feature_set_version_id=job_spec.feature_set_version_id,
            code_commit=job_spec.code_commit,
            seed=job_spec.seed,
            created_by=actor.actor_id,
            created_at_utc=now,
            status=RunStatus.SUCCEEDED,
            metrics=evaluation_manifest["splits"]["test"],
            split_summary=split.summary,
            artifact_id=artifact_id,
        )
        artifact_record = {
            "artifact_id": artifact_id,
            "run_id": run_id,
            "model_name": job_spec.model_name,
            "model_version": job_spec.model_version,
            "strategy_id": job_spec.strategy_id,
            "stage": ModelStage.REGISTERED.value,
            "bundle_sha256": bundle_sha256,
            "signature": signature_hex,
            "bundle_path": str(bundle_dir),
            "dataset_snapshot_key": job_spec.dataset_snapshot_key,
            "feature_set_version_id": job_spec.feature_set_version_id,
            "code_commit": job_spec.code_commit,
            "created_by": actor.actor_id,
            "created_at_utc": now,
        }

        self.registry.save_experiment_run(run)
        self.registry.save_artifact_record(artifact_record)
        self.external_tracker.log_run(run.to_dict())
        self.audit_log.record(
            event_type="TRAINING_JOB_COMPLETED",
            actor=actor,
            entity_type="artifact",
            entity_id=artifact_id,
            outcome="SUCCESS",
            details={
                "run_id": run_id,
                "bundle_sha256": bundle_sha256,
                "test_net_sharpe": run.metrics["net_sharpe"],
            },
        )
        return run, artifact_record, False

    # --- M3: Validation Gate, Model Card, Approval, Promotion & Rollback ---

    def validate_artifact(
        self,
        artifact_id: str,
        validator: Actor,
        intended_use: str = "Shadow and paper-trading research signal generation under human-governed risk controls.",
    ) -> Tuple[ValidationReport, ModelCard]:
        """Verify bundle integrity, run multi-metric validation gate, and persist ValidationReport + ModelCard."""
        artifact = self.registry.get_artifact_record(artifact_id)
        verified_bundle = self.artifact_store.verify_and_load_bundle(
            artifact_id=artifact_id,
            expected_bundle_sha256=artifact["bundle_sha256"],
        )
        report = run_validation_gate(
            artifact_record=artifact,
            verified_bundle=verified_bundle,
            validator=validator,
            gate_cfg=self.config.validation_gate,
        )
        self.registry.save_validation_report(report)

        run = self.registry.get_experiment_run(artifact["run_id"])
        dataset = self.registry.get_dataset(artifact["dataset_snapshot_key"])
        feature_set = self.registry.get_feature_set(artifact["feature_set_version_id"])
        card = generate_model_card(
            artifact_record=artifact,
            run=run,
            dataset=dataset,
            feature_set=feature_set,
            validation_report=report,
            evaluation_manifest=verified_bundle["evaluation_manifest"],
            intended_use=intended_use,
        )
        self.registry.save_model_card(card)

        # Persist model card Markdown inside state_dir/model_cards for review
        cards_dir = self.state_dir / "model_cards"
        cards_dir.mkdir(parents=True, exist_ok=True)
        (cards_dir / f"{artifact_id}.md").write_text(
            render_model_card_markdown(card), encoding="utf-8"
        )
        (cards_dir / f"{artifact_id}.json").write_text(
            json.dumps(card.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )

        new_stage = ModelStage.VALIDATED if report.passed else ModelStage.REJECTED
        self.registry.update_artifact_stage(artifact_id, new_stage)
        self.audit_log.record(
            event_type="VALIDATION_COMPLETED",
            actor=validator,
            entity_type="artifact",
            entity_id=artifact_id,
            outcome="SUCCESS" if report.passed else "FAILED",
            details={
                "report_id": report.report_id,
                "passed": report.passed,
                "failed_checks": [c.check_name for c in report.failed_checks()],
                "model_card_id": card.card_id,
            },
        )
        return report, card

    def approve_promotion(
        self,
        artifact_id: str,
        target_stage: ModelStage,
        approver: Actor,
        rationale: str,
        decision: str = "APPROVED",
        conditions: Tuple[str, ...] = (),
    ) -> ApprovalRecord:
        return self.governance.record_approval(
            artifact_id=artifact_id,
            target_stage=target_stage,
            approver=approver,
            decision=decision,
            rationale=rationale,
            conditions=conditions,
        )

    def promote_artifact(
        self,
        artifact_id: str,
        target_stage: ModelStage,
        environment: str,
        operator: Actor,
        enable_order_execution: bool = False,
        notes: str = "",
    ) -> DeploymentRecord:
        return self.governance.promote(
            artifact_id=artifact_id,
            target_stage=target_stage,
            environment=environment,
            operator=operator,
            enable_order_execution=enable_order_execution,
            notes=notes,
        )

    def rollback_deployment(
        self,
        environment: str,
        model_name: str,
        operator: Actor,
        reason: str,
        target_artifact_id: Optional[str] = None,
    ) -> Tuple[RollbackRecord, DeploymentRecord]:
        return self.governance.rollback(
            environment=environment,
            model_name=model_name,
            operator=operator,
            reason=reason,
            target_artifact_id=target_artifact_id,
        )

    # --- M4: Drift Monitoring, Observability Export, Lineage & Governance Review ---

    def monitor_deployment(
        self,
        environment: str,
        model_name: str,
        recent_records: Sequence[Dict[str, Any]],
        actor: Actor,
        champion_artifact_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Run drift & shadow divergence evaluation against active deployment and emit observability event."""
        active = self.registry.get_active_deployment(environment, model_name)
        if active is None:
            raise MlopsError(
                ErrorCode.NOT_FOUND,
                f"No active deployment found in '{environment}' for model '{model_name}'.",
                {"environment": environment, "model_name": model_name},
            )
        bundle = self.artifact_store.verify_and_load_bundle(
            artifact_id=active.artifact_id,
            expected_bundle_sha256=active.artifact_bundle_sha256,
        )
        champ_weights = None
        if champion_artifact_id:
            champ_art = self.registry.get_artifact_record(champion_artifact_id)
            champ_bundle = self.artifact_store.verify_and_load_bundle(
                artifact_id=champion_artifact_id,
                expected_bundle_sha256=champ_art["bundle_sha256"],
            )
            champ_weights = champ_bundle["weights"]

        report = evaluate_drift_and_alerts(
            environment=environment,
            artifact_id=active.artifact_id,
            weights_payload=bundle["weights"],
            recent_records=recent_records,
            cfg=self.config.monitoring,
            champion_weights_payload=champ_weights,
        )
        self.registry.save_drift_report(report)
        obs_event = ObservabilityEventAdapter.from_drift_report(report)
        self.audit_log.record(
            event_type="DRIFT_MONITORING_EVALUATED",
            actor=actor,
            entity_type="deployment",
            entity_id=active.deployment_id,
            outcome="ALERT" if report["alerts"] else "SUCCESS",
            details={
                "drift_report_id": report["drift_report_id"],
                "overall_severity": report["overall_severity"],
                "alert_count": len(report["alerts"]),
                "recommended_safe_fallback": report["recommended_safe_fallback"],
            },
        )
        return report, obs_event

    def inspect_lineage(self, artifact_id: str) -> Dict[str, Any]:
        return build_lineage_graph(self.registry, artifact_id)

    def export_inference_manifest(self, artifact_id: str) -> Dict[str, Any]:
        artifact = self.registry.get_artifact_record(artifact_id)
        bundle = self.artifact_store.verify_and_load_bundle(
            artifact_id=artifact_id,
            expected_bundle_sha256=artifact["bundle_sha256"],
        )
        fs = self.registry.get_feature_set(artifact["feature_set_version_id"])
        return InferenceEngineManifestAdapter.to_inference_manifest(artifact, bundle, fs)

    def governance_review(self) -> Dict[str, Any]:
        """Generate comprehensive governance audit summary covering lineage, approvals, audit chain, and alerts."""
        audit_status = self.audit_log.verify_chain()
        runs = self.registry.list_experiment_runs()
        artifacts = self.registry.list_artifacts()
        deployments = self.registry.list_deployments()
        rollbacks = self.registry.list_rollbacks()
        drift_reports = self.registry.list_drift_reports()

        deployed_lineage_checks: List[Dict[str, Any]] = []
        for dep in deployments:
            graph = self.inspect_lineage(dep.artifact_id)
            deployed_lineage_checks.append(
                {
                    "deployment_id": dep.deployment_id,
                    "environment": dep.environment,
                    "stage": dep.stage.value,
                    "artifact_id": dep.artifact_id,
                    "is_active": dep.is_active,
                    "order_execution_enabled": dep.order_execution_enabled,
                    "lineage_complete": graph["complete"],
                    "approval_count": len(dep.approval_ids),
                }
            )

        return {
            "reviewed_at_utc": utc_now_iso(),
            "audit_chain": audit_status,
            "experiment_runs": {
                "total": len(runs),
                "succeeded": sum(1 for r in runs if r.status == RunStatus.SUCCEEDED),
                "failed_or_rejected": sum(1 for r in runs if r.status != RunStatus.SUCCEEDED),
            },
            "artifacts_count": len(artifacts),
            "deployments_count": len(deployments),
            "rollbacks_count": len(rollbacks),
            "drift_reports_count": len(drift_reports),
            "critical_drift_reports": sum(
                1 for d in drift_reports if d["overall_severity"] == "CRITICAL"
            ),
            "all_deployed_lineage_complete": all(
                d["lineage_complete"] for d in deployed_lineage_checks
            ),
            "any_live_execution_enabled": any(
                d["order_execution_enabled"] for d in deployed_lineage_checks
            ),
            "deployed_lineage_checks": deployed_lineage_checks,
        }
