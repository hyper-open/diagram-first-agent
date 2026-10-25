"""Append-only audit log.

An audit log is not a debug log.  It answers "who did what, under which
authority, and what came of it" — and it must survive the process that wrote it.

Rules:

* **append-only** — no update, no delete method exists on purpose;
* **bound to a principal** — ``subject`` is the real user, ``actor`` is the
  agent/service that acted on their behalf;
* **records the decision, not just the action** — denied calls are logged too;
  otherwise "who tried to do what and was stopped" is invisible;
* **redacted** — the log must not become the biggest PII leak in the system.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..mini_agent_v2.memory_hygiene import redact, scan_text


@dataclass
class AuditEvent:
    kind: str  # capability_issued | tool_call | tool_denied | quota | route | result
    actor: str  # agent:support / service:pipeline
    subject: str = ""  # user:123 — the real person
    tenant_id: str = ""
    action: str = ""
    resource: str = ""
    decision: str = "allow"  # allow | deny | require_approval
    reason: str = ""
    capability_id: str = ""
    request_id: str = ""
    trace_id: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: "aud_" + uuid.uuid4().hex[:12])

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AuditLog:
    """JSONL sink.  One line per event, flushed immediately."""

    def __init__(self, path: Optional[str] = None):
        self._path = Path(path) if path else None
        self._events: List[AuditEvent] = []
        if self._path and self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._events.append(AuditEvent(**json.loads(line)))

    def append(self, event: AuditEvent) -> AuditEvent:
        self._events.append(event)
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.to_dict(), ensure_ascii=False, default=str) + "\n")
        return event

    def record(
        self,
        kind: str,
        *,
        actor: str,
        subject: str = "",
        tenant_id: str = "",
        action: str = "",
        resource: str = "",
        decision: str = "allow",
        reason: str = "",
        capability_id: str = "",
        request_id: str = "",
        trace_id: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> AuditEvent:
        detail = _scrub(detail or {})
        return self.append(
            AuditEvent(
                kind=kind,
                actor=actor,
                subject=subject,
                tenant_id=tenant_id,
                action=action,
                resource=resource,
                decision=decision,
                reason=reason,
                capability_id=capability_id,
                request_id=request_id,
                trace_id=trace_id,
                detail=detail,
            )
        )

    def events(self) -> List[AuditEvent]:
        return list(self._events)

    def by_request(self, request_id: str) -> List[AuditEvent]:
        return [e for e in self._events if e.request_id == request_id]

    def denials(self) -> List[AuditEvent]:
        return [e for e in self._events if e.decision == "deny"]

    def find_leaks(self) -> List[str]:
        """Scan the whole log for secrets/PII that should never have been written."""
        leaky = []
        for event in self._events:
            payload = json.dumps(event.to_dict(), ensure_ascii=False, default=str)
            findings = scan_text(payload)
            if findings:
                leaky.append(event.event_id)
        return leaky


def _scrub(detail: Dict[str, Any]) -> Dict[str, Any]:
    """Redact PII in log payloads before they are written.

    Only values are scrubbed; keys and structure are preserved so queries still
    work.  A log line that cannot be queried is as useless as a leaking one.
    """
    scrubbed: Dict[str, Any] = {}
    for key, value in detail.items():
        if isinstance(value, str):
            cleaned, _ = redact(value)
            scrubbed[key] = cleaned
        elif isinstance(value, dict):
            scrubbed[key] = _scrub(value)
        elif isinstance(value, list):
            scrubbed[key] = [
                _scrub({"v": item})["v"] if isinstance(item, (str, dict)) else item
                for item in value
            ]
        else:
            scrubbed[key] = value
    return scrubbed
