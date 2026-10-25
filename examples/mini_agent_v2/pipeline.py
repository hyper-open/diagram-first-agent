"""The v2 request pipeline: route, then hand off to exactly one handler.

```
request ──> IntentRouter ──┬── faq       (corpus, no model call)
                           ├── workflow  (fixed steps, tool calls only)
                           ├── rag       (retrieval + evidence, citations required)
                           ├── agent     (v1 MiniAgent loop + RAG tool)
                           ├── clarify   (ask one question, then stop)
                           └── human     (handoff packet, never a model answer)
```

Two rules the pipeline enforces structurally rather than by prompt:

1. **Citations are verified, not requested.**  A RAG answer whose ``[c1]`` style
   references do not resolve to an actually retrieved chunk is rejected.
2. **Every path writes a trace record** with the same shape as
   ``examples/templates/trace_step.json`` so one dashboard covers all routes.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..mini_agent.guard import Guard
from ..mini_agent.tools import Tool, ToolRegistry
from ..mini_agent.types import Budget, Decision, Trace
from .memory_hygiene import MemoryStore
from .retrieval import HybridRetriever, SearchHit
from .router import AGENT, CLARIFY, FAQ, HUMAN, RAG, WORKFLOW, IntentRouter, Route

_CITATION = re.compile(r"\[(c\d+)\]")
_ORDER_ID = re.compile(r"\b([A-Z]\d{3,})\b")


@dataclass
class PipelineRequest:
    text: str
    principals: set = field(default_factory=lambda: {"public"})
    tenant_id: str = "acme"
    user_id: str = "user_88"
    request_id: str = field(default_factory=lambda: "req_" + uuid.uuid4().hex[:8])


@dataclass
class Citation:
    ref: str
    chunk_id: str
    doc_id: str
    section: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "ref": self.ref,
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "section": self.section,
        }


@dataclass
class PipelineResult:
    request_id: str
    route: Route
    status: str  # success | clarify | handoff | blocked | ungrounded
    output: str
    citations: List[Citation] = field(default_factory=list)
    steps: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    trace: List[Dict[str, Any]] = field(default_factory=list)
    handoff: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "route": self.route.to_dict(),
            "status": self.status,
            "output": self.output,
            "citations": [c.to_dict() for c in self.citations],
            "steps": self.steps,
            "latency_ms": self.latency_ms,
            "cost_usd": round(self.cost_usd, 6),
            "trace": self.trace,
            "handoff": self.handoff,
        }


class RequestPipeline:
    """Owns routing and the handler table.  Handlers are plain callables."""

    def __init__(
        self,
        *,
        router: IntentRouter,
        retriever: HybridRetriever,
        memory: MemoryStore,
        llm_factory: Optional[Callable[[], Any]] = None,
        guard: Optional[Guard] = None,
        cost_per_1k_input: float = 0.0004,
        cost_per_1k_output: float = 0.0016,
    ):
        self.router = router
        self.retriever = retriever
        self.memory = memory
        self.llm_factory = llm_factory
        self.guard = guard
        self.cost_in = cost_per_1k_input
        self.cost_out = cost_per_1k_output

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def handle(self, request: PipelineRequest) -> PipelineResult:
        started = time.monotonic()
        # Input gate before spending anything: routing is cheap, a model call is not.
        if self.guard is not None:
            gate = self.guard.check(request.text, stage="input")
            if gate.blocked:
                return PipelineResult(
                    request_id=request.request_id,
                    route=Route("blocked", 1.0, reason="input_guard"),
                    status="blocked",
                    output=getattr(gate, "message", "") or "该请求被内容策略拦截。",
                    latency_ms=int((time.monotonic() - started) * 1000),
                )

        route = self.router.route(request.text)
        handlers: Dict[str, Callable[[PipelineRequest, Route], PipelineResult]] = {
            FAQ: self._handle_faq,
            WORKFLOW: self._handle_workflow,
            RAG: self._handle_rag,
            AGENT: self._handle_agent,
            CLARIFY: self._handle_clarify,
            HUMAN: self._handle_human,
        }
        handler = handlers.get(route.route, self._handle_human)
        result = handler(request, route)
        result.request_id = request.request_id
        result.route = route
        result.latency_ms = int((time.monotonic() - started) * 1000)
        result.trace.insert(
            0,
            {
                "kind": "route",
                "route": route.route,
                "confidence": round(route.confidence, 3),
                "reason": route.reason,
            },
        )
        return result

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_faq(self, request: PipelineRequest, route: Route) -> PipelineResult:
        """No model call: if the corpus covers it, answer from the corpus."""
        hits = self.retriever.search(request.text, request.principals, k=2)
        if not hits:
            # A "no answer" is a route change, not an empty reply.
            return self._handle_clarify(request, route)
        top = hits[0]
        citations = [self._citation("c1", top)]
        return PipelineResult(
            request_id=request.request_id,
            route=route,
            status="success",
            output="%s（依据 %s / %s）" % (
                _first_sentence(top.chunk.text),
                top.chunk.doc_id,
                top.chunk.section or "正文",
            ),
            citations=citations,
            steps=0,
        )

    def _handle_workflow(self, request: PipelineRequest, route: Route) -> PipelineResult:
        """Fixed steps, no model: the control flow is code."""
        order_ids = _ORDER_ID.findall(request.text)
        if not order_ids:
            return self._handle_clarify(request, route)
        order_id = order_ids[0]
        order = MOCK_ORDERS.get(order_id)
        if order is None:
            return PipelineResult(
                request_id=request.request_id,
                route=route,
                status="success",
                output="没有找到订单 %s。请确认订单号，或让我转人工帮你查。" % order_id,
            )
        return PipelineResult(
            request_id=request.request_id,
            route=route,
            status="success",
            output="订单 %s：%s，预计 %s 送达（当前用户：%s）。"
            % (order_id, order["status"], order["eta"], request.user_id),
            steps=1,
            trace=[{"kind": "workflow", "step": "order.lookup", "order_id": order_id}],
        )

    def _handle_rag(self, request: PipelineRequest, route: Route) -> PipelineResult:
        """Retrieve, then answer only from evidence, then verify the citations."""
        hits = self.retriever.search(request.text, request.principals, k=4)
        if not hits:
            return PipelineResult(
                request_id=request.request_id,
                route=route,
                status="success",
                output="知识库里没有找到相关条款。我可以帮你转人工核实。",
            )

        citations = [self._citation("c%d" % (i + 1), hit) for i, hit in enumerate(hits)]
        evidence = "\n".join(
            "%s %s" % (ref.ref, hit.chunk.text) for ref, hit in zip(citations, hits)
        )

        decision = self._ask(
            "只根据下面的证据回答，并在每句结论后用 [cN] 标注来源。"
            "没有证据支持的内容不要写。\n\n证据：\n" + evidence
            + "\n\n问题：" + request.text
        )
        answer = decision.content.strip()
        used = self._verify_citations(answer, {c.ref for c in citations})
        if not used:
            return PipelineResult(
                request_id=request.request_id,
                route=route,
                status="ungrounded",
                output="找到证据但未能给出可核对的引用，已降级为人工处理。",
                citations=citations,
                steps=1,
                latency_ms=0,
                cost_usd=self._cost(decision),
                handoff={"reason": "ungrounded_answer", "evidence": [c.to_dict() for c in citations]},
                trace=[{"kind": "rag", "citations_offered": len(citations), "citations_used": 0}],
            )
        return PipelineResult(
            request_id=request.request_id,
            route=route,
            status="success",
            output=answer,
            citations=[c for c in citations if c.ref in used],
            steps=1,
            cost_usd=self._cost(decision),
            trace=[
                {
                    "kind": "rag",
                    "citations_offered": len(citations),
                    "citations_used": len(used),
                    "retrieved": [h.chunk.chunk_id for h in hits],
                }
            ],
        )

    def _handle_agent(self, request: PipelineRequest, route: Route) -> PipelineResult:
        """The v1 loop, with RAG exposed to it as a tool."""
        if self.llm_factory is None:
            return PipelineResult(
                request_id=request.request_id,
                route=route,
                status="success",
                output="（未配置模型；Agent 路线需要 llm_factory）",
            )
        from ..mini_agent.agent import MiniAgent  # local import: keep import cost off other routes

        agent = MiniAgent(
            llm=self.llm_factory(),
            tools=self._agent_tools(request),
            budget=Budget(max_steps=6, max_tool_calls=8, max_tokens=30_000),
            memory=None,
        )
        result = agent.run(request.text)
        return PipelineResult(
            request_id=request.request_id,
            route=route,
            status="success" if result.status == "success" else result.status,
            output=result.output,
            steps=result.steps,
            cost_usd=self._cost_usage(result.trace),
            trace=[{"kind": "agent", "status": result.status, "steps": result.steps}],
        )

    def _handle_clarify(self, request: PipelineRequest, route: Route) -> PipelineResult:
        question = (
            "请问你具体是哪种情况？（例如：查订单 / 退款政策 / 解释文档条款）"
        )
        return PipelineResult(
            request_id=request.request_id,
            route=Route(CLARIFY, route.confidence, route.intent, "低置信，先澄清"),
            status="clarify",
            output=question,
            trace=[{"kind": "clarify", "asked": question}],
        )

    def _handle_human(self, request: PipelineRequest, route: Route) -> PipelineResult:
        """A handoff is a first-class outcome, not a failure."""
        return PipelineResult(
            request_id=request.request_id,
            route=route,
            status="handoff",
            output="这个问题需要人工同事处理，我已为你转接，并把当前信息整理好了。",
            handoff={
                "reason": route.reason or "low_confidence",
                "request": request.text,
                "route_scores": route.to_dict(),
                "user_id": request.user_id,
                "memory": [r.explain() for r in self.memory.retrieve(request.text, k=2)],
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ask(self, prompt: str) -> Decision:
        if self.llm_factory is None:
            # Deterministic stand-in so the pipeline is testable without a model.
            return Decision(content=_scripted_answer(prompt), final=True, usage={"input_tokens": len(prompt) // 4, "output_tokens": 40})
        llm = self.llm_factory()
        return llm.decide([{"role": "user", "content": prompt}], [])

    def _agent_tools(self, request: PipelineRequest) -> ToolRegistry:
        def search_kb(args: Dict[str, Any]) -> Dict[str, Any]:
            hits = self.retriever.search(
                str(args.get("query", "")), request.principals, k=int(args.get("k", 3))
            )
            if not hits:
                return {"hits": [], "note": "知识库无结果"}
            return {
                "hits": [
                    {"ref": "c%d" % (i + 1), **hit.to_dict()} for i, hit in enumerate(hits)
                ]
            }

        tools = [
            Tool(
                name="kb.search",
                description="在当前用户有权访问的知识库中检索文档片段",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "k": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                handler=search_kb,
            )
        ]
        return ToolRegistry(tools)

    @staticmethod
    def _verify_citations(answer: str, allowed: set) -> set:
        return {ref for ref in _CITATION.findall(answer) if ref in allowed}

    @staticmethod
    def _citation(ref: str, hit: SearchHit) -> Citation:
        return Citation(
            ref=ref,
            chunk_id=hit.chunk.chunk_id,
            doc_id=hit.chunk.doc_id,
            section=hit.chunk.section,
        )

    def _cost(self, decision: Decision) -> float:
        usage = decision.usage or {}
        return (
            int(usage.get("input_tokens", 0)) / 1000 * self.cost_in
            + int(usage.get("output_tokens", 0)) / 1000 * self.cost_out
        )

    def _cost_usage(self, trace: Trace) -> float:
        total = 0.0
        for step in trace.steps:
            total += self._cost(step.decision)
        return total


# ---------------------------------------------------------------------------
# Fixtures used by the CLI and the tests
# ---------------------------------------------------------------------------


def _first_sentence(text: str) -> str:
    body = text.split("\n", 1)[-1] if "\n" in text else text
    for sep in ("。", "；", "\n"):
        if sep in body:
            return body.split(sep)[0] + (sep if sep != "\n" else "")
    return body


def _scripted_answer(prompt: str) -> str:
    """Tiny deterministic 'model' that only quotes evidence with a citation.

    It exists so the RAG path can be exercised (including the citation check)
    without an API key.  It is intentionally dumb: the point is that the
    *pipeline* refuses ungrounded output, not that this mimics a real model.
    """
    evidence = prompt.split("证据：", 1)[-1].split("问题：", 1)[0]
    # Evidence lines look like ``c1 <section>\n<body>`` — the chunk keeps its
    # newline, so parse per reference instead of per line.
    matches = list(re.finditer(r"(?m)^\s*(c\d+)\s+", evidence))
    if not matches:
        return "无法从证据中得出结论。"
    first = matches[0]
    end = matches[1].start() if len(matches) > 1 else len(evidence)
    block = evidence[first.end() : end].strip()
    head, _, body = block.partition("\n")
    body = body.strip() or head
    snippet = body[:120].strip()
    label = "%s：%s" % (head.strip(), snippet) if body != head else snippet
    return "%s [%s]" % (label, first.group(1))


MOCK_ORDERS = {
    "A1024": {"status": "已发货，运输中", "eta": "明天"},
    "B2048": {"status": "已签收", "eta": "已于昨天"},
}
