"""CLI for v9: multi-intent -> routing -> topology selection -> execution.

    python3 -m examples.mini_agent_v9.cli --demo
    python3 -m examples.mini_agent_v9.cli --ask "我要退款，另外查下订单A1024"
    python3 -m examples.mini_agent_v9.cli --matrix    # 决策矩阵一览
    python3 -m examples.mini_agent_v9.cli --demo --json
"""
from __future__ import annotations

import argparse
import json
import sys

from .pipeline import handle
from .select_topology import IntentItem, select_topology

DEMO = [
    "我要退款，另外订单 A1024 也帮我查一下到哪了",
    "这个功能报错了，而且我要投诉你们服务太差",
    "解释一下退款政策",
    "帮我把这个月的退款整理成表格并汇总金额",
    "你好",
]

TOPOS = ["single", "parallel", "chaining", "supervisor", "plan_execute", "escalate", "clarify"]


def _fmt(r: dict) -> str:
    out = []
    out.append("输入: %s" % r["input"])
    out.append("  意图: " + "  ".join(
        "[%s/%s/%s]" % (i["intent"], i["route"], i["complexity"]) for i in r["intents"]))
    out.append("  多意图: %s" % r["is_multi"])
    t = r["topology"]
    out.append("  ▶ 选中拓扑: %s" % t["topology"].upper())
    out.append("    理由: %s" % t["reason"])
    out.append("  执行:")
    for line in r["result"]["output"].splitlines():
        out.append("    " + line)
    return "\n".join(out)


def run_demo(as_json: bool) -> int:
    results = [handle(t) for t in DEMO]
    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    print("=" * 78)
    print("多意图 → 意图路由 → 选拓扑 → 执行")
    print("=" * 78)
    for r in results:
        print(_fmt(r))
        print("-" * 78)
    print()
    print("选择规则（按优先级）：")
    print("  1. 置信度过低/无法归类        → clarify（先追问）")
    print("  2. 含投诉/合规                → escalate（转人工，不被业务埋没）")
    print("  3. 单意图·简单                → single / rag")
    print("  4. 单意图·复杂                → plan_execute")
    print("  5. 多意图·有依赖              → chaining / supervisor")
    print("  6. 多意图·可能冲突            → supervisor（合并要裁决）")
    print("  7. 多意图·相互独立            → parallel（并行扇出）")
    return 0


def run_matrix() -> int:
    print("=" * 78)
    print("拓扑选择决策矩阵")
    print("=" * 78)
    print("条件                         → 拓扑")
    print("-" * 78)
    rows = [
        (["置信度低/无法归类"], "clarify"),
        (["含投诉、合规"], "escalate"),
        (["单个·简单"], "single / rag"),
        (["单个·复杂多步"], "plan_execute"),
        (["多个·有依赖(2个)"], "chaining"),
        (["多个·有依赖(>2)"], "supervisor"),
        (["多个·结论冲突"], "supervisor"),
        (["多个·独立"], "parallel"),
    ]
    for cond, topo in rows:
        print("%-28s → %s" % ("、".join(cond), topo))
    print()
    print("记忆点：独立→并行扇出；依赖→链式；冲突→主管裁决。三者最易混。")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="v9 topology selection")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--ask", metavar="TEXT")
    parser.add_argument("--matrix", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.matrix:
        return run_matrix()
    if args.ask:
        print(json.dumps(handle(args.ask), ensure_ascii=False, indent=2, default=str))
        return 0
    return run_demo(args.json)


if __name__ == "__main__":
    sys.exit(main())
