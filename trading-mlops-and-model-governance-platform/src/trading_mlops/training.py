"""Deterministic fixed-seed financial ML training and cost-aware out-of-sample evaluation."""

from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Sequence, Tuple

from trading_mlops.contracts import FeatureSetContract, TrainingJobSpec


def _solve_linear_system(a: List[List[float]], b: List[float]) -> List[float]:
    """Deterministic Gaussian elimination with partial pivoting (pure stdlib)."""
    n = len(b)
    mat = [row[:] + [b[i]] for i, row in enumerate(a)]

    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(mat[r][col]))
        if abs(mat[pivot_row][col]) < 1e-12:
            mat[pivot_row][col] = 1e-12
        mat[col], mat[pivot_row] = mat[pivot_row], mat[col]
        pivot = mat[col][col]
        for j in range(col, n + 1):
            mat[col][j] /= pivot
        for r in range(n):
            if r != col:
                factor = mat[r][col]
                for j in range(col, n + 1):
                    mat[r][j] -= factor * mat[col][j]
    return [round(mat[i][n], 10) for i in range(n)]


def _compute_feature_stats(
    records: Sequence[Dict[str, Any]], feature_names: Sequence[str]
) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, List[float]]]:
    """Compute training-only mean, std, and quantile bin edges for drift baselines."""
    means: Dict[str, float] = {}
    stds: Dict[str, float] = {}
    bin_edges: Dict[str, List[float]] = {}
    n = len(records)
    for fname in feature_names:
        vals = [float(r["features"][fname]) for r in records]
        mean = sum(vals) / n
        var = sum((v - mean) ** 2 for v in vals) / max(n - 1, 1)
        std = math.sqrt(var) if var > 1e-12 else 1.0
        means[fname] = round(mean, 10)
        stds[fname] = round(std, 10)
        sorted_vals = sorted(vals)
        # 5 quantile bins -> 4 interior edges
        edges: List[float] = []
        for q in (0.2, 0.4, 0.6, 0.8):
            idx = min(max(int(q * (n - 1)), 0), n - 1)
            edges.append(round(sorted_vals[idx], 8))
        bin_edges[fname] = edges
    return means, stds, bin_edges


def _fit_ridge_regression(
    train_records: Sequence[Dict[str, Any]],
    feature_names: Sequence[str],
    target_column: str,
    means: Dict[str, float],
    stds: Dict[str, float],
    l2_penalty: float,
    seed: int,
    feature_scale: float = 1.0,
) -> Tuple[List[float], float]:
    """Fit deterministic standardized Ridge linear alpha predictor."""
    # Fixed-seed deterministic tiny tie-breaking order shuffle (reproducible per seed)
    rng = random.Random(seed)
    indices = list(range(len(train_records)))
    rng.shuffle(indices)

    d = len(feature_names)
    # Augmented with bias term at index d
    xtx = [[0.0 for _ in range(d + 1)] for _ in range(d + 1)]
    xty = [0.0 for _ in range(d + 1)]

    for idx in indices:
        rec = train_records[idx]
        x = [
            ((float(rec["features"][f]) - means[f]) / stds[f]) * feature_scale
            for f in feature_names
        ] + [1.0]
        y = float(rec[target_column])
        for i in range(d + 1):
            xty[i] += x[i] * y
            xi = x[i]
            for j in range(d + 1):
                xtx[i][j] += xi * x[j]

    for i in range(d):
        xtx[i][i] += max(l2_penalty, 1e-6)
    xtx[d][d] += 1e-8  # numerical stability on intercept

    solution = _solve_linear_system(xtx, xty)
    weights = solution[:d]
    bias = solution[d]
    return weights, bias


def score_record(
    features: Dict[str, float],
    weights_payload: Dict[str, Any],
) -> float:
    """Deterministic pure-Python model scoring used in evaluation, shadow, and drift monitoring."""
    feature_names: List[str] = weights_payload["feature_names"]
    means: Dict[str, float] = weights_payload["feature_means"]
    stds: Dict[str, float] = weights_payload["feature_stds"]
    weights: List[float] = weights_payload["weights"]
    bias: float = float(weights_payload["bias"])
    sign_multiplier: float = float(weights_payload.get("signal_multiplier", 1.0))

    pred = bias
    for fname, w in zip(feature_names, weights):
        z = (float(features[fname]) - float(means[fname])) / float(stds[fname])
        pred += float(w) * z
    return round(pred * sign_multiplier, 8)


