"""RBAC governance, independent approval, staged promotion, shadow execution, and verified rollback."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from trading_mlops.artifacts import LocalArtifactStore
from trading_mlops.audit import AuditLog
from trading_mlops.config import PlatformConfig
from trading_mlops.contracts import (
    Actor,
    ActorRole,
    ApprovalRecord,
    DeploymentRecord,
    ModelStage,
    RollbackRecord,
    sha256_canonical,
    utc_now_iso,
)
from trading_mlops.errors import (
    ErrorCode,
    GovernanceError,
    RollbackError,
    ValidationGateError,
)
from trading_mlops.registry import SQLiteRegistryStore

ALLOWED_TRANSITIONS: Dict[ModelStage, Set[ModelStage]] = {
    ModelStage.REGISTERED: {ModelStage.VALIDATED, ModelStage.REJECTED, ModelStage.ARCHIVED},
    ModelStage.VALIDATED: {ModelStage.SHADOW, ModelStage.ARCHIVED},
    ModelStage.SHADOW: {ModelStage.PAPER, ModelStage.ROLLED_BACK, ModelStage.ARCHIVED},
    ModelStage.PAPER: {ModelStage.LIVE_GATED, ModelStage.ROLLED_BACK, ModelStage.ARCHIVED},
    ModelStage.LIVE_GATED: {ModelStage.ROLLED_BACK, ModelStage.ARCHIVED},
    ModelStage.ROLLED_BACK: {ModelStage.SHADOW, ModelStage.PAPER, ModelStage.ARCHIVED},
    ModelStage.REJECTED: {ModelStage.ARCHIVED},
    ModelStage.ARCHIVED: set(),
}

PROMOTION_STAGE_ORDER = {
    ModelStage.SHADOW: "shadow",
    ModelStage.PAPER: "paper",
    ModelStage.LIVE_GATED: "live_gated",
}


class GovernanceController:
    """Enforces separation of duties, approval gates, integrity checks, and safe rollback."""

    def __init__(
        self,
        registry: SQLiteRegistryStore,
        artifact_store: LocalArtifactStore,
        audit_log: AuditLog,
        config: PlatformConfig,
    ) -> None:
        self.registry = registry
        self.artifact_store = artifact_store
        self.audit_log = audit_log
        self.config = config

    def record_approval(
        self,
        artifact_id: str,
        target_stage: ModelStage,
        approver: Actor,
        decision: str,
        rationale: str,
        conditions: Tuple[str, ...] = (),
    ) -> ApprovalRecord:
        """Record an independent governance approval or rejection."""
        if decision not in {"APPROVED", "REJECTED"}:
            raise GovernanceError(
                ErrorCode.INVALID_CONTRACT,
                f"Decision must be 'APPROVED' or 'REJECTED', got {decision!r}.",
                {"decision": decision},
            )

        required_roles = self.config.governance.stage_required_approvals.get(target_stage.value)
        if not required_roles:
            raise GovernanceError(
                ErrorCode.INVALID_STAGE_TRANSITION,
                f"Stage '{target_stage.value}' does not accept promotion approvals.",
                {"target_stage": target_stage.value},
            )

        if approver.role.value not in required_roles:
            self.audit_log.record(
                event_type="APPROVAL_ATTEMPT",
                actor=approver,
                entity_type="artifact",
                entity_id=artifact_id,
                outcome="BLOCKED",
                details={
                    "reason": "UNAUTHORIZED_ROLE",
                    "target_stage": target_stage.value,
                    "required_roles": required_roles,
                },
            )
            raise GovernanceError(
                ErrorCode.UNAUTHORIZED_PROMOTION,
                f"Role '{approver.role.value}' cannot approve promotion to '{target_stage.value}'; required one of {required_roles}.",
                {
                    "actor_id": approver.actor_id,
                    "role": approver.role.value,
                    "target_stage": target_stage.value,
                    "required_roles": required_roles,
                },
            )

        artifact = self.registry.get_artifact_record(artifact_id)
        if self.config.governance.forbid_self_approval and approver.actor_id == artifact["created_by"]:
            self.audit_log.record(
                event_type="APPROVAL_ATTEMPT",
                actor=approver,
                entity_type="artifact",
                entity_id=artifact_id,
                outcome="BLOCKED",
                details={"reason": "SELF_APPROVAL_FORBIDDEN", "target_stage": target_stage.value},
            )
            raise GovernanceError(
                ErrorCode.SELF_APPROVAL_FORBIDDEN,
                f"Separation of duties violated: creator '{approver.actor_id}' cannot approve their own artifact '{artifact_id}'.",
                {"artifact_id": artifact_id, "actor_id": approver.actor_id},
            )

        validation = self.registry.get_latest_validation_report(artifact_id)
        if validation is None or not validation.passed:
            self.audit_log.record(
                event_type="APPROVAL_ATTEMPT",
                actor=approver,
                entity_type="artifact",
                entity_id=artifact_id,
                outcome="BLOCKED",
                details={
                    "reason": "VALIDATION_GATE_FAILED",
                    "validation_report_id": validation.report_id if validation else None,
                },
            )
            raise ValidationGateError(
                ErrorCode.VALIDATION_GATE_FAILED,
                f"Cannot approve artifact '{artifact_id}': no passing ValidationReport exists.",
                {
                    "artifact_id": artifact_id,
                    "validation_report_id": validation.report_id if validation else None,
                },
            )

        now = utc_now_iso()
        approval_id = "appr_" + sha256_canonical(
            {
                "artifact_id": artifact_id,
                "target_stage": target_stage.value,
                "approver_id": approver.actor_id,
                "decision": decision,
                "timestamp": now,
            }
        )[:16]
        record = ApprovalRecord(
            approval_id=approval_id,
            artifact_id=artifact_id,
            target_stage=target_stage,
            approver_id=approver.actor_id,
            approver_role=approver.role,
            approved_at_utc=now,
            decision=decision,
            rationale=rationale,
            validation_report_id=validation.report_id,
            conditions=conditions,
        )
        self.registry.save_approval(record)
        self.audit_log.record(
            event_type="APPROVAL_RECORDED",
            actor=approver,
            entity_type="artifact",
            entity_id=artifact_id,
            outcome="SUCCESS",
            details=record.to_dict(),
        )
        return record

    def promote(
        self,
        artifact_id: str,
        target_stage: ModelStage,
        environment: str,
        operator: Actor,
        enable_order_execution: bool = False,
        notes: str = "",
    ) -> DeploymentRecord:
        """Promote a verified, validated, independently approved artifact to SHADOW, PAPER, or LIVE_GATED."""
        # 1. RBAC: only RELEASE_OPERATOR can execute promotions
        if operator.role != ActorRole.RELEASE_OPERATOR:
            self.audit_log.record(
                event_type="PROMOTION_ATTEMPT",
                actor=operator,
                entity_type="artifact",
                entity_id=artifact_id,
                outcome="BLOCKED",
                details={
                    "reason": "UNAUTHORIZED_PROMOTION",
                    "target_stage": target_stage.value,
                    "environment": environment,
                },
            )
            raise GovernanceError(
                ErrorCode.UNAUTHORIZED_PROMOTION,
                f"Actor '{operator.actor_id}' with role '{operator.role.value}' is not authorized to execute deployments (requires RELEASE_OPERATOR).",
                {"actor_id": operator.actor_id, "role": operator.role.value},
            )

        if target_stage not in PROMOTION_STAGE_ORDER:
            raise GovernanceError(
                ErrorCode.INVALID_STAGE_TRANSITION,
                f"Stage '{target_stage.value}' is not a deployable promotion stage.",
                {"target_stage": target_stage.value},
            )

        # 2. Execution safety guard
        if enable_order_execution:
            if target_stage == ModelStage.SHADOW:
                raise GovernanceError(
                    ErrorCode.EXECUTION_DISABLED_GUARD,
                    "Order execution is permanently forbidden in SHADOW stage.",
                    {"target_stage": target_stage.value},
                )
            if target_stage == ModelStage.LIVE_GATED and not self.config.governance.allow_live_execution_in_mvp:
                self.audit_log.record(
                    event_type="PROMOTION_ATTEMPT",
                    actor=operator,
                    entity_type="artifact",
                    entity_id=artifact_id,
                    outcome="BLOCKED",
                    details={"reason": "LIVE_EXECUTION_DISABLED_IN_MVP", "environment": environment},
                )
                raise GovernanceError(
                    ErrorCode.EXECUTION_DISABLED_GUARD,
                    "Real-capital order execution is disabled in MVP; requires separate legal, broker, security, and risk approval.",
                    {"target_stage": target_stage.value, "environment": environment},
                )
            if not self.config.order_execution_enabled:
                raise GovernanceError(
                    ErrorCode.EXECUTION_DISABLED_GUARD,
                    "Platform configuration keeps order_execution_enabled=false; cannot enable execution.",
                    {"order_execution_enabled": False},
                )

        artifact = self.registry.get_artifact_record(artifact_id)
        current_stage = ModelStage(artifact["stage"])
        if target_stage not in ALLOWED_TRANSITIONS.get(current_stage, set()):
            self.audit_log.record(
                event_type="PROMOTION_ATTEMPT",
                actor=operator,
                entity_type="artifact",
                entity_id=artifact_id,
                outcome="BLOCKED",
                details={
                    "reason": "INVALID_STAGE_TRANSITION",
                    "current_stage": current_stage.value,
                    "target_stage": target_stage.value,
                },
            )
            raise GovernanceError(
                ErrorCode.INVALID_STAGE_TRANSITION,
                f"Invalid stage transition for '{artifact_id}': {current_stage.value} -> {target_stage.value}.",
                {"current_stage": current_stage.value, "target_stage": target_stage.value},
            )

        # 3. Validation gate check
        validation = self.registry.get_latest_validation_report(artifact_id)
        if validation is None or not validation.passed:
            self.audit_log.record(
                event_type="PROMOTION_ATTEMPT",
                actor=operator,
                entity_type="artifact",
                entity_id=artifact_id,
                outcome="BLOCKED",
                details={"reason": "VALIDATION_GATE_FAILED", "target_stage": target_stage.value},
            )
            raise ValidationGateError(
                ErrorCode.VALIDATION_GATE_FAILED,
                f"Promotion blocked: artifact '{artifact_id}' failed or lacks a passing ValidationReport.",
                {"artifact_id": artifact_id, "validation_report_id": validation.report_id if validation else None},
            )

        # 4. Model card & complete lineage check
        model_card = self.registry.get_model_card(artifact_id)
        if model_card is None:
            raise GovernanceError(
                ErrorCode.INCOMPLETE_LINEAGE,
                f"Promotion blocked: artifact '{artifact_id}' has no ModelCard.",
                {"artifact_id": artifact_id},
            )

        # 5. Cryptographic integrity & HMAC signature verification right before deployment
        try:
            self.artifact_store.verify_and_load_bundle(
                artifact_id=artifact_id,
                expected_bundle_sha256=artifact["bundle_sha256"],
            )
        except Exception as exc:
            self.audit_log.record(
                event_type="PROMOTION_ATTEMPT",
                actor=operator,
                entity_type="artifact",
                entity_id=artifact_id,
                outcome="BLOCKED",
                details={"reason": "ARTIFACT_TAMPERING_DETECTED", "error": str(exc)},
            )
            raise

        # 6. Feature compatibility check against target environment contract
        model_feature_set = self.registry.get_feature_set(artifact["feature_set_version_id"])
        env_feature_set = self.registry.get_environment_feature_contract(environment)
        if env_feature_set is not None:
            try:
                model_feature_set.check_serving_compatibility(env_feature_set)
            except Exception as exc:
                self.audit_log.record(
                    event_type="PROMOTION_ATTEMPT",
                    actor=operator,
                    entity_type="artifact",
                    entity_id=artifact_id,
                    outcome="BLOCKED",
                    details={
                        "reason": "INCOMPATIBLE_FEATURE_VERSION",
                        "environment": environment,
                        "model_feature_set": model_feature_set.version_id,
                        "env_feature_set": env_feature_set.version_id,
                        "error": str(exc),
                    },
                )
                raise

        # 7. Independent approval verification
        required_roles = self.config.governance.stage_required_approvals.get(target_stage.value, [])
        approvals = self.registry.list_approvals(artifact_id, target_stage=target_stage)
        valid_approvals: List[ApprovalRecord] = []
        satisfied_roles: Set[str] = set()
        seen_approvers: Set[str] = set()
        for appr in approvals:
            if appr.decision != "APPROVED":
                continue
            if appr.validation_report_id != validation.report_id:
                continue
            if self.config.governance.forbid_self_approval and appr.approver_id == artifact["created_by"]:
                continue
            if appr.approver_id in seen_approvers:
                continue
            if appr.approver_role.value in required_roles:
                valid_approvals.append(appr)
                satisfied_roles.add(appr.approver_role.value)
                seen_approvers.add(appr.approver_id)

        missing_roles = [r for r in required_roles if r not in satisfied_roles]
        if missing_roles:
            self.audit_log.record(
                event_type="PROMOTION_ATTEMPT",
                actor=operator,
                entity_type="artifact",
                entity_id=artifact_id,
                outcome="BLOCKED",
                details={
                    "reason": "APPROVAL_REQUIREMENT_NOT_MET",
                    "target_stage": target_stage.value,
                    "missing_roles": missing_roles,
                },
            )
            raise GovernanceError(
                ErrorCode.APPROVAL_REQUIREMENT_NOT_MET,
                f"Promotion to '{target_stage.value}' blocked: missing independent approval from roles {missing_roles}.",
                {"artifact_id": artifact_id, "missing_roles": missing_roles},
            )

        # 8. Commit deployment
        prev_active = self.registry.get_active_deployment(environment, artifact["model_name"])
        now = utc_now_iso()
        deployment_id = "dep_" + sha256_canonical(
            {
                "environment": environment,
                "artifact_id": artifact_id,
                "stage": target_stage.value,
                "operator": operator.actor_id,
                "timestamp": now,
            }
        )[:16]

        deployment = DeploymentRecord(
            deployment_id=deployment_id,
            environment=environment,
            stage=target_stage,
            model_name=artifact["model_name"],
            model_version=artifact["model_version"],
            artifact_id=artifact_id,
            artifact_bundle_sha256=artifact["bundle_sha256"],
            dataset_snapshot_key=artifact["dataset_snapshot_key"],
            feature_set_version_id=artifact["feature_set_version_id"],
            code_commit=artifact["code_commit"],
            validation_report_id=validation.report_id,
            approval_ids=tuple(a.approval_id for a in valid_approvals),
            deployed_by=operator.actor_id,
            deployed_at_utc=now,
            order_execution_enabled=bool(enable_order_execution),
            is_active=True,
            previous_deployment_id=prev_active.deployment_id if prev_active else None,
            notes=notes,
        )
        self.registry.save_deployment(deployment)
        self.registry.update_artifact_stage(artifact_id, target_stage)
        self.audit_log.record(
            event_type="MODEL_PROMOTED",
            actor=operator,
            entity_type="deployment",
            entity_id=deployment.deployment_id,
            outcome="SUCCESS",
            details=deployment.to_dict(),
        )
        return deployment

    def rollback(
        self,
        environment: str,
        model_name: str,
        operator: Actor,
        reason: str,
        target_artifact_id: Optional[str] = None,
    ) -> Tuple[RollbackRecord, DeploymentRecord]:
        """Restore a previous verified known-good deployment and record full audit trail."""
        if operator.role not in {ActorRole.RELEASE_OPERATOR, ActorRole.RISK_APPROVER}:
            self.audit_log.record(
                event_type="ROLLBACK_ATTEMPT",
                actor=operator,
                entity_type="environment",
                entity_id=environment,
                outcome="BLOCKED",
                details={"reason": "UNAUTHORIZED_PROMOTION", "model_name": model_name},
            )
            raise GovernanceError(
                ErrorCode.UNAUTHORIZED_PROMOTION,
                f"Actor '{operator.actor_id}' with role '{operator.role.value}' is not authorized to execute rollback.",
                {"actor_id": operator.actor_id, "role": operator.role.value},
            )

        current = self.registry.get_active_deployment(environment, model_name)
        if current is None:
            raise RollbackError(
                ErrorCode.ROLLBACK_FAILED,
                f"No active deployment found in '{environment}' for model '{model_name}'.",
                {"environment": environment, "model_name": model_name},
            )

        history = self.registry.list_deployments(environment=environment, model_name=model_name)
        candidate: Optional[DeploymentRecord] = None
        if target_artifact_id:
            for dep in reversed(history):
                if dep.artifact_id == target_artifact_id and dep.deployment_id != current.deployment_id:
                    candidate = dep
                    break
        else:
            for dep in reversed(history):
                if dep.deployment_id != current.deployment_id and dep.artifact_id != current.artifact_id:
                    candidate = dep
                    break

        if candidate is None:
            raise RollbackError(
                ErrorCode.ROLLBACK_FAILED,
                f"No previous known-good deployment found to roll back to in '{environment}' for '{model_name}'.",
                {"environment": environment, "model_name": model_name, "current_deployment_id": current.deployment_id},
            )

        # Verify cryptographic integrity and feature compatibility of the rollback target BEFORE switching
        target_artifact = self.registry.get_artifact_record(candidate.artifact_id)
        self.artifact_store.verify_and_load_bundle(
            artifact_id=candidate.artifact_id,
            expected_bundle_sha256=target_artifact["bundle_sha256"],
        )
        env_feature_set = self.registry.get_environment_feature_contract(environment)
        if env_feature_set is not None:
            model_fs = self.registry.get_feature_set(candidate.feature_set_version_id)
            model_fs.check_serving_compatibility(env_feature_set)

        now = utc_now_iso()
        restored_dep_id = "dep_" + sha256_canonical(
            {
                "rollback_to": candidate.deployment_id,
                "environment": environment,
                "operator": operator.actor_id,
                "timestamp": now,
            }
        )[:16]
        restored_deployment = DeploymentRecord(
            deployment_id=restored_dep_id,
            environment=environment,
            stage=candidate.stage,
            model_name=candidate.model_name,
            model_version=candidate.model_version,
            artifact_id=candidate.artifact_id,
            artifact_bundle_sha256=candidate.artifact_bundle_sha256,
            dataset_snapshot_key=candidate.dataset_snapshot_key,
            feature_set_version_id=candidate.feature_set_version_id,
            code_commit=candidate.code_commit,
            validation_report_id=candidate.validation_report_id,
            approval_ids=candidate.approval_ids,
            deployed_by=operator.actor_id,
            deployed_at_utc=now,
            order_execution_enabled=False,  # Safe fallback always keeps execution disabled
            is_active=True,
            previous_deployment_id=current.deployment_id,
            notes=f"Rollback from {current.model_version} ({current.deployment_id}): {reason}",
        )
        self.registry.save_deployment(restored_deployment)
        self.registry.update_artifact_stage(current.artifact_id, ModelStage.ROLLED_BACK)
        self.registry.update_artifact_stage(candidate.artifact_id, candidate.stage)

        rollback_id = "rbk_" + sha256_canonical(
            {
                "from": current.deployment_id,
                "to": restored_deployment.deployment_id,
                "timestamp": now,
            }
        )[:16]
        rollback_record = RollbackRecord(
            rollback_id=rollback_id,
            environment=environment,
            from_deployment_id=current.deployment_id,
            from_artifact_id=current.artifact_id,
            restored_deployment_id=restored_deployment.deployment_id,
            restored_artifact_id=candidate.artifact_id,
            restored_model_version=candidate.model_version,
            executed_by=operator.actor_id,
            executed_at_utc=now,
            reason=reason,
            verified_integrity=True,
        )
        self.registry.save_rollback(rollback_record)
        self.audit_log.record(
            event_type="MODEL_ROLLED_BACK",
            actor=operator,
            entity_type="rollback",
            entity_id=rollback_id,
            outcome="SUCCESS",
            details=rollback_record.to_dict(),
        )
        return rollback_record, restored_deployment
