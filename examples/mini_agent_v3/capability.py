"""Capability tokens: what the agent may do, on whose behalf, for how long.

The whole design answers one question: **who is the subject?**

* v1 gave the agent a workspace.  v2 added document ACLs.  Neither stops the
  classic confused deputy: an agent holds broad credentials, a user with no
  permission asks for something, and the agent does it "on its own account".
* The fix is that every call carries a capability naming a *real user*
  (``on_behalf_of``), one action, one resource, an expiry and a use limit.

Rules enforced here (all tested):

1. a capability can never be minted for the agent itself;
2. ``action`` and ``resource`` must match exactly — no wildcards;
3. expired or exhausted tokens are rejected before the tool runs;
4. actions marked high-risk require an ``approval_id`` that resolves to a real
   approval record;
5. revoking a token is immediate (the token list is the source of truth).
"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional, Sequence, Set


class CapabilityError(PermissionError):
    """Raised when a capability is missing, malformed, expired or too broad."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# Actions that can never be auto-granted without a human approval record.
HIGH_RISK_ACTIONS: Set[str] = {
    "order.refund",
    "payment.transfer",
    "email.send",
    "file.delete",
    "db.write",
}

AGENT_SUBJECT_PREFIXES = ("agent:", "service:agent", "agent")


@dataclass
class Capability:
    """A short-lived, least-privilege grant.

    Deliberately *not* serialisable as a bearer token with more fields than
    needed: every extra field is something an attacker can try to steer.
    """

    subject: str  # user:123 — must be a real principal
    action: str  # e.g. "order.refund"
    resource: str  # e.g. "order:A1024"
    expires_at: float
    max_uses: int = 1
    approval_id: Optional[str] = None
    tenant: str = "acme"
    issued_by: str = "tool-gateway"
    id: str = field(default_factory=lambda: "cap_" + uuid.uuid4().hex[:12])
    uses: int = 0
    revoked: bool = False
    constraints: Dict[str, float] = field(default_factory=dict)

    # -- checks ----------------------------------------------------------

    def expired(self, now: Optional[float] = None) -> bool:
        return (now or time.time()) >= self.expires_at

    def exhausted(self) -> bool:
        return self.uses >= self.max_uses

    def allows(self, action: str, resource: str) -> bool:
        """Exact match only.  ``order.*`` or ``*`` must never be accepted."""
        return self.action == action and self.resource == resource

    @property
    def ttl_s(self) -> int:
        return max(0, int(self.expires_at - time.time()))

    def fingerprint(self) -> str:
        payload = f"{self.subject}|{self.action}|{self.resource}|{self.tenant}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self, redact_subject: bool = False) -> Dict[str, object]:
        subject = self.subject
        if redact_subject:
            subject = subject.split(":")[0] + ":***"
        return {
            "id": self.id,
            "subject": subject,
            "tenant": self.tenant,
            "action": self.action,
            "resource": self.resource,
            "expires_at": self.expires_at,
            "ttl_s": self.ttl_s,
            "max_uses": self.max_uses,
            "uses": self.uses,
            "approval_id": self.approval_id,
            "fingerprint": self.fingerprint(),
        }


