"""Append-only, tamper-evident hash-chained audit trail."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from trading_mlops.contracts import Actor, sha256_canonical, utc_now_iso
from trading_mlops.errors import AuditIntegrityError, ErrorCode

GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class AuditEvent:
    """Single immutable event in the hash-chained governance audit log."""

    sequence: int
    timestamp_utc: str
    event_type: str
    actor_id: str
    actor_role: str
    entity_type: str
    entity_id: str
    outcome: str  # "SUCCESS" | "BLOCKED" | "FAILED" | "ALERT"
    details: Dict[str, Any]
    prev_event_hash: str
    event_hash: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sequence": self.sequence,
            "timestamp_utc": self.timestamp_utc,
            "event_type": self.event_type,
            "actor_id": self.actor_id,
            "actor_role": self.actor_role,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "outcome": self.outcome,
            "details": self.details,
            "prev_event_hash": self.prev_event_hash,
            "event_hash": self.event_hash,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AuditEvent":
        return cls(
            sequence=int(data["sequence"]),
            timestamp_utc=data["timestamp_utc"],
            event_type=data["event_type"],
            actor_id=data["actor_id"],
            actor_role=data["actor_role"],
            entity_type=data["entity_type"],
            entity_id=data["entity_id"],
            outcome=data["outcome"],
            details=dict(data["details"]),
            prev_event_hash=data["prev_event_hash"],
            event_hash=data["event_hash"],
        )


class AuditLog:
    """Append-only JSONL audit log with SHA-256 hash chaining."""

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.log_path.exists():
            self.log_path.touch()

    def _compute_hash(self, body: Dict[str, Any]) -> str:
        return sha256_canonical(body)

    def read_events(self) -> List[AuditEvent]:
        events: List[AuditEvent] = []
        if not self.log_path.exists():
            return events
        with self.log_path.open("r", encoding="utf-8") as fh:
            for line_no, raw_line in enumerate(fh, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    events.append(AuditEvent.from_dict(data))
                except Exception as exc:
                    raise AuditIntegrityError(
                        ErrorCode.AUDIT_CHAIN_TAMPERED,
                        f"Corrupted audit log entry at line {line_no}.",
                        {"line": line_no, "error": str(exc)},
                    ) from exc
        return events

    def record(
        self,
        event_type: str,
        actor: Optional[Actor],
        entity_type: str,
        entity_id: str,
        outcome: str,
        details: Optional[Dict[str, Any]] = None,
        timestamp_utc: Optional[str] = None,
    ) -> AuditEvent:
        existing = self.read_events()
        if existing:
            last = existing[-1]
            seq = last.sequence + 1
            prev_hash = last.event_hash
        else:
            seq = 1
            prev_hash = GENESIS_HASH

        ts = timestamp_utc or utc_now_iso()
        actor_id = actor.actor_id if actor else "system"
        actor_role = actor.role.value if actor else "SYSTEM"
        body = {
            "sequence": seq,
            "timestamp_utc": ts,
            "event_type": event_type,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "outcome": outcome,
            "details": dict(details or {}),
            "prev_event_hash": prev_hash,
        }
        event_hash = self._compute_hash(body)
        event = AuditEvent(event_hash=event_hash, **body)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.to_dict(), sort_keys=True, ensure_ascii=True) + "\n")
        return event

    def verify_chain(self) -> Dict[str, Any]:
        """Verify continuity and cryptographic hash of every entry in the audit log."""
        events = self.read_events()
        expected_prev = GENESIS_HASH
        for idx, ev in enumerate(events, start=1):
            if ev.sequence != idx:
                raise AuditIntegrityError(
                    ErrorCode.AUDIT_CHAIN_TAMPERED,
                    f"Audit sequence gap at index {idx}: found {ev.sequence}.",
                    {"expected_sequence": idx, "observed_sequence": ev.sequence},
                )
            if ev.prev_event_hash != expected_prev:
                raise AuditIntegrityError(
                    ErrorCode.AUDIT_CHAIN_TAMPERED,
                    f"Audit hash chain broken at sequence {ev.sequence}.",
                    {
                        "sequence": ev.sequence,
                        "expected_prev_hash": expected_prev,
                        "observed_prev_hash": ev.prev_event_hash,
                    },
                )
            body = ev.to_dict()
            observed_hash = body.pop("event_hash")
            recomputed = self._compute_hash(body)
            if recomputed != observed_hash:
                raise AuditIntegrityError(
                    ErrorCode.AUDIT_CHAIN_TAMPERED,
                    f"Audit event payload tampered at sequence {ev.sequence}.",
                    {
                        "sequence": ev.sequence,
                        "expected_hash": recomputed,
                        "observed_hash": observed_hash,
                    },
                )
            expected_prev = observed_hash
        return {
            "verified": True,
            "event_count": len(events),
            "head_hash": expected_prev,
        }

    def query(
        self,
        entity_id: Optional[str] = None,
        event_type: Optional[str] = None,
        outcome: Optional[str] = None,
    ) -> List[AuditEvent]:
        events = self.read_events()
        result: List[AuditEvent] = []
        for ev in events:
            if entity_id is not None and ev.entity_id != entity_id:
                continue
            if event_type is not None and ev.event_type != event_type:
                continue
            if outcome is not None and ev.outcome != outcome:
                continue
            result.append(ev)
        return result
