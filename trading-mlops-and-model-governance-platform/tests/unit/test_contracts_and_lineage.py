"""Unit tests for domain contracts, config guardrails, feature compatibility, and leakage detection."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from trading_mlops.config import GovernanceConfig, LineageConfig, PlatformConfig
from trading_mlops.contracts import (
    DatasetSnapshot,
    FeatureSetContract,
    FeatureSpec,
    TrainingJobSpec,
    parse_utc_timestamp,
)
from trading_mlops.errors import (
    ContractError,
    DataLeakageError,
    ErrorCode,
    FeatureCompatibilityError,
    MlopsError,
    TimestampContractError,
)
from trading_mlops.lineage import validate_and_split_chronologically


def _make_fs(version: str = "1.0.0", imb_ver: str = "1.0.0", unit: str = "ratio") -> FeatureSetContract:
    return FeatureSetContract(
        feature_set_id="eq_micro_alpha_fs",
        version=version,
        owner="quant-fe",
        features=(
            FeatureSpec("order_book_imbalance", imb_ver, "float64", unit, None, "h1"),
            FeatureSpec("momentum_24h_bps", "1.0.0", "float64", "bps", "USD", "h2"),
        ),
    )


def _make_records(n: int = 120, step_hours: int = 12):
    start = datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        ev = start + timedelta(hours=i * step_hours)
        av = ev + timedelta(seconds=30)
        imb = ((i % 7) - 3) * 0.25
        mom = ((i % 5) - 2) * 3.0
        rows.append(
            {
                "entity_id": "SYN:A",
                "event_ts_utc": ev.isoformat().replace("+00:00", "Z"),
                "availability_ts_utc": av.isoformat().replace("+00:00", "Z"),
                "features": {"order_book_imbalance": imb, "momentum_24h_bps": mom},
                "forward_return_bps": round(imb * 4.0 + mom * 0.5 + ((i % 3) - 1) * 5.0, 4),
            }
        )
    return rows


def _make_spec(**overrides) -> TrainingJobSpec:
    fs = _make_fs()
    base = dict(
        idempotency_key="unit-job-1",
        model_name="unit_model",
        model_version="1.0.0",
        strategy_id="strat_unit",
        dataset_snapshot_key="ds@1.0.0+0123456789abcdef",
        feature_set_version_id=fs.version_id,
        code_commit="abcdef123456",
        seed=7,
        train_ratio=0.6,
        val_ratio=0.2,
        test_ratio=0.2,
        embargo_seconds=3600,
        hyperparams={"l2_penalty": 1.0},
    )
    base.update(overrides)
    return TrainingJobSpec(**base)


class TestContractsAndLineage(unittest.TestCase):
    def test_rejects_naive_and_non_utc_timestamps(self) -> None:
        with self.assertRaises(TimestampContractError) as ctx1:
            parse_utc_timestamp("2025-01-01T00:00:00")
        self.assertEqual(ctx1.exception.error_code, ErrorCode.TIMESTAMP_CONTRACT_ERROR)

        with self.assertRaises(TimestampContractError) as ctx2:
            parse_utc_timestamp("2025-01-01T00:00:00+05:30")
        self.assertEqual(ctx2.exception.error_code, ErrorCode.TIMESTAMP_CONTRACT_ERROR)

        dt = parse_utc_timestamp("2025-01-01T00:00:00Z")
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_dataset_snapshot_rejects_inverted_date_range(self) -> None:
        with self.assertRaises(TimestampContractError):
            DatasetSnapshot(
                dataset_id="ds_bad",
                version="1.0.0",
                source_uri="file://bad.json",
                content_sha256="a" * 64,
                schema_hash="b" * 64,
                row_count=10,
                start_event_ts_utc="2025-02-01T00:00:00Z",
                end_event_ts_utc="2025-01-01T00:00:00Z",
                entitlement="INTERNAL",
                license_name="CC0",
                asset_class="EQ",
                currency="USD",
                is_synthetic=True,
            )

    def test_config_forbids_non_goals(self) -> None:
        with self.assertRaises(MlopsError) as ctx1:
            PlatformConfig(allow_automatic_live_promotion=True)
        self.assertEqual(ctx1.exception.error_code, ErrorCode.CONFIGURATION_ERROR)

        with self.assertRaises(MlopsError) as ctx2:
            PlatformConfig(allow_blind_recent_return_retraining=True)
        self.assertEqual(ctx2.exception.error_code, ErrorCode.CONFIGURATION_ERROR)

        with self.assertRaises(MlopsError) as ctx3:
            PlatformConfig(governance=GovernanceConfig(forbid_self_approval=False))
        self.assertEqual(ctx3.exception.error_code, ErrorCode.CONFIGURATION_ERROR)

    def test_feature_set_compatibility_rules(self) -> None:
        m_v1 = _make_fs("1.0.0")
        srv_v1_minor = FeatureSetContract(
            feature_set_id="eq_micro_alpha_fs",
            version="1.1.0",
            owner="quant-fe",
            features=(
                FeatureSpec("order_book_imbalance", "1.0.0", "float64", "ratio", None, "h1"),
                FeatureSpec("momentum_24h_bps", "1.0.0", "float64", "bps", "USD", "h2"),
                FeatureSpec("extra_optional_feat", "1.0.0", "float64", "bps", "USD", "h3"),
            ),
        )
        m_v1.check_serving_compatibility(srv_v1_minor)  # Backward-compatible minor addition

        srv_v2 = _make_fs("2.0.0")
        with self.assertRaises(FeatureCompatibilityError) as ctx:
            m_v1.check_serving_compatibility(srv_v2)
        self.assertEqual(ctx.exception.error_code, ErrorCode.INCOMPATIBLE_FEATURE_VERSION)

        srv_bad_unit = _make_fs("1.0.0", unit="bps")
        with self.assertRaises(FeatureCompatibilityError):
            m_v1.check_serving_compatibility(srv_bad_unit)

    def test_lineage_rejects_availability_leakage_and_future_feature(self) -> None:
        fs = _make_fs()
        spec = _make_spec()
        cfg = LineageConfig()

        rows = _make_records()
        rows[10]["availability_ts_utc"] = "2024-12-31T00:00:00Z"
        with self.assertRaises(DataLeakageError) as ctx1:
            validate_and_split_chronologically(rows, fs, spec, cfg)
        self.assertEqual(ctx1.exception.error_code, ErrorCode.DATA_LEAKAGE_DETECTED)

        rows2 = _make_records()
        rows2[12]["feature_as_of_ts_utc"] = "2026-01-01T00:00:00Z"
        with self.assertRaises(DataLeakageError) as ctx2:
            validate_and_split_chronologically(rows2, fs, spec, cfg)
        self.assertEqual(ctx2.exception.error_code, ErrorCode.DATA_LEAKAGE_DETECTED)

    def test_lineage_rejects_target_leakage_correlation_and_blind_retraining(self) -> None:
        fs = _make_fs()
        spec = _make_spec()
        cfg = LineageConfig()

        # Target-leaking feature (exact copy of forward_return_bps)
        rows = _make_records()
        for r in rows:
            r["features"]["order_book_imbalance"] = r["forward_return_bps"]
        with self.assertRaises(DataLeakageError) as ctx1:
            validate_and_split_chronologically(rows, fs, spec, cfg)
        self.assertEqual(ctx1.exception.error_code, ErrorCode.DATA_LEAKAGE_DETECTED)

        # Blind recent-return retraining policy
        rows_clean = _make_records()
        bad_policy_spec = _make_spec(training_window_policy="recent_returns_only")
        with self.assertRaises(DataLeakageError) as ctx2:
            validate_and_split_chronologically(rows_clean, fs, bad_policy_spec, cfg)
        self.assertEqual(ctx2.exception.error_code, ErrorCode.BLIND_RETRAINING_POLICY_VIOLATION)

        # Short recent window (< 30 days)
        short_rows = _make_records(n=40, step_hours=6)  # 10 days only
        with self.assertRaises(DataLeakageError) as ctx3:
            validate_and_split_chronologically(short_rows, fs, spec, cfg)
        self.assertEqual(ctx3.exception.error_code, ErrorCode.BLIND_RETRAINING_POLICY_VIOLATION)

    def test_chronological_split_enforces_embargo_and_clean_ordering(self) -> None:
        fs = _make_fs()
        spec = _make_spec(embargo_seconds=3600)
        cfg = LineageConfig()
        rows = _make_records(n=120, step_hours=12)
        split = validate_and_split_chronologically(rows, fs, spec, cfg)
        self.assertGreater(len(split.train), 0)
        self.assertGreater(len(split.val), 0)
        self.assertGreater(len(split.test), 0)
        self.assertGreaterEqual(split.summary["train_val_gap_seconds"], 3600)
        self.assertGreaterEqual(split.summary["val_test_gap_seconds"], 3600)

    def test_invalid_semver_raises_contract_error(self) -> None:
        with self.assertRaises(ContractError):
            _make_fs(version="v1.0")


if __name__ == "__main__":
    unittest.main()
