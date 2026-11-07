"""CLI for Mini Agent v5: the five orchestration patterns.

    python3 -m examples.mini_agent_v5.cli --compare
    python3 -m examples.mini_agent_v5.cli --pattern routing --goal "我要退款"
    python3 -m examples.mini_agent_v5.cli --terminals   #五种终止状态演示
    python3 -m examples.mini_agent_v5.cli --compare --json

The comparison is deliberately *unlike* v4's: patterns differ in the **shape**
of control flow, so the table shows stages and terminal state, not "who talks
to whom".
"""
from __future__ import annotations

import argparse
import json
import sys

from . import PATTERNS
from .contract import Stage
from .chaining import PromptChaining
from .routing import Routing, Branch
from .parallel import Parallelization

DEFAULT_GOAL = "处理一笔退款申请：确认政策、核对订单、给出结论"


def run_compare(goal: str, as_json: bool) -> int:
    rows = []
    for name, cls in PATTERNS.items():
        result = cls().run(goal)
        rows.append(result)
        if as_json:
            continue
        print("=" * 78)
        print("模式: %-13s  ok=%-5s  终止=%-11s  阶段=%d  消耗=%d tokens  %dms"
              % (name, result.ok, result.stop_reason.value, result.stages,
                 result.tokens_used, result.elapsed_ms))
        print("-" * 78)
        print(result.answer)
        if result.questions:
            print("需要补充:", "；".join(result.questions))
        print("流:", " → ".join(t.get("step") or t.get("stage") or "?"
                                for t in result.trace if "step" in t or "stage" in t))

    if as_json:
        print(json.dumps([r.to_dict() for r in rows], ensure_ascii=False, indent=2))
        return 0

    print("=" * 78)
    print("对比（同一任务，同一个结果结构，差别只在控制流形状）：")
    print("%-13s %-6s %-12s %-6s %-8s" % ("模式", "ok", "终止", "阶段", "tokens"))
    for r in rows:
        print("%-13s %-6s %-12s %-6d %-8d"
              % (r.pattern, r.ok, r.stop_reason.value, r.stages, r.tokens_used))
    print()
    print("怎么选：")
    print("  chaining      步骤可枚举、每步职责单一（写作/抽取/翻译）。")
    print("  routing       请求类型差异大，选错成本高 → 一定要有兜底。")
    print("  parallel      子任务真正独立（分片）或有多种解法（投票）。")
    print("  orchestrator  无法提前枚举步骤，但能动态拆解（研究/Coding）。")
    print("  evaluator     有明确评价标准、需要迭代到通过（代码/格式/事实）。")
    return 0


def run_single(name: str, goal: str) -> int:
    cls = PATTERNS.get(name)
    if cls is None:
        print("未知模式：%s（可选：%s）" % (name, ", ".join(PATTERNS)), file=sys.stderr)
        return 2
    result = cls().run(goal)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


def run_terminals() -> int:
    """Show the five terminal states from the orchestration chapter, each
    produced by a real pattern in a real condition."""
    print("编排层必须回答“什么时候停”。五种终止状态各演示一次：\n")

    print("1. SUCCESS —— 检查通过")
    r = PromptChaining().run("写一段说明")
    print("   %-11s stages=%d  %s\n" % (r.stop_reason.value, r.stages, r.answer[:40]))

    print("2. PARTIAL —— 预算耗尽/检查未过，但已有产出")
    r = PromptChaining(stages=[
        Stage("gen", "初稿：{input}"),
        Stage("chk", "检查", role="check", passes_when=lambda s: False),
    ]).run("写一段说明")
    print("   %-11s 停在检查阶段，返回已有初稿：%s\n" % (r.stop_reason.value, r.answer[:30]))

    print("3. BLOCKED —— 缺信息，需要用户补充")
    r = Routing(
        branches=[Branch("order", Stage("order", "查单：{input}"),
                         lambda t: 0.9, requires=["订单号"])],
        fallback="order",
    ).run("我要查订单")
    print("   %-11s questions=%s\n" % (r.stop_reason.value, r.questions))

    print("4. FAILED —— 不可恢复错误")
    class Boom(Stage):
        def __call__(self, text, ctx):
            raise RuntimeError("upstream down")
    r = Parallelization(shards=[Boom("a", "x"), Boom("b", "y")]).run("任务")
    print("   %-11s answer=%s\n" % (r.stop_reason.value, r.answer))

    print("5. NEEDS_HUMAN —— 高风险/低置信，转人工")
    r = Routing(branches=[
        Branch("refund", Stage("refund", "退款：{input}"), lambda t: 0.2),
    ], fallback=None, confidence_floor=0.5).run("含糊的请求")
    print("   %-11s %s" % (r.stop_reason.value, r.answer))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Mini Agent v5 — 编排模式")
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--pattern", choices=list(PATTERNS))
    parser.add_argument("--goal", default=DEFAULT_GOAL)
    parser.add_argument("--terminals", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.terminals:
        return run_terminals()
    if args.pattern:
        return run_single(args.pattern, args.goal)
    return run_compare(args.goal, args.json)


if __name__ == "__main__":
    sys.exit(main())
