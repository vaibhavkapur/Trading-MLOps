"""Canonical SQLite-backed metadata registry for experiments, lineage, artifacts, and deployments."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from trading_mlops.contracts import (
    ApprovalRecord,
    DatasetSnapshot,
    DeploymentRecord,
    ExperimentRun,
    FeatureSetContract,
    ModelCard,
    ModelStage,
    RollbackRecord,
    ValidationReport,
)
from trading_mlops.errors import (
    ContractError,
    ErrorCode,
    IdempotencyConflictError,
    MlopsError,
)


class SQLiteRegistryStore:
    """Transactional local metadata registry (cleanly swappable for PostgreSQL via adapter)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS datasets (
                    snapshot_key TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS feature_sets (
                    version_id TEXT PRIMARY KEY,
                    feature_set_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    schema_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS experiment_runs (
                    run_id TEXT PRIMARY KEY,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    spec_hash TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    bundle_sha256 TEXT NOT NULL,
                    bundle_path TEXT NOT NULL,
                    dataset_snapshot_key TEXT NOT NULL,
                    feature_set_version_id TEXT NOT NULL,
                    code_commit TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(model_name, model_version)
                );

                CREATE TABLE IF NOT EXISTS validation_reports (
                    report_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS model_cards (
                    card_id TEXT PRIMARY KEY,
                    artifact_id TEXT UNIQUE NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL,
                    target_stage TEXT NOT NULL,
                    approver_id TEXT NOT NULL,
                    approver_role TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    approved_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS deployments (
                    deployment_id TEXT PRIMARY KEY,
                    environment TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    is_active INTEGER NOT NULL,
                    deployed_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rollbacks (
                    rollback_id TEXT PRIMARY KEY,
                    environment TEXT NOT NULL,
                    executed_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS drift_reports (
                    drift_report_id TEXT PRIMARY KEY,
                    environment TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS environment_feature_contracts (
                    environment TEXT PRIMARY KEY,
                    feature_set_version_id TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );
                """
            )

    # --- Datasets ---

    def save_dataset(self, snapshot: DatasetSnapshot) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM datasets WHERE snapshot_key = ?",
                (snapshot.snapshot_key,),
            ).fetchone()
            if row is not None:
                existing = json.loads(row["payload_json"])
                if existing["content_sha256"] != snapshot.content_sha256 or existing["schema_hash"] != snapshot.schema_hash:
                    raise ContractError(
                        ErrorCode.IMMUTABLE_VERSION_CONFLICT,
                        f"Dataset snapshot '{snapshot.snapshot_key}' already exists with different content.",
                        {"snapshot_key": snapshot.snapshot_key},
                    )
                return
            conn.execute(
                "INSERT INTO datasets (snapshot_key, dataset_id, version, content_sha256, payload_json) VALUES (?, ?, ?, ?, ?)",
                (
                    snapshot.snapshot_key,
                    snapshot.dataset_id,
                    snapshot.version,
                    snapshot.content_sha256,
                    json.dumps(snapshot.to_dict(), sort_keys=True),
                ),
            )

    def get_dataset(self, snapshot_key: str) -> DatasetSnapshot:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM datasets WHERE snapshot_key = ?",
                (snapshot_key,),
            ).fetchone()
        if row is None:
            raise MlopsError(
                ErrorCode.NOT_FOUND,
                f"Dataset snapshot not found: '{snapshot_key}'.",
                {"snapshot_key": snapshot_key},
            )
        return DatasetSnapshot.from_dict(json.loads(row["payload_json"]))

    def list_datasets(self) -> List[DatasetSnapshot]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload_json FROM datasets ORDER BY snapshot_key").fetchall()
        return [DatasetSnapshot.from_dict(json.loads(r["payload_json"])) for r in rows]

    # --- Feature Sets ---

    def save_feature_set(self, contract: FeatureSetContract) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM feature_sets WHERE version_id = ?",
                (contract.version_id,),
            ).fetchone()
            if row is not None:
                existing = json.loads(row["payload_json"])
                if existing["schema_hash"] != contract.schema_hash:
                    raise ContractError(
                        ErrorCode.IMMUTABLE_VERSION_CONFLICT,
                        f"FeatureSet '{contract.version_id}' already registered with different schema.",
                        {"version_id": contract.version_id},
                    )
                return
            # Also forbid mutating same (feature_set_id, version) with a different schema_hash
            same_ver = conn.execute(
                "SELECT version_id, schema_hash FROM feature_sets WHERE feature_set_id = ? AND version = ?",
                (contract.feature_set_id, contract.version),
            ).fetchone()
            if same_ver is not None and same_ver["schema_hash"] != contract.schema_hash:
                raise ContractError(
                    ErrorCode.IMMUTABLE_VERSION_CONFLICT,
                    f"FeatureSet '{contract.feature_set_id}@{contract.version}' is immutable and already exists with a different schema.",
                    {
                        "feature_set_id": contract.feature_set_id,
                        "version": contract.version,
                        "existing_schema_hash": same_ver["schema_hash"],
                        "new_schema_hash": contract.schema_hash,
                    },
                )
            conn.execute(
                "INSERT INTO feature_sets (version_id, feature_set_id, version, schema_hash, payload_json) VALUES (?, ?, ?, ?, ?)",
                (
                    contract.version_id,
                    contract.feature_set_id,
                    contract.version,
                    contract.schema_hash,
                    json.dumps(contract.to_dict(), sort_keys=True),
                ),
            )

    def get_feature_set(self, version_id: str) -> FeatureSetContract:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM feature_sets WHERE version_id = ?",
                (version_id,),
            ).fetchone()
        if row is None:
            raise MlopsError(
                ErrorCode.NOT_FOUND,
                f"FeatureSetContract not found: '{version_id}'.",
                {"version_id": version_id},
            )
        return FeatureSetContract.from_dict(json.loads(row["payload_json"]))

    def set_environment_feature_contract(
        self, environment: str, feature_set_version_id: str, updated_at_utc: str
    ) -> None:
        # Ensure feature set exists
        self.get_feature_set(feature_set_version_id)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO environment_feature_contracts (environment, feature_set_version_id, updated_at_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(environment) DO UPDATE SET
                    feature_set_version_id = excluded.feature_set_version_id,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (environment, feature_set_version_id, updated_at_utc),
            )

    def get_environment_feature_contract(self, environment: str) -> Optional[FeatureSetContract]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT feature_set_version_id FROM environment_feature_contracts WHERE environment = ?",
                (environment,),
            ).fetchone()
        if row is None:
            return None
        return self.get_feature_set(row["feature_set_version_id"])

    # --- Experiment Runs ---

    def get_run_by_idempotency_key(self, idempotency_key: str) -> Optional[ExperimentRun]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM experiment_runs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return ExperimentRun.from_dict(json.loads(row["payload_json"]))

    def save_experiment_run(self, run: ExperimentRun) -> None:
        existing = self.get_run_by_idempotency_key(run.idempotency_key)
        if existing is not None:
            if existing.spec_hash != run.spec_hash:
                raise IdempotencyConflictError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    f"Idempotency key '{run.idempotency_key}' already exists with a different training spec.",
                    {
                        "idempotency_key": run.idempotency_key,
                        "existing_spec_hash": existing.spec_hash,
                        "new_spec_hash": run.spec_hash,
                    },
                )
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO experiment_runs
                    (run_id, idempotency_key, spec_hash, model_name, model_version, status, created_at_utc, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.idempotency_key,
                    run.spec_hash,
                    run.model_name,
                    run.model_version,
                    run.status.value,
                    run.created_at_utc,
                    json.dumps(run.to_dict(), sort_keys=True),
                ),
            )

    def get_experiment_run(self, run_id: str) -> ExperimentRun:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM experiment_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise MlopsError(
                ErrorCode.NOT_FOUND,
                f"ExperimentRun not found: '{run_id}'.",
                {"run_id": run_id},
            )
        return ExperimentRun.from_dict(json.loads(row["payload_json"]))

    def list_experiment_runs(self, status: Optional[str] = None) -> List[ExperimentRun]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT payload_json FROM experiment_runs WHERE status = ? ORDER BY created_at_utc",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM experiment_runs ORDER BY created_at_utc"
                ).fetchall()
        return [ExperimentRun.from_dict(json.loads(r["payload_json"])) for r in rows]

    # --- Artifacts ---

    def save_artifact_record(self, record: Dict[str, Any]) -> None:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT artifact_id, bundle_sha256 FROM artifacts WHERE model_name = ? AND model_version = ?",
                (record["model_name"], record["model_version"]),
            ).fetchone()
            if existing is not None:
                if existing["bundle_sha256"] == record["bundle_sha256"] and existing["artifact_id"] == record["artifact_id"]:
                    return
                raise ContractError(
                    ErrorCode.IMMUTABLE_VERSION_CONFLICT,
                    f"Model version '{record['model_name']}@{record['model_version']}' already registered with different artifact.",
                    {
                        "model_name": record["model_name"],
                        "model_version": record["model_version"],
                        "existing_artifact_id": existing["artifact_id"],
                    },
                )
            conn.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, run_id, model_name, model_version, stage, bundle_sha256,
                    bundle_path, dataset_snapshot_key, feature_set_version_id, code_commit,
                    created_by, created_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["artifact_id"],
                    record["run_id"],
                    record["model_name"],
                    record["model_version"],
                    record["stage"],
                    record["bundle_sha256"],
                    record["bundle_path"],
                    record["dataset_snapshot_key"],
                    record["feature_set_version_id"],
                    record["code_commit"],
                    record["created_by"],
                    record["created_at_utc"],
                    json.dumps(record, sort_keys=True),
                ),
            )

    def get_artifact_record(self, artifact_id: str) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise MlopsError(
                ErrorCode.NOT_FOUND,
                f"Artifact not found: '{artifact_id}'.",
                {"artifact_id": artifact_id},
            )
        return json.loads(row["payload_json"])

    def update_artifact_stage(self, artifact_id: str, new_stage: ModelStage) -> Dict[str, Any]:
        rec = self.get_artifact_record(artifact_id)
        rec["stage"] = new_stage.value
        with self._connect() as conn:
            conn.execute(
                "UPDATE artifacts SET stage = ?, payload_json = ? WHERE artifact_id = ?",
                (new_stage.value, json.dumps(rec, sort_keys=True), artifact_id),
            )
        return rec

    def list_artifacts(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload_json FROM artifacts ORDER BY created_at_utc").fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    # --- Validation Reports ---

    def save_validation_report(self, report: ValidationReport) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO validation_reports
                    (report_id, artifact_id, passed, created_at_utc, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    report.report_id,
                    report.artifact_id,
                    1 if report.passed else 0,
                    report.created_at_utc,
                    json.dumps(report.to_dict(), sort_keys=True),
                ),
            )

    def get_latest_validation_report(self, artifact_id: str) -> Optional[ValidationReport]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json FROM validation_reports
                WHERE artifact_id = ?
                ORDER BY created_at_utc DESC, rowid DESC
                LIMIT 1
                """,
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        return ValidationReport.from_dict(json.loads(row["payload_json"]))

    # --- Model Cards ---

    def save_model_card(self, card: ModelCard) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO model_cards (card_id, artifact_id, created_at_utc, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    card.card_id,
                    card.artifact_id,
                    card.created_at_utc,
                    json.dumps(card.to_dict(), sort_keys=True),
                ),
            )

    def get_model_card(self, artifact_id: str) -> Optional[ModelCard]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM model_cards WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        return ModelCard.from_dict(json.loads(row["payload_json"]))

    # --- Approvals ---

    def save_approval(self, approval: ApprovalRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO approvals (
                    approval_id, artifact_id, target_stage, approver_id,
                    approver_role, decision, approved_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.approval_id,
                    approval.artifact_id,
                    approval.target_stage.value,
                    approval.approver_id,
                    approval.approver_role.value,
                    approval.decision,
                    approval.approved_at_utc,
                    json.dumps(approval.to_dict(), sort_keys=True),
                ),
            )

    def list_approvals(
        self, artifact_id: str, target_stage: Optional[ModelStage] = None
    ) -> List[ApprovalRecord]:
        with self._connect() as conn:
            if target_stage is not None:
                rows = conn.execute(
                    """
                    SELECT payload_json FROM approvals
                    WHERE artifact_id = ? AND target_stage = ?
                    ORDER BY approved_at_utc
                    """,
                    (artifact_id, target_stage.value),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM approvals WHERE artifact_id = ? ORDER BY approved_at_utc",
                    (artifact_id,),
                ).fetchall()
        return [ApprovalRecord.from_dict(json.loads(r["payload_json"])) for r in rows]

    # --- Deployments & Rollbacks ---

    def save_deployment(self, deployment: DeploymentRecord) -> None:
        with self._connect() as conn:
            if deployment.is_active:
                # Deactivate prior active deployments for the same environment & model_name
                rows = conn.execute(
                    "SELECT deployment_id, payload_json FROM deployments WHERE environment = ? AND model_name = ? AND is_active = 1",
                    (deployment.environment, deployment.model_name),
                ).fetchall()
                for row in rows:
                    payload = json.loads(row["payload_json"])
                    payload["is_active"] = False
                    conn.execute(
                        "UPDATE deployments SET is_active = 0, payload_json = ? WHERE deployment_id = ?",
                        (json.dumps(payload, sort_keys=True), row["deployment_id"]),
                    )
            conn.execute(
                """
                INSERT INTO deployments (
                    deployment_id, environment, stage, model_name, model_version,
                    artifact_id, is_active, deployed_at_utc, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    deployment.deployment_id,
                    deployment.environment,
                    deployment.stage.value,
                    deployment.model_name,
                    deployment.model_version,
                    deployment.artifact_id,
                    1 if deployment.is_active else 0,
                    deployment.deployed_at_utc,
                    json.dumps(deployment.to_dict(), sort_keys=True),
                ),
            )

    def get_active_deployment(
        self, environment: str, model_name: str
    ) -> Optional[DeploymentRecord]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json FROM deployments
                WHERE environment = ? AND model_name = ? AND is_active = 1
                ORDER BY deployed_at_utc DESC, rowid DESC
                LIMIT 1
                """,
                (environment, model_name),
            ).fetchone()
        if row is None:
            return None
        return DeploymentRecord.from_dict(json.loads(row["payload_json"]))

    def list_deployments(
        self, environment: Optional[str] = None, model_name: Optional[str] = None
    ) -> List[DeploymentRecord]:
        query = "SELECT payload_json FROM deployments WHERE 1=1"
        params: List[Any] = []
        if environment is not None:
            query += " AND environment = ?"
            params.append(environment)
        if model_name is not None:
            query += " AND model_name = ?"
            params.append(model_name)
        query += " ORDER BY deployed_at_utc ASC, rowid ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [DeploymentRecord.from_dict(json.loads(r["payload_json"])) for r in rows]

    def save_rollback(self, rollback: RollbackRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO rollbacks (rollback_id, environment, executed_at_utc, payload_json) VALUES (?, ?, ?, ?)",
                (
                    rollback.rollback_id,
                    rollback.environment,
                    rollback.executed_at_utc,
                    json.dumps(rollback.to_dict(), sort_keys=True),
                ),
            )

    def list_rollbacks(self, environment: Optional[str] = None) -> List[RollbackRecord]:
        with self._connect() as conn:
            if environment:
                rows = conn.execute(
                    "SELECT payload_json FROM rollbacks WHERE environment = ? ORDER BY executed_at_utc",
                    (environment,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM rollbacks ORDER BY executed_at_utc"
                ).fetchall()
        return [RollbackRecord.from_dict(json.loads(r["payload_json"])) for r in rows]

    # --- Drift Reports ---

    def save_drift_report(self, report: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO drift_reports
                    (drift_report_id, environment, artifact_id, severity, created_at_utc, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    report["drift_report_id"],
                    report["environment"],
                    report["artifact_id"],
                    report["overall_severity"],
                    report["created_at_utc"],
                    json.dumps(report, sort_keys=True),
                ),
            )

    def list_drift_reports(self, environment: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if environment:
                rows = conn.execute(
                    "SELECT payload_json FROM drift_reports WHERE environment = ? ORDER BY created_at_utc",
                    (environment,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload_json FROM drift_reports ORDER BY created_at_utc"
                ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]
