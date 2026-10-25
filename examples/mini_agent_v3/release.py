"""Release gate: preflight checklist + release record.

``preflight_checklist.json`` and ``release_record.json`` in
``examples/templates`` are the data shapes.  This module is the code that
*refuses to ship* when a gate is missing, so "we follow the checklist" becomes
something CI can verify instead of something people remember.

Two design points:

* a gate passes only with **evidence** (a callable/boolean that can fail), not
  with a written "已注意";
* the release record must answer the six questions from the release chapter,
  including how to roll back — a release without a rollback plan is not complete.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List


@dataclass
class Gate:
    id: str
    item: str
    evidence: Callable[[], bool]
    owner: str = "platform"
    blocking: bool = True


@dataclass
class GateResult:
    id: str
    item: str
    passed: bool
    detail: str = ""
    blocking: bool = True


@dataclass
class ReleaseRecord:
    changed_by: str
    approved_by: str
    diff: Dict[str, Any]
    why: Dict[str, Any]
    impact: Dict[str, Any]
    verification: Dict[str, Any]
    rollback: Dict[str, Any]
    change_class: str = "medium"
    release_id: str = field(default_factory=lambda: "rel_" + uuid.uuid4().hex[:10])
    created_at: float = field(default_factory=time.time)

    REQUIRED = ("diff", "why", "verification", "rollback")

    def validate(self) -> List[str]:
        """Return the missing pieces — never raise, the caller reports."""
        missing = []
        for field_name in self.REQUIRED:
            value = getattr(self, field_name)
            if not value:
                missing.append(field_name)
        if "switch" not in self.rollback:
            missing.append("rollback.switch")
        if "previous_version" not in self.rollback:
            missing.append("rollback.previous_version")
        if not self.changed_by or not self.approved_by:
            missing.append("changed_by/approved_by")
        if self.changed_by == self.approved_by:
            missing.append("self_approval")
        return missing

    def to_dict(self) -> Dict[str, Any]:
        return {
            "release_id": self.release_id,
            "changed_by": self.changed_by,
            "approved_by": self.approved_by,
            "change_class": self.change_class,
            "diff": self.diff,
            "why": self.why,
            "impact": self.impact,
            "verification": self.verification,
            "rollback": self.rollback,
            "created_at": self.created_at,
        }


class ReleaseError(RuntimeError):
    def __init__(self, blocked: List[str], failed_gates: List[GateResult]):
        super().__init__(
            "发布被拒绝：缺失 %s；未通过的门禁 %s"
            % (blocked or "无", [g.id for g in failed_gates] or "无")
        )
        self.blocked = blocked
        self.failed_gates = failed_gates


class ReleaseGate:
    def __init__(self, gates: List[Gate]):
        self.gates = gates

    def run(self) -> List[GateResult]:
        results = []
        for gate in self.gates:
            try:
                passed = bool(gate.evidence())
                detail = "ok" if passed else "evidence returned False"
            except Exception as exc:  # noqa: BLE001 - a failing check is a failed gate
                passed = False
                detail = "%s: %s" % (type(exc).__name__, exc)
            results.append(GateResult(gate.id, gate.item, passed, detail, gate.blocking))
        return results

    def ship(self, record: ReleaseRecord) -> Dict[str, Any]:
        results = self.run()
        failed_blocking = [r for r in results if r.blocking and not r.passed]
        missing = record.validate()
        if failed_blocking or missing:
            raise ReleaseError(missing, failed_blocking)
        return {
            "released": True,
            "release_id": record.release_id,
            "gates": [r.__dict__ for r in results],
            "record": record.to_dict(),
        }


def default_gates(
    *,
    acl_leak_count: Callable[[], int],
    duplicate_billing_count: Callable[[], int],
    capability_denials_logged: Callable[[], bool],
    audit_has_leaks: Callable[[], bool],
    tests_passing: Callable[[], bool],
    rollback_switch_available: Callable[[], bool],
) -> List[Gate]:
    """The gates this project can actually evidence today.

    Every callable must be something that can *fail*.  If a gate can only ever
    return True, it is decoration, not a gate.
    """
    return [
        Gate("acl", "检索零 ACL 泄漏", lambda: acl_leak_count() == 0, "security"),
        Gate("billing", "无重复计费", lambda: duplicate_billing_count() == 0, "platform"),
        Gate("audit", "拒绝调用有审计记录", capability_denials_logged, "security"),
        Gate("pii", "审计日志不含 PII", lambda: not audit_has_leaks(), "privacy"),
        Gate("tests", "测试全部通过", tests_passing, "engineering"),
        Gate("rollback", "存在可用的回滚开关", rollback_switch_available, "engineering"),
    ]


def write_record(record: ReleaseRecord, path: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
