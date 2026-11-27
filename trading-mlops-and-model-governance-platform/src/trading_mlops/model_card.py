"""Structured governance Model Card builder and Markdown renderer."""

from __future__ import annotations

from typing import Any, Dict, Tuple

from trading_mlops.contracts import (
    DatasetSnapshot,
    ExperimentRun,
    FeatureSetContract,
    ModelCard,
    ValidationReport,
    sha256_canonical,
    utc_now_iso,
)


def generate_model_card(
    artifact_record: Dict[str, Any],
    run: ExperimentRun,
    dataset: DatasetSnapshot,
    feature_set: FeatureSetContract,
    validation_report: ValidationReport,
    evaluation_manifest: Dict[str, Any],
    intended_use: str = "Shadow and paper-trading research signal generation under human-governed risk controls.",
) -> ModelCard:
    """Create an immutable ModelCard capturing provenance, validation evidence, and risk boundaries."""
    now = utc_now_iso()
    card_id = "card_" + sha256_canonical(
        {
            "artifact_id": artifact_record["artifact_id"],
            "validation_report_id": validation_report.report_id,
            "created_at_utc": now,
        }
    )[:16]

    out_of_scope: Tuple[str, ...] = (
        "Automatic live model promotion based on a single metric is strictly prohibited.",
        "Blind retraining on recent returns without multi-regime review is prohibited.",
        "Real-capital order execution is disabled in MVP and requires separate legal, broker, security, and risk approval.",
        "Simulated historical out-of-sample metrics must not be interpreted as a guarantee of live returns.",
    )
    known_limitations: Tuple[str, ...] = (
        f"Evaluated on {'synthetic' if dataset.is_synthetic else 'historical'} fixture data ({dataset.row_count} total rows; {validation_report.sample_size} OOS test rows).",
        f"Linear standardized ridge model trained with fixed seed {run.seed}; does not capture nonlinear regime breaks without feature engineering.",
        f"Transaction cost assumption is fixed at {evaluation_manifest['transaction_cost_bps']} bps per unit turnover; real slippage and market impact are not modeled.",
        "Order execution remains disabled (order_execution_enabled=false) by default.",
    )

    return ModelCard(
        card_id=card_id,
        artifact_id=artifact_record["artifact_id"],
        model_name=artifact_record["model_name"],
        model_version=artifact_record["model_version"],
        strategy_id=run.strategy_id,
        owner=run.created_by,
        created_at_utc=now,
        intended_use=intended_use,
        out_of_scope_uses=out_of_scope,
        dataset_lineage={
            "snapshot_key": dataset.snapshot_key,
            "content_sha256": dataset.content_sha256,
            "entitlement": dataset.entitlement,
            "license_name": dataset.license_name,
            "asset_class": dataset.asset_class,
            "currency": dataset.currency,
            "is_synthetic": dataset.is_synthetic,
            "date_range_utc": [dataset.start_event_ts_utc, dataset.end_event_ts_utc],
        },
        feature_lineage={
            "feature_set_version_id": feature_set.version_id,
            "schema_hash": feature_set.schema_hash,
            "features": [f.version_id for f in feature_set.features],
        },
        code_commit=run.code_commit,
        evaluation_summary={
            "test_metrics": evaluation_manifest["splits"]["test"],
            "val_metrics": evaluation_manifest["splits"]["val"],
            "baseline_buy_and_hold_test_sharpe": evaluation_manifest[
                "baseline_buy_and_hold_test_sharpe"
            ],
            "uncertainty": validation_report.uncertainty,
            "environment": validation_report.environment_info,
        },
        validation_report_id=validation_report.report_id,
        validation_passed=validation_report.passed,
        known_limitations=known_limitations,
        risk_controls={
            "order_execution_enabled_default": False,
            "requires_independent_approval": True,
            "self_approval_forbidden": True,
            "rollback_supported": True,
            "drift_monitoring_required": True,
        },
    )


def render_model_card_markdown(card: ModelCard) -> str:
    """Render a human-reviewable Markdown model card."""
    test_m = card.evaluation_summary["test_metrics"]
    unc = card.evaluation_summary["uncertainty"]
    lines = [
        f"# Model Card: `{card.model_name}@{card.model_version}`",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Card ID | `{card.card_id}` |",
        f"| Artifact ID | `{card.artifact_id}` |",
        f"| Strategy ID | `{card.strategy_id}` |",
        f"| Owner | `{card.owner}` |",
        f"| Code Commit | `{card.code_commit}` |",
        f"| Validation Report | `{card.validation_report_id}` (`{'PASSED' if card.validation_passed else 'FAILED'}`) |",
        f"| Created (UTC) | `{card.created_at_utc}` |",
        "",
        "## Intended Use",
        card.intended_use,
        "",
        "## Explicit Non-Goals / Out-of-Scope Uses",
    ]
    for item in card.out_of_scope_uses:
        lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "## Dataset & Feature Lineage",
            f"- Dataset Snapshot: `{card.dataset_lineage['snapshot_key']}`",
            f"- Entitlement / License: `{card.dataset_lineage['entitlement']}` / `{card.dataset_lineage['license_name']}` (Synthetic: `{card.dataset_lineage['is_synthetic']}`)",
            f"- Feature Set: `{card.feature_lineage['feature_set_version_id']}`",
            "",
            "## Out-of-Sample Evaluation Summary (Net of Costs)",
            f"- Sample size: `{test_m['sample_size']}` rows (transaction cost: `{test_m['transaction_cost_bps']} bps`)",
            f"- Net Sharpe: `{test_m['net_sharpe']}` (Bootstrap SE: `{unc['test_net_sharpe_bootstrap_se']}`, approx 95% CI: `[{unc['approx_95pct_ci_low']}, {unc['approx_95pct_ci_high']}]`)",
            f"- Mean Net Return: `{test_m['mean_net_return_bps']} bps` | Max Drawdown: `{test_m['max_drawdown_pct']}`",
            f"- Hit Rate: `{test_m['hit_rate']}` | IC: `{test_m['information_coefficient']}` | Avg Turnover: `{test_m['avg_turnover']}`",
            "",
            "## Known Limitations",
        ]
    )
    for lim in card.known_limitations:
        lines.append(f"- {lim}")
    return "\n".join(lines) + "\n"
