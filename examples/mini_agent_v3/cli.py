"""CLI for Mini Agent v3.

    python3 -m examples.mini_agent_v3.cli --demo
    python3 -m examples.mini_agent_v3.cli --demo --json
    python3 -m examples.mini_agent_v3.cli --eval
    python3 -m examples.mini_agent_v3.cli --resume-demo

``--resume-demo`` writes a run to a temp directory, simulates a crash in the
middle, and resumes it from the checkpoint — the only honest way to show that
"durable" is not just "we wrote a file".
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from ..mini_agent_v2.corpus import InMemoryCorpus
from ..mini_agent_v2.fixtures import seed_documents
from ..mini_agent_v2.memory_hygiene import MemoryStore
from ..mini_agent_v2.retrieval import HybridRetriever
from ..mini_agent_v2.router import default_intents, IntentRouter
from .audit import AuditLog
from .billing import Ledger, Quota
from .capability import CapabilityIssuer
from .gated import GatedPipeline, GatedRequest
from .persistence import LeaseLost, FileStore, RunManager


def build(workdir: str | Path | None = None):
    retriever = HybridRetriever(InMemoryCorpus(seed_documents()))
    retriever.build()
    workdir = Path(workdir or tempfile.mkdtemp(prefix="mini_agent_v3_"))
    audit = AuditLog(str(workdir / "audit.jsonl"))
    ledger = Ledger(str(workdir / "ledger.json"))
    pipeline = GatedPipeline(
        router=IntentRouter(default_intents()),
        retriever=retriever,
        memory=MemoryStore(),
        issuer=CapabilityIssuer(),
        audit=audit,
        ledger=ledger,
        quota=Quota(ledger, monthly_limits={"acme": 5.0}, default_limit=1.0),
    )
    return pipeline, audit, ledger, workdir


DEMO = [
    ("订单 A1024 到哪了", "user:88", {"public"}),
    ("解释一下退款时效条款", "user:88", {"public"}),
    ("退款多久到账", "user:88", {"public"}),
    ("我要投诉你们", "user:88", {"public"}),
    ("订单 B2048 到哪了", "agent:support", {"public"}),  # 非法主体，必须被拒
]


def run_demo(as_json: bool = False) -> int:
    pipeline, audit, ledger, workdir = build()
    print("=" * 74)
    print("Mini Agent v3 演示：能力令牌 → 工具网关 → 计量 → 审计")
    print("=" * 74)

    rows = []
    for text, user_id, principals in DEMO:
        result = pipeline.handle(
            GatedRequest(text=text, user_id=user_id, principals=principals)
        )
        rows.append(result)
        if as_json:
            continue
        print("-" * 74)
        print("请求: %s   主体: %s" % (text, user_id))
        print("路由: %-9s 状态: %-17s 成本: $%.6f" % (result["route"], result["status"], result.get("cost_usd", 0)))
        print("输出: %s" % result["output"])

    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print()
    print("=" * 74)
    print("三个问题（来自学习路线步骤 6）：")
    for question, answer in ledger.answer_three_questions().items():
        print("  %s → %s" % (question, answer))
    print()
    print("审计：共 %d 条事件，其中拒绝 %d 条" % (len(audit.events()), len(audit.denials())))
    for event in audit.denials():
        print("  拒绝 %s @ %s：%s" % (event.action, event.subject, event.reason))
    print()
    print("关键点：")
    print("  1. 工具调用必须携带绑定真实用户的短期能力令牌；agent 自己不能是主体。")
    print("  2. 拒绝会留下审计记录——否则“谁试图做什么”不可见。")
    print("  3. 计量带幂等键，重试不会重复计费；账本只追加，冲正用负记录。")
    print("  4. 工作目录: %s" % workdir)
    return 0


def run_eval() -> int:
    """End-to-end eval with the platform invariants as assertions."""
    pipeline, audit, ledger, _ = build()
    failures = []

    # 1. normal paths
    ok = pipeline.handle(GatedRequest(text="订单 A1024 到哪了", user_id="user:88"))
    if ok["status"] != "success":
        failures.append("order lookup should succeed")

    # 2. capability denied for a non-user subject
    denied = pipeline.handle(GatedRequest(text="订单 B2048 到哪了", user_id="agent:support"))
    if denied["status"] != "permission_denied":
        failures.append("agent subject must be denied, got %s" % denied["status"])
    if not audit.denials():
        failures.append("denial must be audited")

    # 3. idempotent retry does not double bill
    request_id = "req_retry_probe"
    before = len(ledger.events())
    pipeline.handle(GatedRequest(text="解释一下退款时效条款", user_id="user:88", request_id=request_id))
    after_first = ledger.total_for_request(request_id)
    pipeline.handle(GatedRequest(text="解释一下退款时效条款", user_id="user:88", request_id=request_id))
    after_second = ledger.total_for_request(request_id)
    if abs(after_first - after_second) > 1e-12:
        failures.append("retry changed billed amount: %.8f -> %.8f" % (after_first, after_second))

    # 4. audit log must not contain PII
    leaks = audit.find_leaks()
    if leaks:
        failures.append("audit log leaked PII in %s" % leaks)

    # 5. quota blocks further spending *before* the work is done
    pipeline_q, _, ledger_q, _ = build()
    from .billing import UsageEvent

    ledger_q.record(
        UsageEvent(tenant_id="acme", kind="llm", model="gpt-4.1", cost_usd=99.0,
                   idempotency_key="probe:quota", request_id="probe")
    )
    exhausted = pipeline_q.handle(GatedRequest(text="订单 A1024 到哪了", user_id="user:88"))
    if exhausted["status"] != "quota_exceeded":
        failures.append("quota should block, got %s" % exhausted["status"])
    if ledger_q.total_for_request(exhausted["request_id"]) != 0:
        failures.append("a quota-blocked request must not be billed")

    metrics = {
        "requests": len({e.request_id for e in audit.events()}),
        "events": len(audit.events()),
        "denials": len(audit.denials()),
        "billed_total": round(sum(e.cost_usd for e in ledger.events()), 6),
        "duplicates_suppressed": ledger.duplicates,
        "audit_pii_leaks": len(leaks),
    }
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    if failures:
        print("\nFAILED:")
        for item in failures:
            print("  -", item)
        return 1
    print("\npassed: True")
    return 0


def run_resume_demo() -> int:
    workdir = tempfile.mkdtemp(prefix="mini_agent_v3_resume_")
    store = FileStore(workdir)
    worker_a = RunManager(store, lease_ttl_s=0.2, worker_id="worker-A")
    state = worker_a.create("汇总本月的退款申请")
    state = worker_a.acquire(state.run_id)
    state = worker_a.checkpoint(state, "collect", "收集到 12 条")
    state = worker_a.checkpoint(state, "normalize", "归一化完成")
    print("worker-A 完成 2 步，模拟崩溃（不再续约）…")

    worker_b = RunManager(store, lease_ttl_s=5.0, worker_id="worker-B")
    plan = worker_b.resume_plan(state.run_id)
    print("worker-B 看到的计划：", json.dumps(plan, ensure_ascii=False))

    # 旧 worker 的租约过期后，新 worker 才能接管
    import time

    time.sleep(0.25)
    taken = worker_b.acquire(state.run_id)
    taken = worker_b.checkpoint(taken, "summarize", "汇总完成：共 12 条")
    taken = worker_b.finish(taken, "done")

    # 围栏令牌：旧 worker 不能覆盖新 worker 的结果
    try:
        worker_a.checkpoint(state, "late-write", "不应该发生")
        print("BUG: 旧 worker 写入成功")
        return 1
    except LeaseLost as exc:
        print("旧 worker 写入被拒绝：", exc)

    final = store.load(state.run_id)
    print("最终状态：", final.status, "| 步骤：", final.completed_steps)
    print("passed:", final.status == "done" and len(final.completed_steps) == 3)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Mini Agent v3")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--eval", action="store_true")
    parser.add_argument("--resume-demo", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.eval:
        return run_eval()
    if args.resume_demo:
        return run_resume_demo()
    return run_demo(as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
