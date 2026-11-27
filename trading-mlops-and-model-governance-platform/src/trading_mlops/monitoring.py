"""Feature/prediction drift metrics, shadow divergence, alerting, and governance review."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

from trading_mlops.config import MonitoringConfig
from trading_mlops.contracts import AlertSeverity, sha256_canonical, utc_now_iso
from trading_mlops.errors import ContractError, ErrorCode
from trading_mlops.training import score_record


def compute_psi(values: Sequence[float], bin_edges: Sequence[float]) -> float:
    """Compute Population Stability Index against 5 uniform-quantile training bins (20% each)."""
    n = len(values)
    if n == 0:
        return 0.0
    num_bins = len(bin_edges) + 1
    counts = [0] * num_bins
    for v in values:
        placed = False
        for idx, edge in enumerate(bin_edges):
            if v <= edge:
                counts[idx] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1

    expected_prop = 1.0 / num_bins
    eps = 1e-4
    psi = 0.0
    for c in counts:
        actual_prop = max(c / n, eps)
        psi += (actual_prop - expected_prop) * math.log(actual_prop / expected_prop)
    return round(max(psi, 0.0), 6)


def evaluate_drift_and_alerts(
    environment: str,
    artifact_id: str,
    weights_payload: Dict[str, Any],
    recent_records: Sequence[Dict[str, Any]],
    cfg: MonitoringConfig,
    champion_weights_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute PSI, mean z-shift, prediction drift, optional shadow divergence, and safe fallback recommendations."""
    n = len(recent_records)
    if n < cfg.min_monitoring_samples:
        raise ContractError(
            ErrorCode.INVALID_CONTRACT,
            f"Monitoring requires at least {cfg.min_monitoring_samples} samples, got {n}.",
            {"sample_size": n, "min_monitoring_samples": cfg.min_monitoring_samples},
        )

    feature_names: List[str] = weights_payload["feature_names"]
    base_means: Dict[str, float] = weights_payload["feature_means"]
    base_stds: Dict[str, float] = weights_payload["feature_stds"]
    bin_edges: Dict[str, List[float]] = weights_payload["feature_bin_edges"]

    alerts: List[Dict[str, Any]] = []
    feature_drift: Dict[str, Dict[str, Any]] = {}
    overall_severity = AlertSeverity.INFO

    def _escalate(sev: AlertSeverity) -> None:
        nonlocal overall_severity
        order = {AlertSeverity.INFO: 0, AlertSeverity.WARNING: 1, AlertSeverity.CRITICAL: 2}
        if order[sev] > order[overall_severity]:
            overall_severity = sev

    for fname in feature_names:
        vals = [float(r["features"][fname]) for r in recent_records]
        obs_mean = sum(vals) / n
        obs_var = sum((v - obs_mean) ** 2 for v in vals) / max(n - 1, 1)
        obs_std = math.sqrt(obs_var)
        z_shift = abs(obs_mean - float(base_means[fname])) / max(float(base_stds[fname]), 1e-9)
        psi = compute_psi(vals, bin_edges[fname])

        sev = AlertSeverity.INFO
        if psi >= cfg.psi_critical_threshold or z_shift >= cfg.mean_shift_z_critical:
            sev = AlertSeverity.CRITICAL
        elif psi >= cfg.psi_warning_threshold or z_shift >= cfg.mean_shift_z_warning:
            sev = AlertSeverity.WARNING
        _escalate(sev)

        feature_drift[fname] = {
            "baseline_mean": round(float(base_means[fname]), 6),
            "observed_mean": round(obs_mean, 6),
            "baseline_std": round(float(base_stds[fname]), 6),
            "observed_std": round(obs_std, 6),
            "mean_z_shift": round(z_shift, 6),
            "psi": psi,
            "severity": sev.value,
        }
        if sev != AlertSeverity.INFO:
            alerts.append(
                {
                    "alert_type": "FEATURE_DRIFT",
                    "severity": sev.value,
                    "feature": fname,
                    "psi": psi,
                    "mean_z_shift": round(z_shift, 6),
                    "recommended_action": (
                        "FREEZE_PROMOTION_AND_EVALUATE_ROLLBACK"
                        if sev == AlertSeverity.CRITICAL
                        else "OPERATOR_INVESTIGATION_REQUIRED"
                    ),
                    "auto_retrain_triggered": False,
                }
            )

    # Prediction distribution drift
    preds = [score_record(r["features"], weights_payload) for r in recent_records]
    pred_mean = sum(preds) / n
    pred_std = math.sqrt(sum((p - pred_mean) ** 2 for p in preds) / max(n - 1, 1)) if n > 1 else 0.0
    base_pred_mean = float(weights_payload.get("baseline_prediction_mean", 0.0))
    base_pred_std = max(float(weights_payload.get("baseline_prediction_std", 1.0)), 1e-6)
    pred_z_shift = abs(pred_mean - base_pred_mean) / base_pred_std

    pred_sev = AlertSeverity.INFO
    if pred_z_shift >= cfg.prediction_mean_shift_critical:
        pred_sev = AlertSeverity.CRITICAL
    elif pred_z_shift >= cfg.prediction_mean_shift_warning:
        pred_sev = AlertSeverity.WARNING
    _escalate(pred_sev)

    if pred_sev != AlertSeverity.INFO:
        alerts.append(
            {
                "alert_type": "PREDICTION_DRIFT",
                "severity": pred_sev.value,
                "prediction_mean_z_shift": round(pred_z_shift, 6),
                "recommended_action": (
                    "DISABLE_EXECUTION_AND_ROLLBACK_TO_PREVIOUS"
                    if pred_sev == AlertSeverity.CRITICAL
                    else "REVIEW_SHADOW_SIGNALS"
                ),
                "auto_retrain_triggered": False,
            }
        )

    # Optional shadow-vs-champion comparison
    shadow_comparison: Optional[Dict[str, Any]] = None
    if champion_weights_payload is not None:
        champ_preds = [score_record(r["features"], champion_weights_payload) for r in recent_records]
        abs_diffs = [abs(a - b) for a, b in zip(preds, champ_preds)]
        mean_abs_div = sum(abs_diffs) / n
        sign_disagreement = sum(
            1 for a, b in zip(preds, champ_preds) if (a > 0) != (b > 0)
        ) / n
        div_sev = AlertSeverity.INFO
        if sign_disagreement >= cfg.shadow_divergence_critical:
            div_sev = AlertSeverity.CRITICAL
        elif sign_disagreement >= cfg.shadow_divergence_warning:
            div_sev = AlertSeverity.WARNING
        _escalate(div_sev)
        shadow_comparison = {
            "sample_size": n,
            "mean_abs_prediction_diff_bps": round(mean_abs_div, 6),
            "directional_disagreement_rate": round(sign_disagreement, 6),
            "severity": div_sev.value,
        }
        if div_sev != AlertSeverity.INFO:
            alerts.append(
                {
                    "alert_type": "SHADOW_DIVERGENCE",
                    "severity": div_sev.value,
                    "directional_disagreement_rate": round(sign_disagreement, 6),
                    "recommended_action": "HOLD_PAPER_PROMOTION_PENDING_RISK_REVIEW",
                    "auto_retrain_triggered": False,
                }
            )

    recommended_fallback = "NONE"
    if overall_severity == AlertSeverity.CRITICAL:
        recommended_fallback = "FREEZE_PROMOTION_AND_ROLLBACK_TO_KNOWN_GOOD"
    elif overall_severity == AlertSeverity.WARNING:
        recommended_fallback = "OPERATOR_REVIEW_AND_KEEP_EXECUTION_DISABLED"

    now = utc_now_iso()
    drift_report_id = "drf_" + sha256_canonical(
        {
            "environment": environment,
            "artifact_id": artifact_id,
            "timestamp": now,
            "sample_size": n,
            "overall_severity": overall_severity.value,
        }
    )[:16]

    return {
        "drift_report_id": drift_report_id,
        "environment": environment,
        "artifact_id": artifact_id,
        "created_at_utc": now,
        "sample_size": n,
        "overall_severity": overall_severity.value,
        "feature_drift": feature_drift,
        "prediction_drift": {
            "baseline_mean": round(base_pred_mean, 6),
            "observed_mean": round(pred_mean, 6),
            "baseline_std": round(base_pred_std, 6),
            "observed_std": round(pred_std, 6),
            "mean_z_shift": round(pred_z_shift, 6),
            "severity": pred_sev.value,
        },
        "shadow_comparison": shadow_comparison,
        "alerts": alerts,
        "recommended_safe_fallback": recommended_fallback,
        "automatic_retrain_permitted": False,
        "automatic_live_promotion_permitted": False,
    }
