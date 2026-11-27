#!/usr/bin/env python3
"""Deterministically generate synthetic chronological financial datasets and provenance manifest."""

from __future__ import annotations

import json
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from trading_mlops.contracts import sha256_bytes, sha256_canonical  # noqa: E402


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def generate_clean_series(
    seed: int,
    num_bars: int,
    start_dt: datetime,
    step_hours: int = 6,
    signal_strength: float = 1.0,
    drift_offset: float = 0.0,
) -> List[Dict[str, Any]]:
    """Generate causal synthetic multi-regime equity signal bars with explicit UTC event & availability timestamps."""
    rng = random.Random(seed)
    records: List[Dict[str, Any]] = []
    # Smooth underlying latent factors so turnover stays realistic while preserving causal signal
    imb = 0.0
    mom = 0.0
    vol = 18.0
    spread = 3.0

    for i in range(num_bars):
        event_dt = start_dt + timedelta(hours=i * step_hours)
        # Availability always >= event time (simulating 15s-90s ETL ingestion latency)
        avail_dt = event_dt + timedelta(seconds=15 + (i % 5) * 15)

        regime_wave = math.sin(2.0 * math.pi * i / 80.0)
        imb = 0.72 * imb + rng.gauss(0.0, 0.35) + 0.12 * regime_wave + drift_offset
        mom = 0.78 * mom + rng.gauss(0.0, 6.0) + 2.5 * regime_wave + drift_offset * 14.0
        vol = max(6.0, 0.85 * vol + 0.15 * (18.0 + 4.0 * abs(regime_wave)) + rng.gauss(0.0, 1.4))
        spread = max(1.0, 0.80 * spread + 0.20 * 3.0 + rng.gauss(0.0, 0.35))

        # Causal forward return in bps (realistic financial SNR with lagged/current available features + noise)
        true_alpha = signal_strength * (6.0 * imb + 0.28 * mom - 0.12 * (spread - 3.0))
        noise = rng.gauss(0.0, 14.0)
        fwd_ret_bps = round(true_alpha + noise, 6)

        records.append(
            {
                "entity_id": "SYN:EQ_ALPHA_01",
                "event_ts_utc": _iso_z(event_dt),
                "availability_ts_utc": _iso_z(avail_dt),
                "currency": "USD",
                "features": {
                    "order_book_imbalance": round(imb, 6),
                    "momentum_24h_bps": round(mom, 6),
                    "realized_vol_24h_bps": round(vol, 6),
                    "bid_ask_spread_bps": round(spread, 6),
                },
                "forward_return_bps": fwd_ret_bps,
            }
        )
    return records


def generate_leaked_series(seed: int, start_dt: datetime) -> List[Dict[str, Any]]:
    """Generate a fixture intentionally containing point-in-time leakage for scenario testing."""
    records = generate_clean_series(seed=seed, num_bars=160, start_dt=start_dt, step_hours=6)
    # Inject point-in-time violation: availability precedes event timestamp at index 25
    bad_event = datetime.fromisoformat(records[25]["event_ts_utc"].replace("Z", "+00:00"))
    records[25]["availability_ts_utc"] = _iso_z(bad_event - timedelta(minutes=10))
    return records


def main() -> None:
    fixtures_dir = REPO_ROOT / "data" / "fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)

    datasets_spec = [
        (
            "market_features_v1.json",
            "syn_alpha_bars_v1",
            "1.0.0",
            generate_clean_series(
                seed=202601,
                num_bars=600,
                start_dt=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
                signal_strength=1.0,
            ),
            "Clean synthetic 150-day chronological multi-regime equity bar dataset for v1 training.",
        ),
        (
            "market_features_v2.json",
            "syn_alpha_bars_v2",
            "1.1.0",
            generate_clean_series(
                seed=202602,
                num_bars=600,
                start_dt=datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc),
                signal_strength=1.05,
            ),
            "Clean synthetic 150-day chronological dataset for v2 candidate training.",
        ),
        (
            "market_features_drifted.json",
            "syn_alpha_bars_drifted",
            "1.0.0",
            generate_clean_series(
                seed=202603,
                num_bars=60,
                start_dt=datetime(2025, 7, 1, 0, 0, tzinfo=timezone.utc),
                signal_strength=0.4,
                drift_offset=2.6,
            ),
            "Synthetic shifted serving batch for M4 PSI and mean-shift drift detection drills.",
        ),
        (
            "market_features_leaked.json",
            "syn_alpha_bars_leaked",
            "1.0.0",
            generate_leaked_series(
                seed=202604,
                start_dt=datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
            ),
            "Fault-injection fixture with point-in-time availability_ts_utc < event_ts_utc leakage.",
        ),
    ]

    manifest_entries: Dict[str, Any] = {}
    feature_schema = [
        "order_book_imbalance",
        "momentum_24h_bps",
        "realized_vol_24h_bps",
        "bid_ask_spread_bps",
    ]
    schema_hash = sha256_canonical({"features": feature_schema, "target": "forward_return_bps"})

    for filename, dataset_id, version, records, description in datasets_spec:
        payload = {
            "dataset_id": dataset_id,
            "version": version,
            "is_synthetic": True,
            "entitlement": "INTERNAL_SYNTHETIC_SANDBOX",
            "license_name": "CC0-1.0-Synthetic",
            "asset_class": "SYNTHETIC_US_EQUITIES",
            "currency": "USD",
            "description": description,
            "records": records,
        }
        out_path = fixtures_dir / filename
        raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        out_path.write_bytes(raw)
        manifest_entries[filename] = {
            "dataset_id": dataset_id,
            "version": version,
            "content_sha256": sha256_bytes(raw),
            "schema_hash": schema_hash,
            "row_count": len(records),
            "start_event_ts_utc": records[0]["event_ts_utc"],
            "end_event_ts_utc": records[-1]["event_ts_utc"],
            "entitlement": "INTERNAL_SYNTHETIC_SANDBOX",
            "license_name": "CC0-1.0-Synthetic",
            "asset_class": "SYNTHETIC_US_EQUITIES",
            "currency": "USD",
            "is_synthetic": True,
            "description": description,
        }

    manifest = {
        "manifest_version": "1.0.0",
        "generator": "scripts/generate_fixtures.py",
        "fixtures": manifest_entries,
    }
    (fixtures_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Generated {len(datasets_spec)} synthetic fixtures in {fixtures_dir}")


if __name__ == "__main__":
    main()