def _compute_sharpe(returns_bps: Sequence[float]) -> float:
    n = len(returns_bps)
    if n < 2:
        return 0.0
    mean = sum(returns_bps) / n
    var = sum((r - mean) ** 2 for r in returns_bps) / (n - 1)
    std = math.sqrt(var)
    if std < 1e-12:
        return 0.0
    # Annualized assuming 252 trading periods
    return (mean / std) * math.sqrt(252.0)


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx < 1e-12 or dy < 1e-12:
        return 0.0
    return num / (dx * dy)


def evaluate_predictions(
    records: Sequence[Dict[str, Any]],
    weights_payload: Dict[str, Any],
    target_column: str,
    transaction_cost_bps: float,
    signal_threshold_bps: float = 0.0,
    seed: int = 42,
) -> Dict[str, Any]:
    """Evaluate out-of-sample predictions with realistic transaction costs, drawdown, turnover, and bootstrap SE."""
    preds: List[float] = []
    targets: List[float] = []
    positions: List[float] = []
    gross_returns: List[float] = []
    net_returns: List[float] = []
    turnovers: List[float] = []

    prev_pos = 0.0
    for rec in records:
        pred = score_record(rec["features"], weights_payload)
        target = float(rec[target_column])
        if pred > signal_threshold_bps:
            pos = 1.0
        elif pred < -signal_threshold_bps:
            pos = -1.0
        else:
            pos = 0.0
        turnover = abs(pos - prev_pos)
        gross = pos * target
        net = gross - turnover * transaction_cost_bps
        preds.append(pred)
        targets.append(target)
        positions.append(pos)
        gross_returns.append(gross)
        net_returns.append(net)
        turnovers.append(turnover)
        prev_pos = pos

    n = len(records)
    mse = sum((p - y) ** 2 for p, y in zip(preds, targets)) / max(n, 1)
    rmse = math.sqrt(mse)
    ic = _pearson(preds, targets)
    hits = sum(
        1
        for p, y in zip(preds, targets)
        if (p > 0 and y > 0) or (p < 0 and y < 0) or (p == 0 and y == 0)
    )
    hit_rate = hits / max(n, 1)

    # Equity curve and max drawdown (in fraction, starting at 1.0; 1 bps = 1e-4)
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r_bps in net_returns:
        equity *= 1.0 + (r_bps * 1e-4)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    gross_sharpe = _compute_sharpe(gross_returns)
    net_sharpe = _compute_sharpe(net_returns)
    mean_net_bps = sum(net_returns) / max(n, 1)
    mean_gross_bps = sum(gross_returns) / max(n, 1)
    avg_turnover = sum(turnovers) / max(n, 1)

    # Sub-period stability (first half vs second half)
    mid = max(n // 2, 1)
    first_half_sharpe = _compute_sharpe(net_returns[:mid])
    second_half_sharpe = _compute_sharpe(net_returns[mid:])
    subperiod_sharpe_gap = abs(first_half_sharpe - second_half_sharpe)

    # Deterministic bootstrap uncertainty on net Sharpe (100 resamples)
    rng = random.Random(seed + 1009)
    boot_sharpes: List[float] = []
    if n >= 5:
        for _ in range(100):
            sample = [net_returns[rng.randrange(n)] for _ in range(n)]
            boot_sharpes.append(_compute_sharpe(sample))
        boot_mean = sum(boot_sharpes) / len(boot_sharpes)
        boot_se = math.sqrt(
            sum((s - boot_mean) ** 2 for s in boot_sharpes) / max(len(boot_sharpes) - 1, 1)
        )
    else:
        boot_se = 0.0

    pred_mean = sum(preds) / max(n, 1)
    pred_std = math.sqrt(sum((p - pred_mean) ** 2 for p in preds) / max(n - 1, 1)) if n > 1 else 1.0

    return {
        "sample_size": n,
        "rmse_bps": round(rmse, 6),
        "information_coefficient": round(ic, 6),
        "hit_rate": round(hit_rate, 6),
        "gross_sharpe": round(gross_sharpe, 6),
        "net_sharpe": round(net_sharpe, 6),
        "net_sharpe_bootstrap_se": round(boot_se, 6),
        "mean_gross_return_bps": round(mean_gross_bps, 6),
        "mean_net_return_bps": round(mean_net_bps, 6),
        "cumulative_net_return_bps": round(sum(net_returns), 4),
        "max_drawdown_pct": round(max_dd, 6),
        "avg_turnover": round(avg_turnover, 6),
        "transaction_cost_bps": float(transaction_cost_bps),
        "first_half_net_sharpe": round(first_half_sharpe, 6),
        "second_half_net_sharpe": round(second_half_sharpe, 6),
        "subperiod_sharpe_gap": round(subperiod_sharpe_gap, 6),
        "prediction_mean": round(pred_mean, 6),
        "prediction_std": round(pred_std if pred_std > 1e-12 else 1.0, 6),
    }


def train_and_evaluate(
    train_records: Sequence[Dict[str, Any]],
    val_records: Sequence[Dict[str, Any]],
    test_records: Sequence[Dict[str, Any]],
    feature_contract: FeatureSetContract,
    job_spec: TrainingJobSpec,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Run deterministic training and produce weights payload and full evaluation manifest."""
    feature_names = feature_contract.feature_names()
    l2_penalty = float(job_spec.hyperparams.get("l2_penalty", 1.0))
    signal_threshold_bps = float(job_spec.hyperparams.get("signal_threshold_bps", 0.0))
    signal_multiplier = float(job_spec.hyperparams.get("signal_multiplier", 1.0))

    means, stds, bin_edges = _compute_feature_stats(train_records, feature_names)
    weights, bias = _fit_ridge_regression(
        train_records=train_records,
        feature_names=feature_names,
        target_column=job_spec.target_column,
        means=means,
        stds=stds,
        l2_penalty=l2_penalty,
        seed=job_spec.seed,
    )

    weights_payload: Dict[str, Any] = {
        "model_family": "standardized_ridge_linear_v1",
        "feature_set_version_id": feature_contract.version_id,
        "feature_names": feature_names,
        "feature_means": means,
        "feature_stds": stds,
        "feature_bin_edges": bin_edges,
        "weights": weights,
        "bias": bias,
        "signal_multiplier": signal_multiplier,
        "signal_threshold_bps": signal_threshold_bps,
        "target_column": job_spec.target_column,
        "target_unit": job_spec.target_unit,
        "currency": job_spec.currency,
        "seed": job_spec.seed,
        "l2_penalty": l2_penalty,
    }

    train_eval = evaluate_predictions(
        train_records,
        weights_payload,
        job_spec.target_column,
        job_spec.transaction_cost_bps,
        signal_threshold_bps=signal_threshold_bps,
        seed=job_spec.seed,
    )
    val_eval = evaluate_predictions(
        val_records,
        weights_payload,
        job_spec.target_column,
        job_spec.transaction_cost_bps,
        signal_threshold_bps=signal_threshold_bps,
        seed=job_spec.seed + 1,
    )
    test_eval = evaluate_predictions(
        test_records,
        weights_payload,
        job_spec.target_column,
        job_spec.transaction_cost_bps,
        signal_threshold_bps=signal_threshold_bps,
        seed=job_spec.seed + 2,
    )

    # Naive baseline: constant buy-and-hold (position = +1)
    bh_returns = [float(r[job_spec.target_column]) for r in test_records]
    baseline_sharpe = round(_compute_sharpe(bh_returns), 6)

    # Attach prediction distribution baseline on training/val for drift monitoring
    weights_payload["baseline_prediction_mean"] = val_eval["prediction_mean"]
    weights_payload["baseline_prediction_std"] = val_eval["prediction_std"]

    evaluation_manifest = {
        "model_name": job_spec.model_name,
        "model_version": job_spec.model_version,
        "strategy_id": job_spec.strategy_id,
        "dataset_snapshot_key": job_spec.dataset_snapshot_key,
        "feature_set_version_id": feature_contract.version_id,
        "code_commit": job_spec.code_commit,
        "seed": job_spec.seed,
        "currency": job_spec.currency,
        "target_unit": job_spec.target_unit,
        "transaction_cost_bps": job_spec.transaction_cost_bps,
        "splits": {
            "train": train_eval,
            "val": val_eval,
            "test": test_eval,
        },
        "baseline_buy_and_hold_test_sharpe": baseline_sharpe,
        "disclaimer": "Simulated historical out-of-sample metrics do not imply or guarantee live trading performance.",
    }
    return weights_payload, evaluation_manifest
