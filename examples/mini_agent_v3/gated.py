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
    # Full original request, used to resolve entities shared across clauses of a
    # multi-intent message (e.g. an order id in a different clause).
    context_text: str = ""


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

        # 2. route — multi-intent aware.  A single-label router silently drops
        #    "我要退款，另外帮我查下 A1024" into one of the two asks; route_multi
        #    splits clauses so both are handled.
        multi = self.router.route_multi(request.text)
        route = multi.primary
        self.audit.record(
            "route",
            actor="service:pipeline",
            subject=request.user_id,
            tenant_id=request.tenant_id,
            action=route.route,
            reason=route.reason,
            request_id=request.request_id,
            trace_id=trace_id,
            detail={
                "confidence": round(route.confidence, 3),
                "is_multi": multi.is_multi,
                "intents": [r.intent or r.route for r in multi.all_routes()],
            },
        )

        # 3. dispatch.  Each distinct scenario runs on its own clause; results
        #    are merged with the primary first.  High-risk is always primary,
        #    so a complaint can never be buried by a refund request.
        result = self._dispatch_multi(request, multi, trace_id)

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
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch_multi(self, request: GatedRequest, multi, trace_id: str) -> Dict[str, Any]:
        """Run every scenario and merge.  Single-intent requests fast-path."""
        if not multi.is_multi:
            return self._dispatch_one(request, multi.primary, request.text, trace_id)

        parts = []
        routes_used = []
        for scenario in multi.scenarios:
            # Run each clause against its own text so handlers see the ask,
            # not the whole compound sentence.
            sub_request = _clone_request(request, scenario.text, context=request.text)
            part = self._dispatch_one(sub_request, scenario.route, scenario.text, trace_id)
            parts.append((scenario, part))
            routes_used.append(scenario.route.route)

        # Primary first, then the rest in priority order.
        order = {id(s): i for i, s in enumerate(multi.scenarios)}

        def priority(item):
            scenario, _ = item
            return (0 if scenario.route is multi.primary else 1, order[id(scenario)])

        parts.sort(key=priority)
        merged = "\n\n".join(
            "【%s】%s" % (_label(scenario.route), part.get("output", ""))
            for scenario, part in parts
        )
        # Status: reduce — any permission/quota problem dominates.
        statuses = [p.get("status") for _, p in parts]
        status = _reduce_status(statuses)
        return {
            "request_id": request.request_id,
            "route": multi.primary.route,
            "status": status,
            "output": merged,
            "is_multi": True,
            "scenarios": [
                {"text": sc.text, "route": sc.route.route,
                 "intent": sc.route.intent, "status": part.get("status")}
                for sc, part in parts
            ],
            "citations": [

                c for _, part in parts for c in part.get("citations", [])
            ],
        }

    def _dispatch_one(self, request: GatedRequest, route, text: str, trace_id: str) -> Dict[str, Any]:
        if route.route == WORKFLOW:
            return self._workflow(request, route, trace_id)
        if route.route == RAG:
            return self._rag(request, route, trace_id)
        return self._generic(request, route)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _workflow(self, request: GatedRequest, route: Route, trace_id: str) -> Dict[str, Any]:
        """The workflow path needs a *real* capability, not a prompt."""
        import re

        # Entity resolution: an order id may live in the *original* compound
        # sentence, not in the clause we are handling.
        haystack = request.text + " " + getattr(request, "context_text", "")
        match = re.search(r"\b([A-Z]\d{3,})\b", haystack)
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

        status = outcome.data.get("status")
        # Same route, different intent: a refund is not a lookup.  We resolve the
        # order the same way, but the *action* differs — and a refund is a
        # destructive action that requires approval, so we do not perform it here.
        if route.intent == "refund.request":
            return self._response(
                request, WORKFLOW, "success",
                "订单 %s 当前状态：%s。退款申请已受理，需人工审批后执行"
                "（金额类动作走审批，不会自动退款）。" % (order_id, status),
            )
        return self._response(
            request,
            WORKFLOW,
            "success",
            "订单 %s：%s（查询人 %s）" % (order_id, status, request.user_id),
        )

    # Minimum RRF score to treat a retrieval hit as relevant.  Tuned on the seed
    # corpus: genuine matches score ~0.03+, unrelated text ~0.016.  Without a
    # floor, a technical question ("这个功能报错了") gets answered with the first
    # arbitrary paragraph in the KB — worse than admitting we don't know.
    RAG_MIN_SCORE = 0.025

    def _rag(self, request: GatedRequest, route: Route, trace_id: str) -> Dict[str, Any]:
        raw_hits = self.retriever.search(request.text, request.principals, k=3)
        hits = [h for h in raw_hits if h.score >= self.RAG_MIN_SCORE]
        if not hits:
            return self._response(
                request, RAG, "success",
                "知识库中没有与该问题相关的内容（可能属于技术问题，需转技术支持）。",
            )
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


# ---------------------------------------------------------------------------
# Multi-intent helpers
# ---------------------------------------------------------------------------


def _clone_request(request: GatedRequest, text: str, context: str = "") -> GatedRequest:
    """Same identity, different text.

    ``text`` is the clause to act on; ``context`` is the whole original request.
    Multi-intent requests often put shared entities (an order id) in a different
    clause than the ask that needs them — "我要退款，订单 A1024" splits into a
    refund ask and an id.  Handlers must still be able to see the id, so we keep
    the full sentence available as context.
    """
    clone = GatedRequest(
        text=text,
        tenant_id=request.tenant_id,
        user_id=request.user_id,
        principals=set(request.principals),
        granted_actions=set(request.granted_actions),
        request_id=request.request_id,
    )
    # Attach the full request for entity resolution (order id, dates, etc.).
    clone.context_text = context or request.text
    return clone


def _label(route) -> str:
    names = {
        "workflow": "业务办理",
        "rag": "问题排查",
        "faq": "咨询",
        "agent": "复杂任务",
        "human": "转人工",
        "clarify": "需澄清",
    }
    return names.get(route.route, route.route)


def _reduce_status(statuses) -> str:
    """Merge multiple scenario statuses into one.

    A blocked/permission_denied anywhere outranks success: if any part of the
    request could not be fulfilled, the user must not be told "全部完成".
    """
    for bad in ("permission_denied", "quota_exceeded", "blocked"):
        if bad in statuses:
            return bad
    if all(s == "success" for s in statuses):
        return "success"
    return "partial"
