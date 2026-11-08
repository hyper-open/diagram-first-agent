"""CLI for the Plan-Execute harness.

    python3 -m examples.mini_agent_v7.cli --demo
    python3 -m examples.mini_agent_v7.cli --scenarios   # 覆盖终止/重试/预算/重规划
    python3 -m examples.mini_agent_v7.cli --demo --trace
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Dict, List

from ..mini_agent.types import Budget
from .plan_execute import (
    ExecResult,
    Plan,
    PlanExecuteHarness,
    Step,
    contains_verifier,
    dict_executor,
    list_planner,
)


def run_demo(show_trace: bool) -> int:
    goal = "处理退款请求 A1024：查政策、查订单、起草答复"
    plan = [Step("lookup_policy", "退款政策"), Step("lookup_order", "A1024"), Step("draft", "答复")]
    execs = {
        "lookup_policy": ExecResult(True, "退款政策：7 天内未拆封可退", tokens=300),
        "lookup_order": ExecResult(True, "订单 A1024：已发货，运输中", tokens=300),
        "draft": ExecResult(True, "答复：可退，需人工审批后执行", tokens=400),
    }
    h = PlanExecuteHarness(
        planner=list_planner(plan),
        executor=dict_executor(execs),
        verifier=contains_verifier(["7 天", "A1024", "可退"]),
        budget=Budget(max_steps=20, max_tool_calls=20, max_tokens=10000),
    )
    result = h.run(goal)

    print("=" * 74)
    print("Plan-Execute Harness（规划·执行·验证 都由 harness 管）")
    print("=" * 74)
    print("目标:", goal)
    print("计划:", [s.name for s in plan])
    print("-" * 74)
    for t in result.trace:
        if t["phase"] == "plan":
            print("  [plan]   round=%d  steps=%s" % (t["round"], t["steps"]))
        elif t["phase"] == "execute":
            mark = "✓" if t["ok"] else "✗"
            print("  [exec]   %s %-14s attempt=%d tokens=%d" %
                  (mark, t["step"], t["attempt"], t["tokens"]))
        elif t["phase"] == "early_stop":
            print("  [stop]   提前通过（%s 后已满足验证），后续步骤不再执行" % t["after"])
        elif t["phase"] == "verify":
            print("  [verify] passed=%s  %s" % (t["passed"], t["reason"]))
    print("-" * 74)
    print("状态: %s | 轮次: %d | 步数: %d | tokens: %d | %dms"
          % (result.status, result.rounds, len(result.steps_done), result.tokens, result.elapsed_ms))
    print("输出:")
    for line in result.output.splitlines():
        print("   ", line)
    if show_trace:
        print("\n完整 trace:")
        print(json.dumps(result.trace, ensure_ascii=False, indent=2))
    print()
    print("关键点：")
    print("  1. 循环骨架和 ReAct 一样，只是内层从'想一步做一步'变成'先规划一批再执行'。")
    print("  2. 预算/trace/终止/重试全在 harness；planner/executor/verifier 是可替换的注入。")
    print("  3. 验证一过就早停，不多跑剩下的步骤——省成本。")
    return 0


def run_scenarios() -> int:
    """Walk through every terminal path with a deterministic executor."""

    def show(name: str, result) -> None:
        print("  %-14s status=%-8s rounds=%d steps=%d tokens=%d"
              % (name, result.status, result.rounds, len(result.steps_done), result.tokens))
        if result.error:
            print("                 error=%s" % result.error)

    print("=" * 74)
    print("plan-execute harness 的四种终止路径")
    print("=" * 74)

    # 1. 正常
    r = PlanExecuteHarness(
        list_planner([Step("a"), Step("b")]),
        dict_executor({"a": ExecResult(True, "x", tokens=10),
                       "b": ExecResult(True, "y", tokens=10)}),
        contains_verifier(["y"]),
    ).run("g")
    show("成功", r)

    # 2. 可重试失败→重试成功
    calls = {"n": 0}

    def flaky(step):
        calls["n"] += 1
        if calls["n"] < 2:
            return ExecResult(False, "timeout", retryable=True, tokens=10)
        return ExecResult(True, "ok", tokens=10)

    r = PlanExecuteHarness(list_planner([Step("s")]), flaky,
                           contains_verifier(["ok"])).run("g")
    show("重试成功", r)

    # 3. 不可重试失败
    r = PlanExecuteHarness(list_planner([Step("s")]),
                           lambda s: ExecResult.failure("bad_arg"),
                           budget=Budget(max_steps=10, max_tool_calls=10)).run("g")
    show("不可重试", r)

    # 4. 预算耗尽
    many = [Step("s%d" % i) for i in range(20)]
    execs = {s.name: ExecResult(True, "ok", tokens=500) for s in many}
    r = PlanExecuteHarness(list_planner(many), dict_executor(execs),
                           contains_verifier(["never"]),
                           budget=Budget(max_steps=5, max_tool_calls=100,
                                         max_tokens=100000)).run("g")
    show("预算耗尽", r)

    print()
    print("注意 3/4 都是 PARTIAL/FAILED，不是 SUCCESS——harness 不粉饰失败。")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Plan-Execute Harness")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--scenarios", action="store_true")
    parser.add_argument("--trace", action="store_true")
    args = parser.parse_args(argv)
    if args.scenarios:
        return run_scenarios()
    return run_demo(show_trace=args.trace)


if __name__ == "__main__":
    sys.exit(main())
