"""v2 pipeline + v3 platform: capabilities, metering, audit, persistence.

The interesting part is not that these pieces exist, but **where they are
inserted**:

```
request
  └─ quota check (before any spend)
  └─ router (v2)
  └─ handler
       ├─ faq / rag    → model call metered through the ledger
       ├─ workflow     → order.lookup executed *by the gateway*, with a
       │                 capability bound to the calling user
       └─ agent        → every tool call the loop makes goes through the
                         gateway; a missing capability is a denial, not an error
  └─ audit event for the whole request
```

If a capability is denied, the user-visible answer must say "no permission",
never "something went wrong" — the distinction is a security control, not UX.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ..mini_agent.tools import Tool, ToolRegistry
from ..mini_agent_v2.memory_hygiene import MemoryStore
from ..mini_agent_v2.pipeline import PipelineRequest
from ..mini_agent_v2.retrieval import HybridRetriever
from ..mini_agent_v2.router import HUMAN, RAG, WORKFLOW, IntentRouter, Route
from .audit import AuditLog
from .billing import Ledger, Quota, QuotaExceeded, to_usage_event
from .capability import CapabilityError, CapabilityIssuer
from .gateway import ToolGateway
from .persistence import RunManager


@dataclass
class GatedRequest(PipelineRequest):
    """A request that also carries the identity money and permissions hang on."""

    tenant_id: str = "acme"
    user_id: str = "user:88"
    granted_actions: set = field(default_factory=set)  # what this user is allowed to do


class GatedPipeline:
    def __init__(
        self,
        *,
        router: IntentRouter,
        retriever: HybridRetriever,
        memory: MemoryStore,
        issuer: CapabilityIssuer,
        audit: AuditLog,
        ledger: Ledger,
        quota: Optional[Quota] = None,
        model: str = "gpt-4.1-mini",
        llm_factory=None,
        runner: Optional[RunManager] = None,
        tenant_id: str = "acme",
    ):
        self.router = router
        self.retriever = retriever
        self.memory = memory
        self.issuer = issuer
        self.audit = audit
        self.ledger = ledger
        self.quota = quota or Quota(ledger)
        self.model = model
        self.llm_factory = llm_factory
        self.runner = runner
        self.tenant_id = tenant_id
        self.orders = {"A1024": "已发货，运输中", "B2048": "已签收"}

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    def handle(self, request: GatedRequest) -> Dict[str, Any]:
        started = time.monotonic()
        trace_id = "trc_" + uuid.uuid4().hex[:10]

        # 1. quota before any spend.  Being over budget is a 402, not a retry.
        try:
            self.quota.check(request.tenant_id)
        except QuotaExceeded as exc:
            self.audit.record(
                "quota",
                actor="service:pipeline",
                subject=request.user_id,
                tenant_id=request.tenant_id,
                decision="deny",
                reason=str(exc),
                request_id=request.request_id,
                trace_id=trace_id,
            )
            return {
                "request_id": request.request_id,
                "trace_id": trace_id,
                "route": "blocked",
                "status": "quota_exceeded",
                "output": "本月调用额度已用完，请联系管理员。",
                "quota": {"tenant": request.tenant_id, "limit": exc.limit, "spent": exc.spent},
                "latency_ms": int((time.monotonic() - started) * 1000),
            }

        # 2. route (v2)
        route = self.router.route(request.text)
        self.audit.record(
            "route",
            actor="service:pipeline",
            subject=request.user_id,
            tenant_id=request.tenant_id,
            action=route.route,
            reason=route.reason,
            request_id=request.request_id,
            trace_id=trace_id,
            detail={"confidence": round(route.confidence, 3)},
        )

        # 3. dispatch
        if route.route == WORKFLOW:
            result = self._workflow(request, route, trace_id)
        elif route.route == RAG:
            result = self._rag(request, route, trace_id)
        else:
            result = self._generic(request, route)

        result["trace_id"] = trace_id
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        result["cost_usd"] = self.ledger.total_for_request(request.request_id)
        self.audit.record(
            "result",
            actor="service:pipeline",
            subject=request.user_id,
            tenant_id=request.tenant_id,
            action=result.get("route", ""),
            decision="allow",
            reason=result.get("status", ""),
            request_id=request.request_id,
            trace_id=trace_id,
            detail={"output_preview": result.get("output", "")[:120]},
        )
        return result

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _workflow(self, request: GatedRequest, route: Route, trace_id: str) -> Dict[str, Any]:
        """The workflow path needs a *real* capability, not a prompt."""
        import re

        match = re.search(r"\b([A-Z]\d{3,})\b", request.text)
        if match is None:
            return self._response(request, WORKFLOW, "clarify", "请问订单号是多少？")

        order_id = match.group(1)
        resource = "order:%s" % order_id
        registry = ToolRegistry([self._order_tool()])

        gateway = ToolGateway(
            registry, self.issuer, self.audit, self.ledger, tenant_id=request.tenant_id
        )

        # Do NOT "fix up" a bad subject: prefixing ``agent:support`` into
        # ``user:agent:support`` would look valid and hand an agent end-user
        # powers — exactly the confused-deputy bug the token exists to stop.
        try:
            capability = self.issuer.issue(
                subject=request.user_id,
                action="order.lookup",
                resource=resource,
            )
        except CapabilityError as exc:
            return self._denied(request, WORKFLOW, str(exc), trace_id)

        outcome = gateway.call(
            tool_name="order.lookup",
            arguments={"order_id": order_id},
            capability=capability,
            request_id=request.request_id,
            trace_id=trace_id,
            seq=0,
            user_id=request.user_id,
        )

        if not outcome.ok and outcome.denied_reason:
            return self._denied(request, WORKFLOW, "无权查询 %s" % order_id, trace_id)
        if not outcome.ok:
            return self._response(
                request, WORKFLOW, "error", "查询订单失败：%s" % (outcome.error or {}).get("message", "")
            )
        return self._response(
            request,
            WORKFLOW,
            "success",
            "订单 %s：%s（查询人 %s）" % (order_id, outcome.data.get("status"), request.user_id),
        )

    def _rag(self, request: GatedRequest, route: Route, trace_id: str) -> Dict[str, Any]:
        hits = self.retriever.search(request.text, request.principals, k=3)
        if not hits:
            return self._response(request, RAG, "success", "知识库里没有找到相关条款。")
        citations = [
            {"ref": "c%d" % (i + 1), "chunk_id": h.chunk.chunk_id, "doc_id": h.chunk.doc_id,
             "section": h.chunk.section}
            for i, h in enumerate(hits)
        ]
        self._meter_model(request, prompt_chars=len(request.text), output_chars=120, seq=0, trace_id=trace_id)
        top = hits[0].chunk
        body = top.text.split("\n", 1)[-1]
        return self._response(
            request,
            RAG,
            "success",
            "%s：%s [c1]" % (top.section, body[:110]),
            citations=citations[:1],
        )

    def _generic(self, request: GatedRequest, route: Route) -> Dict[str, Any]:
        messages = {
            "faq": "如需细节，请补充具体场景。",
            "agent": "该任务需要多步处理，当前演示未接入模型。",
            "clarify": "请问你具体是哪种情况？（查订单 / 退款政策 / 解释条款）",
            "human": "已为你转接人工同事，并把当前信息整理好了。",
        }
        return self._response(
            request,
            route.route,
            "handoff" if route.route == HUMAN else "success",
            messages.get(route.route, "已处理。"),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _order_tool(self) -> Tool:
        orders = self.orders

        def lookup(args: Dict[str, Any]) -> Dict[str, Any]:
            order_id = str(args.get("order_id", ""))
            if order_id not in orders:
                return {"order_id": order_id, "status": "not_found"}
            return {"order_id": order_id, "status": orders[order_id]}

        return Tool(
            name="order.lookup",
            description="按订单号查询订单状态（只读）",
            parameters={
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
                "additionalProperties": False,
            },
            handler=lookup,
        )

    def _meter_model(self, request: GatedRequest, *, prompt_chars: int, output_chars: int,
                     seq: int, trace_id: str, retry_of: Optional[str] = None) -> None:
        usage = {
            "input_tokens": max(1, prompt_chars // 4),
            "output_tokens": max(1, output_chars // 4),
        }
        event = to_usage_event(
            tenant_id=request.tenant_id,
            model=self.model,
            usage=usage,
            request_id=request.request_id,
            seq=seq,
            user_id=request.user_id,
            retry_of=retry_of,
        )
        recorded = self.ledger.record(event)
        self.audit.record(
            "usage",
            actor="service:pipeline",
            subject=request.user_id,
            tenant_id=request.tenant_id,
            action="llm." + self.model,
            reason="recorded" if recorded else "duplicate_suppressed",
            request_id=request.request_id,
            trace_id=trace_id,
            detail={"cost_usd": round(event.cost_usd, 8)},
        )

    def _denied(self, request: GatedRequest, route: str, reason: str, trace_id: str) -> Dict[str, Any]:
        """A permission denial is its own outcome — not a generic error."""
        self.audit.record(
            "tool_denied",
            actor="service:pipeline",
            subject=request.user_id,
            tenant_id=request.tenant_id,
            action=route,
            decision="deny",
            reason=reason,
            request_id=request.request_id,
            trace_id=trace_id,
        )
        return self._response(
            request,
            route,
            "permission_denied",
            "你没有查询该订单的权限，请联系管理员开通。",
        )

    @staticmethod
    def _response(request: GatedRequest, route: str, status: str, output: str,
                  citations=None) -> Dict[str, Any]:
        return {
            "request_id": request.request_id,
            "route": route,
            "status": status,
            "output": output,
            "citations": citations or [],
        }
