"""CLI for Mini Agent v4: run every multi-agent topology on the same task.

    python3 -m examples.mini_agent_v4.cli --compare
    python3 -m examples.mini_agent_v4.cli --topology supervisor
    python3 -m examples.mini_agent_v4.cli --failures     # error propagation demo
    python3 -m examples.mini_agent_v4.cli --budget       # budget invariant demo
    python3 -m examples.mini_agent_v4.cli --compare --json

The pedagogical point of ``--compare`` is that all four answer the *same* task
and return the *same* result shape, so the differences you see are purely about
control flow: how many sub-agents talk, in what order, and where merging happens.
"""
from __future__ import annotations

import argparse
import json
import sys

from .contract import AgentRole, BudgetTree
from .supervisor import SupervisorTopology
from . import TOPOLOGIES

DEFAULT_GOAL = "评估一笔超期订单是否应批准退款，并给出理由"


def run_compare(goal: str, as_json: bool) -> int:
    rows = []
    for name, cls in TOPOLOGIES.items():
        result = cls().run(goal)
        rows.append(result)
        if as_json:
            continue
        print("=" * 78)
        print("拓扑: %-13s  ok=%-5s  置信度=%.2f  子任务=%d  消耗=%d tokens  %dms"
              % (name, result.ok, result.confidence, result.subtasks,
                 result.tokens_used, result.elapsed_ms))
        print("-" * 78)
        print(result.answer)
        if result.failures:
            print("失败: %s" % "; ".join(result.failures))
        print("步骤:", " → ".join(t.get("step", "?") for t in result.trace))

    if as_json:
        print(json.dumps([r.to_dict() for r in rows], ensure_ascii=False, indent=2))
        return 0

    print("=" * 78)
    print("对比（同一个任务，同一种结果结构，差别只在控制流）：")
    print("%-13s %-6s %-8s %-8s %-8s" % ("拓扑", "ok", "置信度", "子任务", "tokens"))
    for r in rows:
        print("%-13s %-6s %-8.2f %-8d %-8d"
              % (r.topology, r.ok, r.confidence, r.subtasks, r.tokens_used))
    print()
    print("怎么选：")
    print("  supervisor   责任清晰、默认选择；子任务独立时优先。")
    print("  peer         灵活性换稳定性；务必有轮数上限和实质终止条件。")
    print("  hierarchical 单上下文装不下计划时才用；代价是层级延迟叠加。")
    print("  debate       只用于判断类、错一次代价高的任务；成本至少 3 次调用。")
    return 0


def run_single(name: str, goal: str) -> int:
    cls = TOPOLOGIES.get(name)
    if cls is None:
        print("未知拓扑：%s（可选：%s）" % (name, ", ".join(TOPOLOGIES)), file=sys.stderr)
        return 2
    result = cls().run(goal)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


def run_failures() -> int:
    """Show that a failed worker is *data*, not a crash."""
    print("场景：3 个 worker，中间那个坏掉")
    roles = [
        AgentRole("researcher", "检索 {goal} 的依据", confidence=0.85),
        AgentRole("analyzer", "分析 {goal}", confidence=0.8, fail_above=0, fail_reason="tool_timeout"),
        AgentRole("writer", "整理 {goal} 的结论", confidence=0.7),
    ]
    result = SupervisorTopology(workers=roles).run("整理退款政策")
    print("supervisor ok=%s 置信度=%.2f（取可用结果的最小值，不是平均）"
          % (result.ok, result.confidence))
    print("失败被记录而非抛出:", result.failures)
    print("仍然产出了答案:")
    print(result.answer)
    print()
    print("反例：全部 worker 都坏掉")
    bad = [AgentRole("a", "x", fail_above=0, fail_reason="upstream_503"),
           AgentRole("b", "y", fail_above=0, fail_reason="upstream_503")]
    r = SupervisorTopology(workers=bad).run("任务")
    print("ok=%s  answer=%s" % (r.ok, r.answer))
    print("→ 没有可用输入时，必须明确说“无法完成”，不能编一个答案。")
    return 0


def run_budget() -> int:
    """Show that fan-out cannot spend more than the parent allows."""
    print("父预算 3000 tokens，请求 spawn 10 个 worker：")
    roles = [AgentRole("w%d" % i, "任务 {goal}", confidence=0.7) for i in range(10)]
    result = SupervisorTopology(workers=roles).run(
        "一个很大的任务", budget=BudgetTree(max_tokens=3000, max_steps=20)
    )
    plan = result.trace[0]
    print("  计划 spawn: %d，实际 spawn: %d（预算只够这么多）"
          % (len(plan["planned"]), plan["spawned"]))
    print("  实际消耗: %d tokens（上限 3000，未越界: %s）"
          % (result.tokens_used, result.tokens_used <= 3000))
    print()
    print("深层原因：split 会把池子切成 n 份，导致每份都不够一次调用；")
    print("本项目改用 reserve/settle：先按最小调用成本算能养几个 worker，")
    print("再从父池子里真正扣掉、用不完的退还。")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Mini Agent v4 — 多 Agent 拓扑")
    parser.add_argument("--compare", action="store_true", help="同一任务跑全部拓扑")
    parser.add_argument("--topology", choices=list(TOPOLOGIES), help="只跑一个拓扑")
    parser.add_argument("--goal", default=DEFAULT_GOAL, help="任务描述")
    parser.add_argument("--failures", action="store_true", help="错误传播演示")
    parser.add_argument("--budget", action="store_true", help="预算封顶演示")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)

    if args.failures:
        return run_failures()
    if args.budget:
        return run_budget()
    if args.topology:
        return run_single(args.topology, args.goal)
    return run_compare(args.goal, args.json)


if __name__ == "__main__":
    sys.exit(main())
