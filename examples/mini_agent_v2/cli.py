"""CLI for Mini Agent v2.

    python3 -m examples.mini_agent_v2.cli --demo
    python3 -m examples.mini_agent_v2.cli --ask "超过七天还能退吗"
    python3 -m examples.mini_agent_v2.cli --eval
    python3 -m examples.mini_agent_v2.cli --demo --json

No API key is required: the RAG handler uses a deterministic evidence-quoting
stand-in so the citation check can be exercised offline.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import List

from .corpus import InMemoryCorpus
from .fixtures import seed_cases, seed_documents
from .memory_hygiene import MemoryStore
from .pipeline import PipelineRequest, RequestPipeline
from .retrieval import HybridRetriever, evaluate_retrieval
from .router import default_intents, IntentRouter


def build_pipeline(tenant_id: str = "acme", user_id: str = "user_88", principals=None):
    corpus = InMemoryCorpus(seed_documents())
    retriever = HybridRetriever(corpus)
    router = IntentRouter(default_intents())
    pipeline = RequestPipeline(
        router=router,
        retriever=retriever,
        memory=MemoryStore(tenant_id=tenant_id),
        llm_factory=None,  # deterministic stand-in; pass a factory to use a real model
    )
    return pipeline, retriever


DEMO_REQUESTS = [
    ("超过七天还能退吗", {"public"}),
    ("订单 A1024 到哪了", {"public"}),
    ("解释一下退款时效条款", {"public"}),
    ("VIP 会员退款上限是多少", {"public"}),  # ACL trap: must not see policy-vip
    ("我要投诉你们，退款一直不到账", {"public"}),
    ("嗯嗯那个东西", {"public"}),  # low confidence -> clarify
]


def run_demo(as_json: bool = False) -> int:
    pipeline, retriever = build_pipeline()
    print("=" * 72)
    print("Mini Agent v2 演示：路由 → 处理 → 引用校验")
    print("=" * 72)
    print("知识库切分后共 %d 个片段\n" % len(retriever.chunks))

    rows = []
    for text, principals in DEMO_REQUESTS:
        result = pipeline.handle(
            PipelineRequest(text=text, principals=principals)
        )
        rows.append(result.to_dict())
        if as_json:
            continue
        print("-" * 72)
        print("请求: %s" % text)
        print(
            "路由: %-9s 置信度=%.2f  状态=%s  (%s)"
            % (result.route.route, result.route.confidence, result.status, result.route.reason)
        )
        print("输出: %s" % result.output)
        if result.citations:
            print(
                "引用: "
                + ", ".join("%s→%s" % (c.ref, c.chunk_id) for c in result.citations)
            )
        if result.handoff:
            print("转人工: %s" % result.handoff.get("reason"))

    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print()
    print("=" * 72)
    print("关键点：")
    print("  1. 简单请求不进入 Agent 循环（FAQ/查单走 0-1 步）。")
    print("  2. 权限过滤发生在召回阶段：VIP 问题在 public 身份下看不到 policy-vip。")
    print("  3. 引用会被校验：无法对应到检索结果的引用一律降级转人工。")
    print("  4. 低置信不是失败，而是澄清或转人工的明确出口。")
    return 0


def run_eval() -> int:
    corpus = InMemoryCorpus(seed_documents())
    retriever = HybridRetriever(corpus)
    retriever.build()
    report = evaluate_retrieval(retriever, seed_cases())
    print("retrieval eval: samples=%d recall@5=%.3f recall@20=%.3f forbidden_hits=%d"
          % (report.samples, report.recall_at_5, report.recall_at_20, report.forbidden_hits))
    print("passed:", report.passed)
    return 0 if report.passed else 1


def run_ask(text: str, principals: List[str]) -> int:
    pipeline, _ = build_pipeline()
    result = pipeline.handle(
        PipelineRequest(text=text, principals=set(principals))
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Mini Agent v2")
    parser.add_argument("--demo", action="store_true", help="run the guided demo")
    parser.add_argument("--ask", metavar="TEXT", help="handle one request")
    parser.add_argument("--eval", action="store_true", help="run the retrieval eval")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--principal",
        action="append",
        default=None,
        help="principal for --ask (default: public)",
    )
    args = parser.parse_args(argv)

    if args.eval:
        return run_eval()
    if args.ask:
        return run_ask(args.ask, args.principal or ["public"])
    return run_demo(as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
