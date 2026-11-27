"""Sensitive-field classification, scrubbing firewall, and role-based telemetry redaction.

Enforces the non-goal: no confidential order details or secrets exposed to unauthorized viewers.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from trading_observability.schema import ViewerRole

REDACTED_SECRET = "[REDACTED:SECRET]"
REDACTED_CONFIDENTIAL = "[REDACTED:CONFIDENTIAL_ORDER]"
REDACTED_PII = "[REDACTED:PII]"

# Field names that must ALWAYS be scrubbed regardless of viewer role
SECRET_FIELD_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "api_secret",
        "secret",
        "secret_key",
        "password",
        "passwd",
        "pwd",
        "token",
        "access_token",
        "refresh_token",
        "auth_token",
        "authorization",
        "bearer",
        "private_key",
        "broker_secret",
        "broker_credentials",
        "credential",
        "credentials",
        "session_cookie",
        "signature",
        "hmac",
    }
)

# Confidential order / strategy details restricted from standard telemetry & unauthorized viewers
CONFIDENTIAL_ORDER_KEYS = frozenset(
    {
        "limit_price",
        "stop_price",
        "order_qty",
        "order_quantity",
        "quantity",
        "notional_value",
        "client_account",
        "account_number",
        "account_id",
        "counterparty",
        "prime_broker_account",
        "strategy_params",
        "alpha_weights",
        "alpha_signal_raw",
        "raw_order_payload",
        "execution_instructions",
        "target_position",
        "fill_price_confidential",
    }
)

PII_FIELD_KEYS = frozenset(
    {
        "email",
        "phone",
        "ssn",
        "tax_id",
        "trader_name",
        "client_name",
        "address",
    }
)

# Value patterns that detect secret or PII leaks embedded inside free-text messages
SECRET_VALUE_PATTERNS: Tuple[Tuple[str, re.Pattern[str], str], ...] = (
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]+=*"), f"Bearer {REDACTED_SECRET}"),
    ("sk_live_or_test_key", re.compile(r"\b(?:sk|pk|rk)_(?:live|test|prod)_[A-Za-z0-9]{8,}\b"), REDACTED_SECRET),
    ("kv_secret", re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*[^\s,;\"']+"), r"\1=" + REDACTED_SECRET),
    ("account_number", re.compile(r"(?i)\b(ACCT|ACC|ACCOUNT)[-_#:]?\d{5,16}\b"), REDACTED_CONFIDENTIAL),
    ("email_address", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), REDACTED_PII),
    ("confidential_kv", re.compile(r"(?i)\b(limit_price|order_qty|client_account|strategy_params)\s*[:=]\s*[^\s,;]+"), r"\1=" + REDACTED_CONFIDENTIAL),
)


@dataclass
class ScrubResult:
    sanitized_text: str
    sanitized_attributes: Dict[str, Any]
    redacted_fields: List[str] = field(default_factory=list)
    secret_leak_detected: bool = False
    confidential_leak_detected: bool = False


class SensitiveFieldScrubber:
    """Recursively scrubs secrets, PII, and confidential order details from telemetry."""

    def __init__(
        self,
        on_leak_detected: Optional[Callable[[str, List[str]], None]] = None,
    ) -> None:
        self._on_leak_detected = on_leak_detected

    def scrub_text(self, text: str) -> Tuple[str, List[str], bool, bool]:
        if not text:
            return text, [], False, False
        redacted: List[str] = []
        secret_hit = False
        confidential_hit = False
        out = text
        for pattern_name, pattern, replacement in SECRET_VALUE_PATTERNS:
            new_out, count = pattern.subn(replacement, out)
            if count > 0:
                out = new_out
                redacted.append(f"text:{pattern_name}")
                if replacement.endswith(REDACTED_SECRET):
                    secret_hit = True
                else:
                    confidential_hit = True
        return out, redacted, secret_hit, confidential_hit

    def _scrub_value(
        self,
        key: str,
        value: Any,
        path: str,
        redacted: List[str],
        flags: Dict[str, bool],
    ) -> Any:
        norm_key = key.strip().lower()
        if norm_key in SECRET_FIELD_KEYS or any(
            tok in norm_key for tok in ("secret", "password", "passwd", "api_key", "apikey", "private_key", "auth_token")
        ):
            redacted.append(path)
            flags["secret"] = True
            return REDACTED_SECRET

        if norm_key in CONFIDENTIAL_ORDER_KEYS:
            redacted.append(path)
            flags["confidential"] = True
            return REDACTED_CONFIDENTIAL

        if norm_key in PII_FIELD_KEYS:
            redacted.append(path)
            flags["confidential"] = True
            return REDACTED_PII

        if isinstance(value, dict):
            return {
                str(k): self._scrub_value(str(k), v, f"{path}.{k}" if path else str(k), redacted, flags)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [
                self._scrub_value(key, item, f"{path}[{idx}]", redacted, flags)
                for idx, item in enumerate(value)
            ]
        if isinstance(value, str):
            sanitized, hits, sec_hit, conf_hit = self.scrub_text(value)
            if hits:
                redacted.extend(f"{path}:{h}" for h in hits)
                if sec_hit:
                    flags["secret"] = True
                if conf_hit:
                    flags["confidential"] = True
            return sanitized
        return value

    def scrub_payload(
        self,
        service: str,
        message: str = "",
        attributes: Optional[Dict[str, Any]] = None,
    ) -> ScrubResult:
        redacted: List[str] = []
        flags = {"secret": False, "confidential": False}

        sanitized_msg, msg_hits, msg_sec, msg_conf = self.scrub_text(message)
        if msg_hits:
            redacted.extend(msg_hits)
            flags["secret"] = flags["secret"] or msg_sec
            flags["confidential"] = flags["confidential"] or msg_conf

        sanitized_attrs: Dict[str, Any] = {}
        for k, v in (attributes or {}).items():
            sanitized_attrs[str(k)] = self._scrub_value(str(k), v, str(k), redacted, flags)

        if redacted and self._on_leak_detected is not None:
            self._on_leak_detected(service, list(redacted))

        return ScrubResult(
            sanitized_text=sanitized_msg,
            sanitized_attributes=sanitized_attrs,
            redacted_fields=redacted,
            secret_leak_detected=flags["secret"],
            confidential_leak_detected=flags["confidential"],
        )


def authorize_viewer_role(
    requested_role: ViewerRole,
    provided_token: Optional[str],
    expected_auditor_token: Optional[str],
) -> ViewerRole:
    """Fail-safe role authorization: downgrades unauthorized requests to VIEWER."""
    if requested_role == ViewerRole.AUDITOR_AUTHORIZED:
        if expected_auditor_token and provided_token and provided_token == expected_auditor_token:
            return ViewerRole.AUDITOR_AUTHORIZED
        raise PermissionError(
            "UNAUTHORIZED_AUDITOR_ACCESS: Valid auditor authorization token required for privileged view."
        )
    return requested_role


def project_trace_for_role(
    trace_bundle: Dict[str, Any],
    role: ViewerRole,
    scrubber: Optional[SensitiveFieldScrubber] = None,
) -> Dict[str, Any]:
    """Project a trace bundle for a viewer role; unauthorized viewers never see secrets or confidential fields."""
    active_scrubber = scrubber or SensitiveFieldScrubber()
    cloned = copy.deepcopy(trace_bundle)
    # Secrets and confidential order fields are NEVER exposed to VIEWER or OPERATOR.
    # Even AUDITOR_AUTHORIZED never receives raw secrets (API keys/tokens remain redacted).
    for span in cloned.get("spans", []):
        attrs = span.get("attributes", {})
        res = active_scrubber.scrub_payload(span.get("service", "unknown"), "", attrs)
        span["attributes"] = res.sanitized_attributes
        if role == ViewerRole.VIEWER:
            # Extra defense-in-depth for basic viewers: mask client_order_id / strategy_id if present
            for restricted in ("client_order_id", "strategy_id", "venue_session_id"):
                if restricted in span["attributes"]:
                    span["attributes"][restricted] = REDACTED_CONFIDENTIAL
    cloned["viewer_role"] = role.value
    return cloned
