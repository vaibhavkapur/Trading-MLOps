"""Multi-metric validation gate refusing single-metric promotion."""

from __future__ import annotations

import platform
import sys
from typing import Any, Dict, List

from trading_mlops.config import ValidationGateConfig
from trading_mlops.contracts import (
    Actor,
    ActorRole,
    ValidationCheckResult,
    ValidationReport,
    sha256_canonical,
    utc_now_iso,
)
from trading_mlops.errors import ErrorCode, GovernanceError


def run_validation_gate(
    artifact_record: Dict[str, Any],
    verified_bundle: Dict[str, Any],
    validator: Actor,
    gate_cfg: ValidationGateConfig,
) -> ValidationReport:
    """Execute multi-dimensional validation gate on a verified artifact bundle."""
    if validator.role not in {ActorRole.VALIDATOR, ActorRole.RISK_APPROVER, ActorRole.GOVERNANCE_AUDITOR}:
        raise GovernanceError(
            ErrorCode.UNAUTHORIZED_PROMOTION,
            f"Actor '{validator.actor_id}' with role '{validator.role.value}' is not permitted to run validation gates.",
            {"actor_id": validator.actor_id, "role": validator.role.value},
        )

    eval_manifest = verified_bundle["evaluation_manifest"]
    test_metrics = eval_manifest["splits"]["test"]
    val_metrics = eval_manifest["splits"]["val"]

    checks: List[ValidationCheckResult] = []

    # 1. Artifact cryptographic integrity (already verified before entering, recorded explicitly)
    checks.append(
        ValidationCheckResult(
            check_name="artifact_integrity_and_signature",
            passed=True,
            observed=verified_bundle["bundle_sha256"][:16],
            threshold="valid_sha256_and_hmac",
            unit="digest",
            message="Artifact bundle SHA-256 and HMAC-SHA256 signature verified.",
        )
    )

    # 2. Out-of-sample minimum sample size
    oos_n = int(test_metrics["sample_size"])
    checks.append(
        ValidationCheckResult(
            check_name="min_oos_sample_size",
            passed=oos_n >= gate_cfg.min_oos_samples,
            observed=oos_n,
            threshold=gate_cfg.min_oos_samples,
            unit="records",
            message=f"OOS sample size {oos_n} >= {gate_cfg.min_oos_samples}.",
        )
    )

    # 3. Net-of-cost Sharpe ratio (test split)
    net_sharpe = float(test_metrics["net_sharpe"])
    checks.append(
        ValidationCheckResult(
            check_name="min_net_sharpe",
            passed=net_sharpe >= gate_cfg.min_net_sharpe,
            observed=net_sharpe,
            threshold=gate_cfg.min_net_sharpe,
            unit="ratio",
            message=f"Test net Sharpe {net_sharpe:.4f} >= {gate_cfg.min_net_sharpe:.4f}.",
        )
    )

    # 4. Mean net return net of transaction costs (bps)
    mean_net_bps = float(test_metrics["mean_net_return_bps"])
    checks.append(
        ValidationCheckResult(
            check_name="min_mean_net_return_bps",
            passed=mean_net_bps >= gate_cfg.min_net_return_bps,
            observed=mean_net_bps,
            threshold=gate_cfg.min_net_return_bps,
            unit="bps",
            message=f"Test mean net return {mean_net_bps:.4f} bps >= {gate_cfg.min_net_return_bps:.4f} bps.",
        )
    )

    # 5. Maximum drawdown bound
    max_dd = float(test_metrics["max_drawdown_pct"])
    checks.append(
        ValidationCheckResult(
            check_name="max_drawdown_pct",
            passed=max_dd <= gate_cfg.max_drawdown_pct,
            observed=max_dd,
            threshold=gate_cfg.max_drawdown_pct,
            unit="fraction",
            message=f"Test max drawdown {max_dd:.4f} <= {gate_cfg.max_drawdown_pct:.4f}.",
        )
    )

    # 6. Turnover ceiling
    avg_turnover = float(test_metrics["avg_turnover"])
    checks.append(
        ValidationCheckResult(
            check_name="max_daily_turnover",
            passed=avg_turnover <= gate_cfg.max_daily_turnover,
            observed=avg_turnover,
            threshold=gate_cfg.max_daily_turnover,
            unit="position_delta_per_bar",
            message=f"Test average turnover {avg_turnover:.4f} <= {gate_cfg.max_daily_turnover:.4f}.",
        )
    )

    # 7. Hit rate floor
    hit_rate = float(test_metrics["hit_rate"])
    checks.append(
        ValidationCheckResult(
            check_name="min_hit_rate",
            passed=hit_rate >= gate_cfg.min_hit_rate,
            observed=hit_rate,
            threshold=gate_cfg.min_hit_rate,
            unit="fraction",
            message=f"Test directional hit rate {hit_rate:.4f} >= {gate_cfg.min_hit_rate:.4f}.",
        )
    )

    # 8. Information Coefficient (IC) floor
    ic = float(test_metrics["information_coefficient"])
    checks.append(
        ValidationCheckResult(
            check_name="min_information_coefficient",
            passed=ic >= gate_cfg.min_information_coefficient,
            observed=ic,
            threshold=gate_cfg.min_information_coefficient,
            unit="correlation",
            message=f"Test IC {ic:.4f} >= {gate_cfg.min_information_coefficient:.4f}.",
        )
    )

    # 9. Sub-period stability
    sub_gap = float(test_metrics["subperiod_sharpe_gap"])
    checks.append(
        ValidationCheckResult(
            check_name="max_subperiod_sharpe_gap",
            passed=sub_gap <= gate_cfg.max_subperiod_sharpe_gap,
            observed=sub_gap,
            threshold=gate_cfg.max_subperiod_sharpe_gap,
            unit="sharpe_diff",
            message=f"Sub-period Sharpe gap {sub_gap:.4f} <= {gate_cfg.max_subperiod_sharpe_gap:.4f}.",
        )
    )

    # 10. Validation split consistency (val net return non-catastrophic)
    val_sharpe = float(val_metrics["net_sharpe"])
    checks.append(
        ValidationCheckResult(
            check_name="validation_split_positive_signal",
            passed=val_sharpe >= 0.0,
            observed=val_sharpe,
            threshold=0.0,
            unit="ratio",
            message=f"Validation split net Sharpe {val_sharpe:.4f} >= 0.0.",
        )
    )

    all_passed = all(c.passed for c in checks)
    now = utc_now_iso()
    report_id = "val_" + sha256_canonical(
        {
            "artifact_id": artifact_record["artifact_id"],
            "validator": validator.actor_id,
            "timestamp": now,
            "checks": [c.to_dict() for c in checks],
        }
    )[:16]

    env_info = {
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "transaction_cost_bps": float(eval_manifest["transaction_cost_bps"]),
        "currency": eval_manifest["currency"],
    }
    uncertainty = {
        "test_net_sharpe": net_sharpe,
        "test_net_sharpe_bootstrap_se": float(test_metrics["net_sharpe_bootstrap_se"]),
        "approx_95pct_ci_low": round(net_sharpe - 1.96 * float(test_metrics["net_sharpe_bootstrap_se"]), 4),
        "approx_95pct_ci_high": round(net_sharpe + 1.96 * float(test_metrics["net_sharpe_bootstrap_se"]), 4),
        "sample_size": oos_n,
    }

    return ValidationReport(
        report_id=report_id,
        artifact_id=artifact_record["artifact_id"],
        run_id=artifact_record["run_id"],
        model_name=artifact_record["model_name"],
        model_version=artifact_record["model_version"],
        validator_id=validator.actor_id,
        created_at_utc=now,
        passed=all_passed,
        checks=tuple(checks),
        sample_size=oos_n,
        environment_info=env_info,
        uncertainty=uncertainty,
    )
