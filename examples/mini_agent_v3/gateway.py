"""The tool gateway: one choke point for authorization, execution and audit.

Everything that could touch the outside world goes through ``ToolGateway.call``,
which enforces a fixed order:

```
quota -> capability -> approval -> schema -> idempotency -> execute -> sanitize -> audit
```

Why this order:

* **quota first**: never spend tokens/actions for a tenant that is out of budget;
* **capability before execution**: the tool must not run even partially for an
  unauthorized call (this is where "check permission inside the tool" fails);
* **idempotency before execution**: a retry returns the stored result instead
  of repeating the side effect;
* **sanitize after execution**: tool output is *untrusted input* and is scanned
  as such before it can reach the model;
* **audit always**: denials are logged as loudly as successes, otherwise you
  cannot answer "who tried".

v1's ``ToolRegistry`` is reused for validation and execution details; the
gateway wraps it rather than replacing it.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from ..mini_agent.guard import Guard
from ..mini_agent.tools import Tool, ToolRegistry
from ..mini_agent.types import ToolCall, ToolResult
from .audit import AuditLog
from .billing import Ledger, UsageEvent
from .capability import Capability, CapabilityError, CapabilityIssuer


@dataclass
class GatewayResult:
    ok: bool
    data: Any = None
    error: Optional[Dict[str, Any]] = None
    denied_reason: str = ""
    cached: bool = False
    latency_ms: int = 0
    capability_id: str = ""

    @classmethod
    def from_tool_result(cls, result: ToolResult) -> "GatewayResult":
        return cls(ok=result.ok, data=result.data, error=result.error)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "denied_reason": self.denied_reason,
            "cached": self.cached,
            "latency_ms": self.latency_ms,
        }


class IdempotencyStore:
    """Remembers a result per idempotency key.

    The key must be composed by the *host* (``request_id:tool:seq``), never by
    the model, otherwise a prompt can talk its way into double execution.
    """

    def __init__(self) -> None:
        self._results: Dict[str, Dict[str, Any]] = {}

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        return self._results.get(key)

    def put(self, key: str, payload: Dict[str, Any]) -> None:
        self._results[key] = payload

    def __len__(self) -> int:
        return len(self._results)


class ToolGateway:
    def __init__(
        self,
        registry: ToolRegistry,
        issuer: CapabilityIssuer,
        audit: AuditLog,
        ledger: Ledger,
        *,
        tenant_id: str = "acme",
        guard: Optional[Guard] = None,
        idempotency: Optional[IdempotencyStore] = None,
        actor: str = "agent:support",
    ):
        self.registry = registry
        self.issuer = issuer
        self.audit = audit
        self.ledger = ledger
        self.tenant_id = tenant_id
        self.guard = guard
        self.idempotency = idempotency or IdempotencyStore()
        self.actor = actor
        # Resource resolution is pluggable: a tool may expose a ``resource``
        # attribute or the caller may pass it explicitly.
        self.resource_for: Callable[[str, Dict[str, Any]], str] = _default_resource

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def call(
        self,
        *,
        tool_name: str,
        arguments: Dict[str, Any],
        capability: Optional[Capability],
        request_id: str,
        trace_id: str = "",
        resource: Optional[str] = None,
        seq: int = 0,
        user_id: str = "",
    ) -> GatewayResult:
        started = time.monotonic()
        resolved = resource or self.resource_for(tool_name, arguments)

        # 1. capability (a missing token is denied, never "allowed by default")
        #    Note: on an idempotent replay we still require *a* valid-looking
        #    capability, but we do not consume a use — the side effect already
        #    happened.  That is why the replay check comes right after validation.
        idem_key = f"{request_id}:{tool_name}:{seq}"
        replay = self.idempotency.get(idem_key) is not None
        try:
            token = self.issuer.validate(
                capability, tool_name, resolved, allow_exhausted=replay
            )
        except CapabilityError as exc:
            self._audit(
                "tool_denied",
                decision="deny",
                action=tool_name,
                resource=resolved,
                reason="%s: %s" % (exc.code, exc),
                subject=capability.subject if capability else "",
                request_id=request_id,
                trace_id=trace_id,
                capability_id=capability.id if capability else "",
            )
            return GatewayResult(
                ok=False,
                denied_reason=exc.code,
                error={
                    "type": "permission_denied",
                    "message": str(exc),
                    "retryable": False,
                    "hint": "需要为当前用户申请 %s@%s 的能力令牌" % (tool_name, resolved),
                },
                latency_ms=int((time.monotonic() - started) * 1000),
            )

        # 2. idempotency before any side effect
        cached = self.idempotency.get(idem_key)
        if cached is not None:
            self._audit(
                "tool_call",
                decision="allow",
                action=tool_name,
                resource=resolved,
                reason="idempotent_replay",
                subject=token.subject,
                request_id=request_id,
                trace_id=trace_id,
                capability_id=token.id,
            )
            return GatewayResult(
                ok=bool(cached.get("ok")),
                data=cached.get("data"),
                error=cached.get("error"),
                cached=True,
                latency_ms=int((time.monotonic() - started) * 1000),
                capability_id=token.id,
            )

        # 3. execute through the v1 registry (schema validation + timeouts live there)
        result = self.registry.execute(ToolCall(name=tool_name, arguments=arguments))

        # 4. tool output is untrusted input: scan it before it reaches the model
        if result.ok and self.guard is not None:
            gate = self.guard.check_json(result.data, stage="tool_result")
            if gate.blocked:
                result = ToolResult.failure(
                    "policy_blocked",
                    "工具返回内容命中了安全策略，已拦截",
                    retryable=False,
                    hint="该结果包含不可信指令或敏感信息，请换一种方式获取信息",
                )

        # 5. consume the grant only after a successful attempt
        self.issuer.consume(token)
        self.idempotency.put(idem_key, result.to_dict())

        self._audit(
            "tool_call",
            decision="allow",
            action=tool_name,
            resource=resolved,
            reason="ok" if result.ok else "tool_error",
            subject=token.subject,
            request_id=request_id,
            trace_id=trace_id,
            capability_id=token.id,
            detail={"arguments": arguments, "ok": result.ok},
        )
        self._charge_tool(tool_name, request_id, token)

        return GatewayResult(
            ok=result.ok,
            data=result.data,
            error=result.error,
            latency_ms=int((time.monotonic() - started) * 1000),
            capability_id=token.id,
        )

    # ------------------------------------------------------------------

    def _charge_tool(self, tool_name: str, request_id: str, token: Capability) -> None:
        self.ledger.record(
            UsageEvent(
                tenant_id=self.tenant_id,
                kind="tool",
                tool_name=tool_name,
                tool_calls=1,
                cost_usd=0.0,  # internal tools are not metered by price in this toy
                idempotency_key=f"{request_id}:tool:{tool_name}:{token.id}",
                request_id=request_id,
                user_id=token.subject,
            )
        )

    def _audit(self, kind: str, **kwargs: Any) -> None:
        self.audit.record(
            kind,
            actor=self.actor,
            tenant_id=self.tenant_id,
            **kwargs,
        )


def _default_resource(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Map a tool call to the resource it touches.

    The mapping is explicit and boring on purpose: a fuzzy mapping is a bypass.
    """
    if tool_name.startswith("order."):
        return "order:%s" % arguments.get("order_id", "unknown")
    if tool_name.startswith("kb."):
        return "kb:all"
    if tool_name.startswith("file."):
        return "file:%s" % arguments.get("path", "unknown")
    return "tool:%s" % tool_name
