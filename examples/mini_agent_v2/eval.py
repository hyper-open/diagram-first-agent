"""End-to-end eval for v2: routing accuracy, retrieval recall, ACL leaks, cost.

    python3 -m examples.mini_agent_v2.eval

Reports four things that fail independently:

* ``route_acc``  — did the request reach the right handler?
* ``recall@5``   — did retrieval find the evidence (only for RAG/FAQ cases)?
* ``acl_leaks``  — did a restricted chunk reach a caller who may not see it?
* ``handoff``    — how often we escalated (a cost and experience signal)

A single "overall accuracy" number would hide all of this.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Set

from .corpus import InMemoryCorpus
from .fixtures import seed_cases, seed_documents
from .memory_hygiene import MemoryStore
from .pipeline import PipelineRequest, RequestPipeline
from .retrieval import HybridRetriever, evaluate_retrieval
from .router import FAQ, HUMAN, RAG, IntentRouter, default_intents


@dataclass
class RouteCase:
    text: str
    expected: str
    principals: Set[str] = field(default_factory=lambda: {"public"})


ROUTE_CASES: List[RouteCase] = [
    RouteCase("退款多久到账", FAQ),
    RouteCase("多少钱包邮", FAQ),
    RouteCase("订单 A1024 到哪了", "workflow"),
    RouteCase("我想查一下订单", "workflow"),
    RouteCase("解释一下退款时效条款", RAG),
    RouteCase("合同怎么约定争议解决", RAG),
    RouteCase("我要投诉你们，退款一直不到账", HUMAN),
    RouteCase("VIP 会员退款上限是多少", HUMAN),  # public cannot see it -> escalate
]


def run() -> int:
    corpus = InMemoryCorpus(seed_documents())
    retriever = HybridRetriever(corpus)
    retriever.build()
    router = IntentRouter(default_intents())
    pipeline = RequestPipeline(
        router=router, retriever=retriever, memory=MemoryStore(tenant_id="acme")
    )

    results = [
        pipeline.handle(PipelineRequest(text=c.text, principals=c.principals))
        for c in ROUTE_CASES
    ]
    correct = sum(
        1
        for case, result in zip(ROUTE_CASES, results)
        if result.route.route == case.expected
        # ``clarify`` is an acceptable outcome for a valid route that lacks a
        # required parameter (e.g. "查订单" with no order id).
        or (case.expected == "workflow" and result.status == "clarify")
    )
    handoffs = sum(1 for r in results if r.status == "handoff")
    retrieval = evaluate_retrieval(retriever, seed_cases())

    print("route_acc   %.2f (%d/%d)" % (correct / len(ROUTE_CASES), correct, len(ROUTE_CASES)))
    print("recall@5    %.3f" % retrieval.recall_at_5)
    print("recall@20   %.3f" % retrieval.recall_at_20)
    print("acl_leaks   %d" % retrieval.forbidden_hits)
    print("handoff     %d/%d" % (handoffs, len(results)))
    print("avg_latency_ms %d" % (sum(r.latency_ms for r in results) / len(results)))
    print("total_cost_usd %.6f" % sum(r.cost_usd for r in results))
    print()

    for case, result in zip(ROUTE_CASES, results):
        flag = "ok " if result.route.route == case.expected else "MISS"
        print(
            "%-4s %-28s expected=%-8s got=%-9s %.2f  status=%s"
            % (
                flag,
                case.text[:28],
                case.expected,
                result.route.route,
                result.route.confidence,
                result.status,
            )
        )

    ok = correct == len(ROUTE_CASES) and retrieval.forbidden_hits == 0
    print("\npassed:", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
