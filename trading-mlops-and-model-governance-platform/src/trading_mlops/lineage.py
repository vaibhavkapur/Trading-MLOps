"""Dataset provenance verification, point-in-time leakage detection, and lineage graph."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from trading_mlops.config import LineageConfig
from trading_mlops.contracts import (
    DatasetSnapshot,
    FeatureSetContract,
    TrainingJobSpec,
    parse_utc_timestamp,
    sha256_bytes,
    sha256_canonical,
)
from trading_mlops.errors import (
    ContractError,
    DataLeakageError,
    ErrorCode,
    MlopsError,
)
from trading_mlops.registry import SQLiteRegistryStore


@dataclass(frozen=True)
class ChronologicalSplit:
    """Verified leak-free chronological train/validation/test partition."""

    train: List[Dict[str, Any]]
    val: List[Dict[str, Any]]
    test: List[Dict[str, Any]]
    summary: Dict[str, Any]


def load_dataset_records(dataset_path: Path, expected_sha256: str = "") -> Tuple[List[Dict[str, Any]], str]:
    """Load JSON dataset fixture and verify SHA-256 content integrity."""
    if not dataset_path.exists():
        raise MlopsError(
            ErrorCode.ADAPTER_ERROR,
            f"Dataset file not found: {dataset_path}",
            {"path": str(dataset_path)},
        )
    raw = dataset_path.read_bytes()
    actual_sha = sha256_bytes(raw)
    if expected_sha256 and actual_sha != expected_sha256:
        raise ContractError(
            ErrorCode.ARTIFACT_TAMPERING_DETECTED,
            f"Dataset SHA-256 mismatch for '{dataset_path.name}'.",
            {"expected": expected_sha256, "observed": actual_sha},
        )
    payload = json.loads(raw.decode("utf-8"))
    if isinstance(payload, dict) and "records" in payload:
        records = payload["records"]
    elif isinstance(payload, list):
        records = payload
    else:
        raise ContractError(
            ErrorCode.INVALID_CONTRACT,
            "Dataset JSON must be a list of records or an object with a 'records' array.",
            {"path": str(dataset_path)},
        )
    if not records:
        raise ContractError(
            ErrorCode.INVALID_CONTRACT,
            "Dataset contains zero records.",
            {"path": str(dataset_path)},
        )
    return records, actual_sha


def _pearson_corr(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mx) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - my) ** 2 for y in ys))
    if den_x == 0.0 or den_y == 0.0:
        return 0.0
    return num / (den_x * den_y)


def validate_and_split_chronologically(
    records: List[Dict[str, Any]],
    feature_contract: FeatureSetContract,
    job_spec: TrainingJobSpec,
    lineage_cfg: LineageConfig,
) -> ChronologicalSplit:
    """Enforce point-in-time timestamps, anti-leakage checks, anti-blind-retrain policy, and embargoed chronological split."""
    if job_spec.training_window_policy == "recent_returns_only":
        raise DataLeakageError(
            ErrorCode.BLIND_RETRAINING_POLICY_VIOLATION,
            "Blind retraining on recent returns only is explicitly prohibited by platform policy.",
            {"training_window_policy": job_spec.training_window_policy},
        )

    if job_spec.embargo_seconds < lineage_cfg.min_embargo_seconds:
        raise DataLeakageError(
            ErrorCode.DATA_LEAKAGE_DETECTED,
            f"Embargo gap {job_spec.embargo_seconds}s is below required minimum {lineage_cfg.min_embargo_seconds}s.",
            {
                "embargo_seconds": job_spec.embargo_seconds,
                "min_embargo_seconds": lineage_cfg.min_embargo_seconds,
            },
        )

    feature_names = feature_contract.feature_names()
    parsed: List[Tuple[datetime, datetime, Dict[str, Any]]] = []
    seen_keys = set()

    for idx, rec in enumerate(records):
        if "event_ts_utc" not in rec or "availability_ts_utc" not in rec:
            raise DataLeakageError(
                ErrorCode.TIMESTAMP_CONTRACT_ERROR,
                f"Record {idx} missing required UTC timestamps (event_ts_utc / availability_ts_utc).",
                {"record_index": idx},
            )
        event_dt = parse_utc_timestamp(rec["event_ts_utc"], "event_ts_utc")
        avail_dt = parse_utc_timestamp(rec["availability_ts_utc"], "availability_ts_utc")
        if avail_dt < event_dt:
            raise DataLeakageError(
                ErrorCode.DATA_LEAKAGE_DETECTED,
                f"Point-in-time leakage at record {idx}: availability_ts_utc precedes event_ts_utc.",
                {
                    "record_index": idx,
                    "event_ts_utc": rec["event_ts_utc"],
                    "availability_ts_utc": rec["availability_ts_utc"],
                },
            )

        # Check optional feature_as_of_ts_utc if present (explicit lookahead feature timestamp)
        if "feature_as_of_ts_utc" in rec and rec["feature_as_of_ts_utc"] is not None:
            feat_dt = parse_utc_timestamp(rec["feature_as_of_ts_utc"], "feature_as_of_ts_utc")
            if feat_dt > avail_dt:
                raise DataLeakageError(
                    ErrorCode.DATA_LEAKAGE_DETECTED,
                    f"Future feature leakage at record {idx}: feature_as_of_ts_utc exceeds availability_ts_utc.",
                    {
                        "record_index": idx,
                        "feature_as_of_ts_utc": rec["feature_as_of_ts_utc"],
                        "availability_ts_utc": rec["availability_ts_utc"],
                    },
                )

        # Check optional label_horizon_ts_utc if present
        if "label_available_ts_utc" in rec and rec["label_available_ts_utc"] is not None:
            label_dt = parse_utc_timestamp(rec["label_available_ts_utc"], "label_available_ts_utc")
            if label_dt < avail_dt:
                raise DataLeakageError(
                    ErrorCode.DATA_LEAKAGE_DETECTED,
                    f"Label timestamp leakage at record {idx}: label_available_ts_utc precedes availability_ts_utc.",
                    {"record_index": idx},
                )

        entity_id = rec.get("entity_id", "UNKNOWN")
        dedup_key = (entity_id, rec["event_ts_utc"])
        if dedup_key in seen_keys:
            raise DataLeakageError(
                ErrorCode.DATA_LEAKAGE_DETECTED,
                f"Duplicate (entity_id, event_ts_utc) record detected ({dedup_key}), risking train/test contamination.",
                {"entity_id": entity_id, "event_ts_utc": rec["event_ts_utc"]},
            )
        seen_keys.add(dedup_key)

        features_map = rec.get("features")
        if not isinstance(features_map, dict):
            raise ContractError(
                ErrorCode.INVALID_CONTRACT,
                f"Record {idx} missing 'features' dictionary.",
                {"record_index": idx},
            )
        for fname in feature_names:
            if fname not in features_map:
                raise ContractError(
                    ErrorCode.INCOMPATIBLE_FEATURE_VERSION,
                    f"Record {idx} missing required feature '{fname}' from contract '{feature_contract.version_id}'.",
                    {"record_index": idx, "missing_feature": fname},
                )
            val = features_map[fname]
            if not isinstance(val, (int, float)) or not math.isfinite(float(val)):
                raise ContractError(
                    ErrorCode.INVALID_CONTRACT,
                    f"Non-finite feature value for '{fname}' at record {idx}.",
                    {"record_index": idx, "feature": fname, "value": val},
                )
        target_val = rec.get(job_spec.target_column)
        if not isinstance(target_val, (int, float)) or not math.isfinite(float(target_val)):
            raise ContractError(
                ErrorCode.INVALID_CONTRACT,
                f"Missing or non-finite target '{job_spec.target_column}' at record {idx}.",
                {"record_index": idx, "target_column": job_spec.target_column},
            )
        parsed.append((event_dt, avail_dt, rec))

    # Sort strictly chronologically by (event_ts_utc, availability_ts_utc, entity_id)
    parsed.sort(key=lambda item: (item[0], item[1], str(item[2].get("entity_id", ""))))

    # Enforce minimum historical span (anti-blind-retrain on tiny recent window)
    span_days = (parsed[-1][0] - parsed[0][0]).total_seconds() / 86400.0
    if span_days < lineage_cfg.min_training_span_days:
        raise DataLeakageError(
            ErrorCode.BLIND_RETRAINING_POLICY_VIOLATION,
            f"Dataset span ({span_days:.2f} days) is below minimum multi-regime span ({lineage_cfg.min_training_span_days} days).",
            {"span_days": round(span_days, 2), "min_training_span_days": lineage_cfg.min_training_span_days},
        )

    # Suspicious target leakage check (near-1.0 correlation between any single feature and future target)
    targets = [float(item[2][job_spec.target_column]) for item in parsed]
    for fname in feature_names:
        fvals = [float(item[2]["features"][fname]) for item in parsed]
        corr = abs(_pearson_corr(fvals, targets))
        if corr >= lineage_cfg.max_suspicious_feature_target_corr:
            raise DataLeakageError(
                ErrorCode.DATA_LEAKAGE_DETECTED,
                f"Suspicious target leakage detected: feature '{fname}' has |corr(feature, target)|={corr:.5f} >= {lineage_cfg.max_suspicious_feature_target_corr}.",
                {
                    "feature": fname,
                    "abs_correlation": round(corr, 6),
                    "threshold": lineage_cfg.max_suspicious_feature_target_corr,
                },
            )

    # Check if records carry explicit split tags that violate chronological order
    explicit_splits = {item[2].get("split") for item in parsed if "split" in item[2]}
    if explicit_splits:
        train_items = [item for item in parsed if item[2].get("split") == "train"]
        val_items = [item for item in parsed if item[2].get("split") == "val"]
        test_items = [item for item in parsed if item[2].get("split") == "test"]
        if not train_items or not val_items or not test_items:
            raise DataLeakageError(
                ErrorCode.DATA_LEAKAGE_DETECTED,
                "Explicit split labels must populate train, val, and test partitions.",
                {"observed_splits": sorted(str(s) for s in explicit_splits)},
            )
    else:
        n = len(parsed)
        n_train = int(n * job_spec.train_ratio)
        n_val = int(n * job_spec.val_ratio)
        train_items = parsed[:n_train]
        # Apply embargo gap between train and val, and between val and test
        max_train_avail = max(item[1] for item in train_items)
        after_train = [
            item
            for item in parsed[n_train:]
            if (item[0] - max_train_avail).total_seconds() >= job_spec.embargo_seconds
        ]
        val_items = after_train[:n_val]
        if not val_items:
            raise DataLeakageError(
                ErrorCode.DATA_LEAKAGE_DETECTED,
                "Insufficient records remaining for validation split after applying embargo gap.",
                {"total_records": n, "embargo_seconds": job_spec.embargo_seconds},
            )
        max_val_avail = max(item[1] for item in val_items)
        test_items = [
            item
            for item in after_train[n_val:]
            if (item[0] - max_val_avail).total_seconds() >= job_spec.embargo_seconds
        ]
        if not test_items:
            raise DataLeakageError(
                ErrorCode.DATA_LEAKAGE_DETECTED,
                "Insufficient records remaining for test split after applying embargo gap.",
                {"total_records": n, "embargo_seconds": job_spec.embargo_seconds},
            )

    # Final strict chronological non-overlap invariant verification
    max_train_avail = max(item[1] for item in train_items)
    min_val_event = min(item[0] for item in val_items)
    max_val_avail = max(item[1] for item in val_items)
    min_test_event = min(item[0] for item in test_items)

    train_val_gap = (min_val_event - max_train_avail).total_seconds()
    val_test_gap = (min_test_event - max_val_avail).total_seconds()

    if train_val_gap < job_spec.embargo_seconds:
        raise DataLeakageError(
            ErrorCode.DATA_LEAKAGE_DETECTED,
            f"Chronological leakage between train and val splits: gap={train_val_gap:.1f}s < embargo={job_spec.embargo_seconds}s.",
            {
                "max_train_availability_ts_utc": max_train_avail.isoformat().replace("+00:00", "Z"),
                "min_val_event_ts_utc": min_val_event.isoformat().replace("+00:00", "Z"),
                "gap_seconds": train_val_gap,
                "required_embargo_seconds": job_spec.embargo_seconds,
            },
        )
    if val_test_gap < job_spec.embargo_seconds:
        raise DataLeakageError(
            ErrorCode.DATA_LEAKAGE_DETECTED,
            f"Chronological leakage between val and test splits: gap={val_test_gap:.1f}s < embargo={job_spec.embargo_seconds}s.",
            {
                "max_val_availability_ts_utc": max_val_avail.isoformat().replace("+00:00", "Z"),
                "min_test_event_ts_utc": min_test_event.isoformat().replace("+00:00", "Z"),
                "gap_seconds": val_test_gap,
                "required_embargo_seconds": job_spec.embargo_seconds,
            },
        )

    summary = {
        "total_input_records": len(parsed),
        "train_count": len(train_items),
        "val_count": len(val_items),
        "test_count": len(test_items),
        "embargoed_dropped_count": len(parsed) - (len(train_items) + len(val_items) + len(test_items)),
        "span_days": round(span_days, 2),
        "train_val_gap_seconds": round(train_val_gap, 1),
        "val_test_gap_seconds": round(val_test_gap, 1),
        "train_range_utc": [train_items[0][2]["event_ts_utc"], train_items[-1][2]["event_ts_utc"]],
        "val_range_utc": [val_items[0][2]["event_ts_utc"], val_items[-1][2]["event_ts_utc"]],
        "test_range_utc": [test_items[0][2]["event_ts_utc"], test_items[-1][2]["event_ts_utc"]],
    }
    return ChronologicalSplit(
        train=[item[2] for item in train_items],
        val=[item[2] for item in val_items],
        test=[item[2] for item in test_items],
        summary=summary,
    )


def build_lineage_graph(registry: SQLiteRegistryStore, artifact_id: str) -> Dict[str, Any]:
    """Assemble and verify complete end-to-end lineage for a registered artifact."""
    artifact = registry.get_artifact_record(artifact_id)
    run = registry.get_experiment_run(artifact["run_id"])
    dataset = registry.get_dataset(artifact["dataset_snapshot_key"])
    feature_set = registry.get_feature_set(artifact["feature_set_version_id"])
    validation = registry.get_latest_validation_report(artifact_id)
    model_card = registry.get_model_card(artifact_id)
    approvals = registry.list_approvals(artifact_id)

    missing_links: List[str] = []
    if not artifact.get("code_commit"):
        missing_links.append("code_commit")
    if not artifact.get("bundle_sha256"):
        missing_links.append("bundle_sha256")
    if validation is None:
        missing_links.append("validation_report")
    if model_card is None:
        missing_links.append("model_card")

    graph = {
        "artifact_id": artifact_id,
        "model_name": artifact["model_name"],
        "model_version": artifact["model_version"],
        "stage": artifact["stage"],
        "bundle_sha256": artifact["bundle_sha256"],
        "code_commit": artifact["code_commit"],
        "experiment_run": {
            "run_id": run.run_id,
            "idempotency_key": run.idempotency_key,
            "spec_hash": run.spec_hash,
            "seed": run.seed,
            "status": run.status.value,
            "created_by": run.created_by,
        },
        "dataset": {
            "snapshot_key": dataset.snapshot_key,
            "content_sha256": dataset.content_sha256,
            "entitlement": dataset.entitlement,
            "license_name": dataset.license_name,
            "is_synthetic": dataset.is_synthetic,
        },
        "feature_set": {
            "version_id": feature_set.version_id,
            "schema_hash": feature_set.schema_hash,
            "features": feature_set.feature_names(),
        },
        "validation_report": {
            "report_id": validation.report_id,
            "passed": validation.passed,
            "report_sha256": validation.report_sha256,
        }
        if validation
        else None,
        "model_card": {
            "card_id": model_card.card_id,
            "card_sha256": model_card.card_sha256,
        }
        if model_card
        else None,
        "approvals": [a.to_dict() for a in approvals],
        "complete": len(missing_links) == 0,
        "missing_links": missing_links,
    }
    graph["lineage_sha256"] = sha256_canonical(graph)
    return graph
