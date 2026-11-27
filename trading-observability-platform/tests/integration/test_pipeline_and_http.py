"""Integration tests for the 6-service trading pipeline, idempotency, config integrity, and HTTP endpoints."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from trading_observability.adapters import FixtureMarketDataAdapter
from trading_observability.config import load_platform_config
from trading_observability.pipeline import ObservabilityPlatform, TradingPipeline
from trading_observability.scenarios import DEFAULT_FIXTURE
from trading_observability.server import dispatch_http_get

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestPipelineAndHttpIntegration(unittest.TestCase):
    def test_end_to_end_pipeline_and_idempotency(self) -> None:
        cfg = load_platform_config().with_paper_sandbox_enabled()
        plat = ObservabilityPlatform(config=cfg)
        try:
            adapter = FixtureMarketDataAdapter(fixture_path=DEFAULT_FIXTURE, seed=20261127)
            pipe = TradingPipeline(plat, market_data=adapter, seed=20261127)
            ticks = adapter.next_ticks(limit=3)

            first = pipe.process_tick(ticks[0], client_order_id="CLORD-IDEMP-001")
            duplicate = pipe.process_tick(ticks[0], client_order_id="CLORD-IDEMP-001")

            self.assertEqual(first.order_id, duplicate.order_id)
            self.assertEqual(first.trace_id, duplicate.trace_id)
            self.assertEqual(first.status, "filled")
            self.assertTrue(first.reconciled)
            self.assertEqual(
                first.services_traversed,
                ["market_data", "research", "risk", "oms", "broker", "portfolio"],
            )

            trace = plat.trace_store.get_trace_by_order_id(first.order_id)
            self.assertTrue(trace["found"])
            self.assertEqual(trace["span_count"], 6)

            dash = plat.dashboard_snapshot()
            self.assertEqual(dash["trading_health"]["orders_filled"], 1)
            self.assertEqual(dash["trading_health"]["reconciliation_mismatches"], 0)
            self.assertIn("PROPOSED", dash["slo_disclaimer"])
        finally:
            plat.close()

    def test_http_endpoints_and_role_access_control(self) -> None:
        cfg = load_platform_config().with_paper_sandbox_enabled()
        plat = ObservabilityPlatform(config=cfg)
        pipe = TradingPipeline(
            plat,
            market_data=FixtureMarketDataAdapter(fixture_path=DEFAULT_FIXTURE, seed=20261127),
            seed=20261127,
        )
        results = pipe.run_batch(limit=2)
        prev_token = os.environ.get("TRADING_OBS_AUDITOR_TOKEN")
        os.environ["TRADING_OBS_AUDITOR_TOKEN"] = "unit-test-auditor-secret"
        try:
            status, ctype, body = dispatch_http_get(plat, "/metrics")
            self.assertEqual(status, 200)
            self.assertIn("text/plain", ctype)
            self.assertIn("trading_orders_total", body.decode("utf-8"))

            status, _ct, body = dispatch_http_get(plat, "/health")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body.decode("utf-8"))["status"], "ok")

            status, _ct, body = dispatch_http_get(plat, "/ready")
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body.decode("utf-8"))["ready"])

            status, _ct, body = dispatch_http_get(plat, "/dashboard")
            self.assertEqual(status, 200)
            dash = json.loads(body.decode("utf-8"))
            self.assertEqual(dash["trading_health"]["orders_total"], 2)

            status, _ct, body = dispatch_http_get(plat, f"/traces/{results[0].order_id}")
            self.assertEqual(status, 200)
            tr = json.loads(body.decode("utf-8"))
            self.assertEqual(tr["viewer_role"], "VIEWER")
            self.assertEqual(tr["span_count"], 6)

            # Unauthorized auditor role must receive 403
            status, _ct, _body = dispatch_http_get(
                plat, f"/traces/{results[0].order_id}?role=AUDITOR_AUTHORIZED"
            )
            self.assertEqual(status, 403)

            # Authorized auditor token receives 200
            status, _ct, body = dispatch_http_get(
                plat,
                f"/traces/{results[0].order_id}?role=AUDITOR_AUTHORIZED",
                headers={"X-Auditor-Token": "unit-test-auditor-secret"},
            )
            self.assertEqual(status, 200)
            tr_auth = json.loads(body.decode("utf-8"))
            self.assertEqual(tr_auth["viewer_role"], "AUDITOR_AUTHORIZED")

            # Ready returns 503 when trace backend is unavailable
            plat.trace_store.set_backend_available(False)
            status, _ct, _body = dispatch_http_get(plat, "/ready")
            self.assertEqual(status, 503)
        finally:
            if prev_token is None:
                os.environ.pop("TRADING_OBS_AUDITOR_TOKEN", None)
            else:
                os.environ["TRADING_OBS_AUDITOR_TOKEN"] = prev_token
            plat.close()

    def test_config_and_grafana_json_artifacts_are_valid(self) -> None:
        json_paths = [
            REPO_ROOT / "config" / "platform.json",
            REPO_ROOT / "config" / "slis.json",
            REPO_ROOT / "data" / "fixtures" / "manifest.json",
            REPO_ROOT / "config" / "grafana" / "dashboards" / "trading-health.json",
            REPO_ROOT / "config" / "grafana" / "dashboards" / "trading-latency.json",
            REPO_ROOT / "config" / "grafana" / "dashboards" / "observability-self-health.json",
        ]
        for p in json_paths:
            self.assertTrue(p.exists(), f"Missing artifact: {p}")
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertIsInstance(data, dict)


if __name__ == "__main__":
    unittest.main()
