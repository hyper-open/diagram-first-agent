"""CLI for Mini Agent v6: Arena and Swarm/Handoff.

    python3 -m examples.mini_agent_v6.cli --arena
    python3 -m examples.mini_agent_v6.cli --swarm
    python3 -m examples.mini_agent_v6.cli --cycles     # 非传递性演示
    python3 -m examples.mini_agent_v6.cli --handoff-loop
    python3 -m examples.mini_agent_v6.cli --arena --json
"""
from __future__ import annotations

import argparse
import json
import sys

from .arena import Arena, Candidate
from .swarm import Swarm

DEFAULT_GOAL = "这笔超期订单能否批准退款？"

CANDIDATES = [
    Candidate("c1", "这是直接把退款金额算出来的结论，清晰明确。", "agent-A"),
    Candidate("c2", "可能会退款，也许需要审核，大概要看政策。", "agent-B"),
    Candidate("c3", "根据退款政策第七条，签收后 7 天内可无理由退款，已核实订单未拆封。", "agent-C"),
    Candidate("c4", "可以退。", "agent-D"),
]


def run_arena(as_json: bool) -> int:
    result = Arena(candidates=CANDIDATES).run(DEFAULT_GOAL)
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print("=" * 78)
    print("Arena：4 个候选，两两对战（正反顺序各一次，避免位置偏差）")
    print("=" * 78)
    print(result["answer"])
    print()
    print("对战记录：")
    for b in result["battles"]:
        mark = "一致" if b["consistent"] else "两序不一致→平局"
        print("  %s vs %s → %s（%s）" % (b["a"], b["b"], b["winner"], mark))
    print()
    print("关键点：")
    print("  1. Arena 输出的是排名，Debate 输出的是一次裁决——两者别混。")
    print("  2. 每对都换顺序再判一次：位置偏差会让“先看到的”总赢。")
    print("  3. 对战是 O(N²) 次调用，候选池要设上限。")
    return 0


def run_swarm() -> int:
    print("=" * 78)
    print("Swarm / Handoff：去中心化移交（没有主管）")
    print("=" * 78)
    cases = [
        ("我要退款，订单 A1024", "正常：triage→refund→answer"),
        ("网站报错打不开", "正常：triage→tech→answer"),
        ("我要投诉你们", "正常：triage→human（escalation 是成功）"),
        ("我要退款", "陷阱：refund 想退回 triage，被“不重访”守卫拦下"),
    ]
    for goal, note in cases:
        result = Swarm().run(goal)
        print()
        print("请求: %s  （%s）" % (goal, note))
        print("  路径: %s" % " → ".join(result["path"]))
        print("  结果: ok=%s  escalated=%s" % (result["ok"], result["escalated"]))
        print("  %s" % result["answer"])
    print()
    print("关键点：")
    print("  1. 同一时刻只有一个 Agent 活跃，靠“移交”换人，不是靠轮流发言。")
    print("  2. 必须禁止重访目标，否则 A→B→A 会无限循环。")
    print("  3. 转人工是成功、是正确行为，不是失败。")
    return 0


def run_cycles() -> int:
    """Demonstrate intransitivity: A beats B, B beats C, C beats A."""
    # A judge that produces a rock-paper-scissors preference by id.
    cyclic = {"a": "b", "b": "c", "c": "a"}  # a>b, b>c, c>a

    def judge(x: Candidate, y: Candidate) -> int:
        if cyclic.get(x.id) == y.id:
            return 1
        if cyclic.get(y.id) == x.id:
            return -1
        return 0

    cands = [Candidate("a", "答案 A"), Candidate("b", "答案 B"), Candidate("c", "答案 C")]
    result = Arena(candidates=cands, judge=judge).run("循环判定演示")
    print("=" * 78)
    print("Arena 的一个固有问题：非传递性（A>B>C>A）")
    print("=" * 78)
    print(result["answer"])
    print()
    print("检测到的环:", result["cycles"])
    print()
    print("这说明：任何两两对战的方法都可能遇到环，排名依赖对阵顺序。")
    print("正确做法是如实报告，而不是假装第一名毫无争议。")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Mini Agent v6 — Arena / Swarm")
    parser.add_argument("--arena", action="store_true")
    parser.add_argument("--swarm", action="store_true")
    parser.add_argument("--cycles", action="store_true")
    parser.add_argument("--handoff-loop", action="store_true",
                        help="与 --swarm 相同，聚焦移交环")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.cycles:
        return run_cycles()
    if args.swarm or args.handoff_loop:
        return run_swarm()
    return run_arena(args.json)


if __name__ == "__main__":
    sys.exit(main())
