"""Version-one local monitor contracts. All stored timestamps sort lexically in UTC."""

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone


class Invalid(ValueError):
    """An input, state transition or configuration violates the contract."""


def check(condition, message):
    if not condition:
        raise Invalid(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def stamp(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def instant(value):
    check(isinstance(value, str) and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z", value
    ), "timestamp must be UTC ISO 8601 ending in Z")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise Invalid("invalid calendar timestamp") from exc


def keys(value, expected):
    check(isinstance(value, dict) and set(value) == set(expected),
          "expected fields: " + ", ".join(sorted(expected)))


def identifier(value):
    check(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value),
          "invalid identifier")
    return value


def numeric(value):
    return type(value) in (int, float) and abs(value) <= 1.7976931348623157e308 and math.isfinite(value)


LIMITS = ("ks_threshold", "missing_rate_limit", "missing_rate_increase", "accuracy_drop",
          "brier_increase", "calibration_increase", "min_label_coverage")
COUNTS = ("window_minutes", "step_minutes", "min_samples", "label_horizon_minutes",
          "label_grace_minutes", "cooldown_minutes")


def policy(value):
    keys(value, ("schema_version", "registry_id", "model", "model_version", "features",
                 "baseline_start", "baseline_end") + LIMITS + COUNTS)
    check(type(value["schema_version"]) is int and value["schema_version"] == 1, "unsupported version")
    for key in ("registry_id", "model", "model_version"):
        identifier(value[key])
    check(isinstance(value["features"], dict) and value["features"], "declare feature units")
    for name, unit in value["features"].items():
        identifier(name)
        check(isinstance(unit, str) and 0 < len(unit.strip()) <= 128, "invalid feature unit")
    for key in LIMITS:
        check(numeric(value[key]) and 0 < value[key] <= 1, "invalid " + key)
    for key in COUNTS:
        lower = 0 if key == "label_grace_minutes" else 1
        check(type(value[key]) is int and lower <= value[key] <= 525600, "invalid " + key)
    check(value["min_samples"] >= 2, "min_samples must be at least two")
    check(value["step_minutes"] <= value["window_minutes"], "step exceeds window")
    check(instant(value["baseline_start"]) < instant(value["baseline_end"]), "invalid baseline interval")
    return value


def record(value, cfg):
    check(isinstance(value, dict), "expected JSON object")
    kind = value.get("kind")
    check(kind in ("observation", "label"), "unknown record kind")
    common = ("kind", "id", "available_at")
    keys(value, common + (("event_at", "features", "probability") if kind == "observation" else ("outcome",)))
    identifier(value["id"])
    available = instant(value["available_at"])
    result = dict(value, available_at=stamp(available))
    if kind == "label":
        check(type(value["outcome"]) is int and value["outcome"] in (0, 1), "expected binary outcome")
        return result
    event = instant(value["event_at"])
    check(event <= available, "observation available before event")
    keys(value["features"], cfg["features"])
    check(all(v is None or numeric(v) for v in value["features"].values()), "invalid feature value")
    p = value["probability"]
    check(p is None or (numeric(p) and 0 <= p <= 1), "invalid probability")
    result["event_at"] = stamp(event)
    return result


def window(cfg, end, as_of):
    finish, cutoff = instant(end), instant(as_of)
    start = finish - timedelta(minutes=cfg["window_minutes"])
    anchor = instant(cfg["baseline_end"])
    check(start >= anchor and cutoff >= finish, "window overlaps baseline or extends beyond as-of")
    check((finish - anchor) % timedelta(minutes=cfg["step_minutes"]) == timedelta(0),
          "window end is off the configured schedule")
    return start, finish, cutoff