class CapabilityIssuer:
    """Mints and validates capabilities; owns the approval records."""

    def __init__(self, *, default_ttl_s: float = 300.0, tenant: str = "acme"):
        self.default_ttl_s = default_ttl_s
        self.tenant = tenant
        self._issued: Dict[str, Capability] = {}
        self._approvals: Dict[str, Dict[str, object]] = {}

    # -- approvals -------------------------------------------------------

    def approve(self, action: str, resource: str, approved_by: str, reason: str) -> str:
        """A human approval.  ``approved_by`` must be a human principal."""
        if approved_by.startswith("agent"):
            raise CapabilityError("invalid_approver", "审批人不能是 agent")
        approval_id = "apr_" + uuid.uuid4().hex[:10]
        self._approvals[approval_id] = {
            "action": action,
            "resource": resource,
            "approved_by": approved_by,
            "reason": reason,
            "at": time.time(),
            "consumed": False,
        }
        return approval_id

    # -- issuing ---------------------------------------------------------

    def issue(
        self,
        *,
        subject: str,
        action: str,
        resource: str,
        ttl_s: Optional[float] = None,
        max_uses: int = 1,
        approval_id: Optional[str] = None,
        constraints: Optional[Dict[str, float]] = None,
    ) -> Capability:
        if not subject.startswith("user:"):
            # agent:* / service:* are not legitimate subjects for end-user
            # actions.  This single check removes most confused-deputy bugs.
            raise CapabilityError(
                "invalid_subject",
                "能力令牌必须绑定真实用户（user:*），当前为 %r" % subject,
            )
        if action.endswith(".*") or action == "*" or resource == "*":
            raise CapabilityError("too_broad", "不允许通配符能力")
        if action in HIGH_RISK_ACTIONS:
            approval = self._approvals.get(approval_id or "")
            if approval is None:
                raise CapabilityError(
                    "approval_required", "%s 需要人工审批后才能签发" % action
                )
            if approval["action"] != action or approval["resource"] != resource:
                raise CapabilityError(
                    "approval_mismatch", "审批记录与请求的动作/资源不一致"
                )

        ttl = ttl_s or self.default_ttl_s
        if ttl > 3600:
            raise CapabilityError("ttl_too_long", "能力令牌有效期不得超过 1 小时")

        capability = Capability(
            subject=subject,
            action=action,
            resource=resource,
            expires_at=time.time() + ttl,
            max_uses=max_uses,
            approval_id=approval_id,
            tenant=self.tenant,
            constraints=dict(constraints or {}),
        )
        self._issued[capability.id] = capability
        return capability

    # -- validating ------------------------------------------------------

    def validate(
        self,
        token: Optional[Capability],
        action: str,
        resource: str,
        *,
        allow_exhausted: bool = False,
    ) -> Capability:
        """Check identity, scope and expiry.

        ``allow_exhausted`` exists for exactly one case: an idempotent replay.
        The side effect already happened under this grant, so returning the
        stored result is correct and must not be blocked by ``max_uses``.
        Every other call path leaves it False.
        """
        if token is None:
            raise CapabilityError("missing_capability", "调用工具必须携带能力令牌")
        stored = self._issued.get(token.id)
        if stored is None:
            raise CapabilityError("unknown_capability", "能力令牌不在签发记录中")
        if stored.revoked:
            raise CapabilityError("revoked", "能力令牌已被撤销")
        if stored.expired():
            raise CapabilityError("expired", "能力令牌已过期 (TTL %ds)" % self.default_ttl_s)
        # Order matters: a high-risk grant that already consumed its approval is
        # reported as ``approval_consumed`` (the actionable cause), not just as
        # "exhausted" (a symptom).  Operators act on the first, not the second.
        if stored.approval_id and not allow_exhausted:
            approval = self._approvals.get(stored.approval_id)
            if approval is None or approval["consumed"]:
                raise CapabilityError("approval_consumed", "审批记录不存在或已被使用")
        if stored.exhausted() and not allow_exhausted:
            raise CapabilityError("exhausted", "能力令牌已用尽 (max_uses=%d)" % stored.max_uses)
        if not stored.allows(action, resource):
            raise CapabilityError(
                "scope_mismatch",
                "令牌只授权 %s@%s，不能用于 %s@%s"
                % (stored.action, stored.resource, action, resource),
            )
        return stored

    def consume(self, token: Capability) -> None:
        stored = self._issued.get(token.id)
        if stored is not None:
            stored.uses += 1
            if stored.approval_id:
                approval = self._approvals.get(stored.approval_id)
                if approval is not None:
                    approval["consumed"] = True

    def revoke(self, token_id: str) -> bool:
        stored = self._issued.get(token_id)
        if stored is None:
            return False
        stored.revoked = True
        return True

    def issued(self) -> Sequence[Capability]:
        return list(self._issued.values())
