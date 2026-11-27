"""Deterministic point-in-time comparisons. Distribution distances are effect sizes."""

from collections import defaultdict
from datetime import timedelta

from .contracts import instant, stamp


def ks_distance(left, right):
    """Two-sample empirical CDF distance, advancing tied values together."""
    counts = defaultdict(lambda: [0, 0])
    for i, sample in enumerate((left, right)):
        for value in sample:
            counts[value][i] += 1
    a = b = 0
    maximum = 0
    for value in sorted(counts):
        x, y = counts[value]
        a += x
        b += y
        maximum = max(maximum, abs(a * len(right) - b * len(left)))
    return maximum / (len(left) * len(right))


def performance(pairs):
    n = len(pairs)
    buckets = defaultdict(float)
    correct = error = 0
    for p, y in pairs:
        correct += (p >= 0.5) == y
        error += (p - y) ** 2
        buckets[min(9, int(10 * p))] += p - y
    return {"accuracy": correct / n, "brier": error / n,
            "calibration": sum(abs(v) for v in buckets.values()) / n}


def cohort(rows, cutoff, cfg):
    maturity = cutoff - timedelta(minutes=cfg["label_horizon_minutes"] + cfg["label_grace_minutes"])
    boundary = stamp(maturity)
    mature = [r for r in rows if r["event_at"] <= boundary]
    observed = [r for r in mature if r["outcome"] is not None]
    pairs = [(r["probability"], r["outcome"]) for r in observed if r["probability"] is not None]
    return pairs, {"mature": len(mature), "pending": len(rows) - len(mature),
                   "overdue": len(mature) - len(observed), "usable": len(pairs),
                   "coverage": len(observed) / len(mature) if mature else None}


def compare(cfg, base, current, hour, cutoff):
    result = {"utc_hour": hour, "baseline_count": len(base), "current_count": len(current), "checks": []}
    checks = result["checks"]
    minimum = cfg["min_samples"]
    if min(len(base), len(current)) < minimum:
        checks.append(dict(signal="sample_size", status="inconclusive", minimum=minimum))
        return result

    series = [("prediction", [r["probability"] for r in base], [r["probability"] for r in current])]
    for name in sorted(cfg["features"]):
        series.append(("feature." + name, [r["features"][name] for r in base],
                       [r["features"][name] for r in current]))
    distributions = []
    input_failure = False
    for name, reference, recent in series:
        left = [v for v in reference if v is not None]
        right = [v for v in recent if v is not None]
        a = (len(reference) - len(left)) / len(reference)
        b = (len(recent) - len(right)) / len(recent)
        failed = max(a, b) >= cfg["missing_rate_limit"] or b - a >= cfg["missing_rate_increase"]
        input_failure |= failed
        checks.append(dict(signal="missing." + name, status="data_quality" if failed else "ok",
                           baseline=a, current=b, absolute_limit=cfg["missing_rate_limit"],
                           increase_limit=cfg["missing_rate_increase"]))
        distributions.append((name, left, right))

    for name, left, right in distributions:
        item = dict(signal="ks." + name, baseline_usable=len(left), current_usable=len(right),
                    threshold=cfg["ks_threshold"])
        if input_failure:
            item["status"] = "blocked_by_quality"
        elif min(len(left), len(right)) < minimum:
            item["status"] = "inconclusive"
        else:
            distance = ks_distance(left, right)
            item.update(value=distance, status="distribution_shift" if distance >= item["threshold"] else "ok")
        checks.append(item)

    reference, a = cohort(base, instant(cfg["baseline_end"]), cfg)
    recent, b = cohort(current, cutoff, cfg)
    result["label_cohorts"] = {"baseline": a, "current": b}
    failing = any(c["mature"] >= minimum and c["coverage"] < cfg["min_label_coverage"] for c in (a, b))
    checks.append(dict(signal="label_coverage", status="data_quality" if failing else
                       ("inconclusive" if min(a["mature"], b["mature"]) < minimum else "ok"),
                       baseline=a["coverage"], current=b["coverage"], minimum=cfg["min_label_coverage"]))
    if input_failure or failing:
        checks.append(dict(signal="performance", status="blocked_by_quality"))
    elif min(len(reference), len(recent)) < minimum:
        checks.append(dict(signal="performance", status="pending_labels" if b["pending"] else "inconclusive"))
    else:
        before, after = performance(reference), performance(recent)
        for name, limit in (("accuracy", "accuracy_drop"), ("brier", "brier_increase"),
                            ("calibration", "calibration_increase")):
            delta = after[name] - before[name]
            deterioration = -delta if name == "accuracy" else delta
            checks.append(dict(signal=name, baseline=before[name], current=after[name],
                               deterioration=deterioration, threshold=cfg[limit],
                               status="performance_degradation" if deterioration >= cfg[limit] else "ok"))
    return result


def analyze(cfg, baseline, recent, hours, cutoff):
    def by_hour(rows):
        groups = defaultdict(list)
        for row in rows:
            groups[int(row["event_at"][11:13])].append(row)
        return groups
    reference, current = by_hour(baseline), by_hour(recent)
    return [compare(cfg, reference[h], current[h], h, cutoff) for h in hours]


def window_hours(start, end):
    cursor = start.replace(minute=0, second=0, microsecond=0)
    hours = set()
    while cursor < end and len(hours) < 24:
        hours.add(cursor.hour)
        cursor += timedelta(hours=1)
    return sorted(hours)
