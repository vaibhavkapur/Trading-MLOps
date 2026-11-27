"""Lightweight local HTTP server exposing Prometheus /metrics, /health, /ready, /dashboard, /alerts, and /traces/<order_id>."""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from trading_observability.pipeline import ObservabilityPlatform
from trading_observability.redaction import authorize_viewer_role
from trading_observability.schema import ViewerRole


def _json_response(payload: Dict[str, Any], status: int = HTTPStatus.OK) -> Tuple[int, str, bytes]:
    body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    return int(status), "application/json; charset=utf-8", body


def dispatch_http_get(
    platform: ObservabilityPlatform,
    raw_path: str,
    headers: Optional[Mapping[str, str]] = None,
) -> Tuple[int, str, bytes]:
    """Pure HTTP GET router shared by the HTTP server and deterministic in-process integration tests."""
    parsed = urlparse(raw_path)
    path = parsed.path.rstrip("/") or "/"
    qs = parse_qs(parsed.query)
    hdrs = {k.lower(): v for k, v in (headers or {}).items()}

    if path == "/metrics":
        body = platform.metrics.export_prometheus().encode("utf-8")
        return int(HTTPStatus.OK), "text/plain; version=0.0.4; charset=utf-8", body

    if path == "/health":
        return _json_response(
            {
                "status": "ok",
                "trace_backend_available": platform.trace_store.is_backend_available,
                "spool_depth": platform.trace_store.spool_depth,
            }
        )

    if path == "/ready":
        ready = platform.trace_store.is_backend_available
        code = HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE
        return _json_response({"ready": ready, "backend": "trace_store"}, status=code)

    if path == "/dashboard":
        return _json_response(platform.dashboard_snapshot())

    if path == "/alerts":
        return _json_response(
            {
                "active_alerts": [a.to_dict() for a in platform.router.active_alerts()],
                "routed_notifications": platform.router.routed_notifications(),
            }
        )

    if path.startswith("/traces/"):
        order_id = path.split("/traces/", 1)[1]
        req_role_str = (qs.get("role", [platform.config.security.default_viewer_role])[0]).upper()
        try:
            req_role = ViewerRole(req_role_str)
        except ValueError:
            return _json_response({"error": "INVALID_VIEWER_ROLE"}, status=HTTPStatus.BAD_REQUEST)

        auth_token = hdrs.get("x-auditor-token")
        try:
            effective_role = authorize_viewer_role(
                req_role,
                provided_token=auth_token,
                expected_auditor_token=platform.config.security.resolve_auditor_token(),
            )
        except PermissionError as exc:
            return _json_response({"error": str(exc)}, status=HTTPStatus.FORBIDDEN)

        bundle = platform.trace_store.get_trace_by_order_id(order_id, role=effective_role)
        status = HTTPStatus.OK if bundle.get("found") else HTTPStatus.NOT_FOUND
        return _json_response(bundle, status=status)

    return _json_response(
        {
            "error": "NOT_FOUND",
            "endpoints": ["/metrics", "/health", "/ready", "/dashboard", "/alerts", "/traces/<order_id>"],
        },
        status=HTTPStatus.NOT_FOUND,
    )


def create_observability_server(
    platform: ObservabilityPlatform,
    host: str = "127.0.0.1",
    port: int = 9108,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            status, content_type, body = dispatch_http_get(
                platform, self.path, headers=dict(self.headers.items())
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, port), Handler)
