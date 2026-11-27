"""Unit tests for canonical telemetry schema, redaction firewall, role authorization, and execution policy."""

from __future__ import annotations

import unittest

from trading_observability.config import ExecutionPolicyConfig, load_platform_config
from trading_observability.redaction import (
    REDACTED_CONFIDENTIAL,
    REDACTED_PII,
    REDACTED_SECRET,
    SensitiveFieldScrubber,
    authorize_viewer_role,
    project_trace_for_role,
)
from trading_observability.schema import (
    CorrelationContext,
    ViewerRole,
    generate_span_id,
    generate_trace_id,
    parse_utc_iso_to_ns,
)


class TestSchemaAndRedaction(unittest.TestCase):
    def test_w3c_traceparent_roundtrip(self) -> None:
        tid = generate_trace_id("seed-1")
        sid = generate_span_id("span-1")
        ctx = CorrelationContext(
            trace_id=tid,
            span_id=sid,
            correlation_id="corr-001",
            service="market_data",
            order_id="ORD-000001",
            feed_batch_id="FB-0001",
        )
        headers = ctx.to_headers()
        self.assertEqual(headers["traceparent"], f"00-{tid}-{sid}-01")

        child = CorrelationContext.from_headers(headers, service="research")
        self.assertEqual(child.trace_id, tid)
        self.assertEqual(child.parent_span_id, sid)
        self.assertEqual(child.correlation_id, "corr-001")
        self.assertEqual(child.order_id, "ORD-000001")
        self.assertEqual(child.feed_batch_id, "FB-0001")

    def test_invalid_trace_context_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CorrelationContext(
                trace_id="not-valid-hex",
                span_id=generate_span_id(),
                correlation_id="corr-1",
                service="oms",
            )
        with self.assertRaises(ValueError):
            CorrelationContext.from_headers({"traceparent": "bad-header"}, service="oms")

    def test_utc_timestamp_requires_timezone(self) -> None:
        ns = parse_utc_iso_to_ns("2026-11-27T10:00:00.000000Z")
        self.assertGreater(ns, 0)
        with self.assertRaises(ValueError):
            parse_utc_iso_to_ns("2026-11-27T10:00:00")

    def test_sensitive_field_scrubber_blocks_secrets_confidential_and_pii(self) -> None:
        detected = []
        scrubber = SensitiveFieldScrubber(on_leak_detected=lambda svc, fields: detected.extend(fields))
        res = scrubber.scrub_payload(
            service="oms",
            message="Order failed Bearer tok_abc123 api_key=MYSECRET999 email trader@example.com limit_price=105.25",
            attributes={
                "symbol": "SYN-EQ-01",
                "api_key": "sk_live_0000111122223333",
                "nested": {
                    "client_account": "ACCT-12345678",
                    "strategy_params": {"alpha": 0.9},
                    "email": "ops@saat.internal",
                },
                "order_qty": 100,
            },
        )
        self.assertTrue(res.secret_leak_detected)
        self.assertTrue(res.confidential_leak_detected)
        self.assertNotIn("tok_abc123", res.sanitized_text)
        self.assertNotIn("MYSECRET999", res.sanitized_text)
        self.assertNotIn("trader@example.com", res.sanitized_text)
        self.assertNotIn("105.25", res.sanitized_text)
        self.assertEqual(res.sanitized_attributes["symbol"], "SYN-EQ-01")
        self.assertEqual(res.sanitized_attributes["api_key"], REDACTED_SECRET)
        self.assertEqual(res.sanitized_attributes["order_qty"], REDACTED_CONFIDENTIAL)
        self.assertEqual(res.sanitized_attributes["nested"]["client_account"], REDACTED_CONFIDENTIAL)
        self.assertEqual(res.sanitized_attributes["nested"]["strategy_params"], REDACTED_CONFIDENTIAL)
        self.assertEqual(res.sanitized_attributes["nested"]["email"], REDACTED_PII)
        self.assertGreater(len(detected), 0)

    def test_role_authorization_and_trace_projection(self) -> None:
        self.assertEqual(authorize_viewer_role(ViewerRole.VIEWER, None, None), ViewerRole.VIEWER)
        with self.assertRaises(PermissionError):
            authorize_viewer_role(ViewerRole.AUDITOR_AUTHORIZED, "wrong-token", "correct-token")
        self.assertEqual(
            authorize_viewer_role(ViewerRole.AUDITOR_AUTHORIZED, "correct-token", "correct-token"),
            ViewerRole.AUDITOR_AUTHORIZED,
        )

        bundle = {
            "order_id": "ORD-1",
            "spans": [
                {
                    "service": "oms",
                    "attributes": {
                        "symbol": "SYN-EQ-01",
                        "client_order_id": "CLORD-1",
                        "limit_price": 100.5,
                        "api_key": "sk_live_TEST12345678",
                    },
                }
            ],
        }
        projected = project_trace_for_role(bundle, ViewerRole.VIEWER)
        attrs = projected["spans"][0]["attributes"]
        self.assertEqual(attrs["symbol"], "SYN-EQ-01")
        self.assertEqual(attrs["limit_price"], REDACTED_CONFIDENTIAL)
        self.assertEqual(attrs["client_order_id"], REDACTED_CONFIDENTIAL)
        self.assertEqual(attrs["api_key"], REDACTED_SECRET)

    def test_execution_policy_fails_closed_by_default(self) -> None:
        cfg = load_platform_config()
        self.assertEqual(cfg.execution_policy.mode, "DISABLED")
        with self.assertRaises(PermissionError):
            cfg.execution_policy.validate_for_paper_sandbox()

        live_policy = ExecutionPolicyConfig(mode="LIVE", allow_live_execution=True)
        with self.assertRaises(PermissionError):
            live_policy.validate_for_paper_sandbox()

        paper_cfg = cfg.with_paper_sandbox_enabled()
        paper_cfg.execution_policy.validate_for_paper_sandbox()


if __name__ == "__main__":
    unittest.main()
