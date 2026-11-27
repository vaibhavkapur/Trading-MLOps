"""Original deterministic synthetic binary forecasts. No market or external data."""

import math
import random
from datetime import timedelta

from drift_monitor.contracts import instant, stamp


def hour(start, seed, scenario="stable", strength=0.0, count=80):
    rng = random.Random(seed)
    daypart = 4.0 * math.sin(start.hour * math.pi / 12)
    for i in range(count):
        event = start + timedelta(seconds=(3600 * i) // count)
        momentum = rng.gauss(daypart, 1) + strength
        spread = rng.gauss(5 + daypart / 2, 0.4)
        p = rng.uniform(0.12, 0.88)
        y = int(rng.random() < p)
        if scenario == "missing" and i % 3 == 0:
            spread = None
        if scenario == "degradation":
            y = 1 - y
        if scenario == "prediction":
            p = min(1.0, p + 0.35)
        identifier = start.strftime("s%Y%m%d%H") + "-" + str(i)
        yield {"kind": "observation", "id": identifier, "event_at": stamp(event),
               "available_at": stamp(event), "features": {"momentum": momentum, "spread": spread},
               "probability": p}
        delay = 150 if scenario == "delayed" else 10
        yield {"kind": "label", "id": identifier, "available_at": stamp(event + timedelta(minutes=delay)),
               "outcome": y}


def baseline(cfg):
    start, end = instant(cfg["baseline_start"]), instant(cfg["baseline_end"])
    i = 0
    while start < end:
        yield from hour(start, 49000 + i)
        start += timedelta(hours=1)
        i += 1


def demo_records(cfg):
    yield from baseline(cfg)
    start = instant(cfg["baseline_end"])
    cases = [("stable", 0), ("missing", 1.8), ("stable", 1.8), ("stable", 1.8),
             ("degradation", 0), ("delayed", 0), ("prediction", 0)]
    for i, (case, shift) in enumerate(cases):
        yield from hour(start + timedelta(hours=i), 84000 + i, case, shift)
