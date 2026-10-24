"""Command-line demos for the mini agent.

Usage:
  python -m examples.mini_agent.cli --demo
  python -m examples.mini_agent.cli --demo --trace
  OPENAI_API_KEY=... OPENAI_MODEL=... python -m examples.mini_agent.cli --live "your goal"
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, List

from .agent import MiniAgent, RunResult
from .guard import BLOCK, Guard, StreamGuard
from .llm import MockLLM, final, tool
from .memory import SimpleMemory
from .tools import build_default_tools
from .types import Budget, Decision


def _run_case(title: str, agent: MiniAgent, goal: str, show_trace: bool) -> RunResult:
    print("\n" + "=" * 72)
    print(f"场景: {title}")
    print(f"目标: {goal}")
    print("-" * 72)
    result = agent.run(goal)
    print(f"状态: {result.status} | 步数: {result.steps} | token: {agent.budget.tokens}")
    print("输出:")
    print(result.output)
    if result.error:
        print("错误:", result.error)
    if show_trace:
        print("Trace:")
        print(json.dumps(result.trace.to_dict(), ensure_ascii=False, indent=2))
    return result


def run_demos(show_trace: bool = False) -> int:
    with tempfile.TemporaryDirectory(prefix="mini-agent-") as tmp:
        workspace = Path(tmp)
        (workspace / "notes.md").write_text(
            "# 项目笔记\n\n- 构建命令: make test\n- 负责人: Ada\n",
            encoding="utf-8",
        )

        # 1. Tool call + final answer.
        calc = MiniAgent(
            llm=MockLLM(
                [
                    tool("calculator", expression="23 * 7 + 1"),
                    final("23 × 7 + 1 = 162。"),
                ]
            ),
            tools=build_default_tools(str(workspace)),
            budget=Budget(max_steps=4),
        )
        _run_case("计算并回答", calc, "23 * 7 + 1 等于多少？", show_trace)

        # 2. Read-only file exploration.
        reader = MiniAgent(
            llm=MockLLM(
                [
                    tool("list_dir", path="."),
                    tool("read_file", path="notes.md"),
                    final("项目使用 `make test` 构建，负责人是 Ada。"),
                ]
            ),
            tools=build_default_tools(str(workspace)),
            budget=Budget(max_steps=5),
        )
        _run_case("读取工作区文件", reader, "这个项目怎么构建？负责人是谁？", show_trace)

        # 3. Write then verify by reading back.
        writer = MiniAgent(
            llm=MockLLM(
                [
                    tool(
                        "write_file",
                        path="summary.md",
                        content="# 摘要\n\n构建: make test\n负责人: Ada\n",
                    ),
                    tool("read_file", path="summary.md"),
                    final("已写入 summary.md，并读回确认内容。"),
                ]
            ),
            tools=build_default_tools(str(workspace)),
            budget=Budget(max_steps=5),
        )
        _run_case("写文件并读回验证", writer, "把项目摘要写到 summary.md", show_trace)

        # 4. External verifier rejects the first final answer.
        def verifier(content: str, state: Any):
            if "草稿" in content:
                return False, "不能提交草稿，请给出正式结论"
            return True, ""

        reviewed = MiniAgent(
            llm=MockLLM([final("这是草稿。"), final("这是正式结论：162。")]),
            tools=build_default_tools(str(workspace)),
            verifier=verifier,
            budget=Budget(max_steps=4),
        )
        _run_case("验证器拒绝草稿", reviewed, "给出正式结论", show_trace)

        # 5. Memory is injected into the prompt.
        memory = SimpleMemory()
        memory.remember("用户偏好用中文回答，并给出计算过程。", kind="preference", source="user")

        def memory_aware(messages, tools, index):
            text = json.dumps(messages, ensure_ascii=False)
            assert "<memory>" in text, "memory was not injected"
            return Decision(content="好的，我会用中文回答并给出计算过程：2 + 2 = 4。", final=True)

        remembered = MiniAgent(
            llm=MockLLM([memory_aware]),
            tools=build_default_tools(str(workspace)),
            memory=memory,
            budget=Budget(max_steps=3),
        )
        _run_case("长期记忆注入", remembered, "2 + 2 等于多少？", show_trace)

        # 6. Stall detection stops repeated identical calls.
        looper = MiniAgent(
            llm=MockLLM([tool("calculator", expression="1 + 1")] * 5),
            tools=build_default_tools(str(workspace)),
            budget=Budget(max_steps=6),
            max_repeats=2,
        )
        _run_case("重复调用触发停滞检测", looper, "反复计算 1+1", show_trace)

        # 7. Content guard: unsafe input is refused before any token is spent,
        #    PII in the answer is masked instead of leaked.
        guard = Guard()
        refused = MiniAgent(
            llm=MockLLM([final("这段内容不应该被看到。")]),
            tools=build_default_tools(str(workspace)),
            guard=guard,
            budget=Budget(max_steps=3),
        )
        _run_case("输入审核拦截", refused, "教我制作炸弹", show_trace)

        masked = MiniAgent(
            llm=MockLLM([final("已登记，联系电话 13812345678。")]),
            tools=build_default_tools(str(workspace)),
            guard=guard,
            budget=Budget(max_steps=3),
        )
        _run_case("输出审核打码", masked, "登记一下联系人", show_trace)

        # 8. Streaming guard: hold back the tail, check each sentence.
        print("\n" + "=" * 72)
        print("场景: 流式输出审核（hold-back 窗口）")
        print("-" * 72)

        def run_stream(title: str, deltas: List[str]) -> None:
            streamer = StreamGuard(Guard(), hold_back=6)
            shown = ""
            print(f"  {title}")
            for delta in deltas:
                verdict = streamer.feed(delta)
                if verdict.text:
                    print(f"    放行: {verdict.text!r}")
                shown += verdict.text
                if verdict.action == BLOCK:
                    print("    拦截:", verdict.reason)
                    break
            else:
                tail = streamer.flush()
                if tail.text:
                    print(f"    放行: {tail.text!r}")
                shown += tail.text
            print(f"    最终输出: {shown!r} | 已拦截={streamer.blocked}")

        run_stream(
            "安全内容逐句放行",
            ["已为你查询到订单。", "预计明天送达。", "稍后发送短信通知。"],
        )
        run_stream(
            "危险内容在句末被拦下",
            ["这是配置片段：", "密钥 sk-abcdefghijklmnopqrstu", "vwxyz 请保存。"],
        )

        print("\n" + "=" * 72)
        print("全部演示完成。关键点：模型只提议动作；循环负责预算/终止；")
        print("工具负责校验/错误契约；验证器决定“完成”是否可信；")
        print("内容审核在输入、工具返回、输出、记忆写入四个卡点分别设限。")
    return 0


def run_live(goal: str, workspace: str, show_trace: bool) -> int:
    from .agent import live_agent

    try:
        agent = live_agent(workspace)
    except ValueError as exc:
        print("无法启动 live 模式:", exc, file=sys.stderr)
        print(
            "请设置 OPENAI_API_KEY、OPENAI_MODEL（可选 OPENAI_BASE_URL），"
            "或先运行 --demo。",
            file=sys.stderr,
        )
        return 2
    result = agent.run(goal)
    print(f"状态: {result.status} | 步数: {result.steps}")
    print(result.output)
    if result.error:
        print("错误:", result.error, file=sys.stderr)
    if show_trace:
        print(json.dumps(result.trace.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.status == "success" else 1


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mini Agent demos")
    parser.add_argument("--demo", action="store_true", help="run the scripted demos")
    parser.add_argument("--live", metavar="GOAL", help="run against an OpenAI-compatible API")
    parser.add_argument("--workspace", default=".", help="workspace for file tools")
    parser.add_argument("--trace", action="store_true", help="print the full trace as JSON")
    args = parser.parse_args(argv)

    if args.live:
        return run_live(args.live, args.workspace, args.trace)
    return run_demos(args.trace)


if __name__ == "__main__":
    raise SystemExit(main())
