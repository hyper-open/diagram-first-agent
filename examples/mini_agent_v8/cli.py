"""CLI for v8: harness inside a graph.

    python3 -m examples.mini_agent_v8.cli --demo        # 中断 → 恢复 两趟
    python3 -m examples.mini_agent_v8.cli --trace       # 打印 state 与 checkpoint
    python3 -m examples.mini_agent_v8.cli --json
"""
from __future__ import annotations

import argparse
import json
import sys

from .compose import resume_after_approval, run_to_approval


def _print_stage(state: dict, key: str) -> None:
    r = state.get(key)
    if not r:
        return
    print("    %-16s status=%-8s steps=%s tokens=%d"
          % (key.replace("_result", ""), r["status"], r["steps"], r["tokens"]))
    for line in r["output"].splitlines():
        print("        " + line)


def run_demo(show_trace: bool, as_json: bool) -> int:
    if as_json:
        first = run_to_approval("处理退款 A1024")["result"]
        second = resume_after_approval("处理退款 A1024")["result"]
        print(json.dumps({"interrupted": first.to_dict(), "completed": second.to_dict()},
                         ensure_ascii=False, indent=2, default=str))
        return 0

    print("=" * 76)
    print("Harness + Graph：图管「阶段」，harness 管「每个阶段的内部循环」")
    print("=" * 76)

    print("\n【第一趟】跑到审批闸门 → 中断")
    out = run_to_approval("处理退款 A1024")
    res = out["result"]
    print("  status=%s  path=%s" % (res.status, " → ".join(res.path)))
    print("  中断于: %s  提示: %s" % (res.interrupted_at, res.prompt))
    print("  stage_order:", res.state.get("stage_order"))
    print("  已产出各阶段（每个都是一个 harness run）:")
    for k in ("gather_result", "draft_result"):
        _print_stage(res.state, k)
    print("  checkpoint 数: %d（每节点一份，可恢复）" % len(out["graph"].checkpointer))

    print("\n【第二趟】人工批准 → 从头跑到结束")
    out2 = resume_after_approval("处理退款 A1024")
    res2 = out2["result"]
    print("  status=%s  path=%s" % (res2.status, " → ".join(res2.path)))
    for k in ("gather_result", "draft_result", "approve_result", "summarize_result"):
        _print_stage(res2.state, k)
    print("  tokens_total=%d（各阶段累加）" % res2.state.get("tokens_total"))

    if show_trace:
        print("\n【完整 state】")
        print(json.dumps(res2.state, ensure_ascii=False, indent=2, default=str))
        print("\n【checkpoint 历史】")
        for snap in out2["graph"].checkpointer.history():
            print("  step=%d node=%s" % (snap["step"], snap["node"]))

    print()
    print("分工：")
    print("  · Graph：决定 gather→draft→approve→summarize 的顺序、检查点、中断/恢复")
    print("  · Harness：每个节点内部 规划→执行→验证→重试，产出 run result")
    print("  · 节点只返回 state 更新，图负责合并——这就是可检查点/可恢复的前提")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Harness + Graph demo")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    return run_demo(show_trace=args.trace, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
