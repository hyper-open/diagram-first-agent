"""A tiny evaluation harness for the mini agent.

Run with:
  python3 -m examples.mini_agent.eval

The point is the shape of the report, not a large benchmark: task success,
pass^k reliability, steps, and token cost per case.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .agent import MiniAgent, Verifier
from .llm import MockLLM
from .tools import build_default_tools
from .types import Budget


@dataclass
class EvalCase:
    name: str
    goal: str
    script: List[Any]
    expected: str
    tags: List[str] = field(default_factory=list)
    fixture: Optional[Callable[[Path], None]] = None
    verifier: Optional[Verifier] = None
    max_steps: int = 6

    def passed(self, output: str) -> bool:
        return self.expected in output


@dataclass
class RunRecord:
    case: str
    run: int
    passed: bool
    steps: int
    tokens: int
    status: str
    output: str


def _default_cases() -> List[EvalCase]:
    def notes_fixture(root: Path) -> None:
        (root / "notes.md").write_text(
            "# Notes\n\nbuild: make test\nowner: Ada\n", encoding="utf-8"
        )

    def reject_draft(content: str, state) -> tuple:
        if "草稿" in content:
            return False, "不能提交草稿"
        return True, ""

    return [
        EvalCase(
            name="calculator",
            goal="23 * 7 + 1 等于多少？",
            script=[
                {"tool": "calculator", "arguments": {"expression": "23 * 7 + 1"}},
                {"final": "答案是 162。"},
            ],
            expected="162",
            tags=["tool", "math"],
        ),
        EvalCase(
            name="read_file",
            goal="项目怎么构建？",
            script=[
                {"tool": "read_file", "arguments": {"path": "notes.md"}},
                {"final": "使用 make test 构建。"},
            ],
            expected="make test",
            fixture=notes_fixture,
            tags=["tool", "file"],
        ),
        EvalCase(
            name="write_read_back",
            goal="把摘要写入 summary.md 并确认",
            script=[
                {
                    "tool": "write_file",
                    "arguments": {"path": "summary.md", "content": "build: make test\n"},
                },
                {"tool": "read_file", "arguments": {"path": "summary.md"}},
                {"final": "已写入并读回确认。"},
            ],
            expected="已写入",
            fixture=notes_fixture,
            tags=["tool", "write"],
        ),
        EvalCase(
            name="verifier",
            goal="给出正式结论",
            script=[{"final": "这是草稿。"}, {"final": "这是正式结论。"}],
            expected="正式结论",
            verifier=reject_draft,
            tags=["verification"],
        ),
    ]


def run_case(case: EvalCase, runs: int = 3) -> List[RunRecord]:
    records: List[RunRecord] = []
    for run in range(runs):
        with tempfile.TemporaryDirectory(prefix="mini-eval-") as tmp:
            root = Path(tmp)
            if case.fixture:
                case.fixture(root)
            agent = MiniAgent(
                llm=MockLLM(case.script),
                tools=build_default_tools(str(root)),
                verifier=case.verifier,
                budget=Budget(max_steps=case.max_steps),
                max_repeats=10,
            )
            result = agent.run(case.goal)
            records.append(
                RunRecord(
                    case=case.name,
                    run=run,
                    passed=case.passed(result.output),
                    steps=result.steps,
                    tokens=agent.budget.tokens,
                    status=result.status,
                    output=result.output,
                )
            )
    return records


def summarize(records: List[RunRecord], runs: int) -> Dict[str, float]:
    if not records:
        return {}
    first = [r.passed for r in records if r.run == 0]
    by_case: Dict[str, List[bool]] = {}
    for record in records:
        by_case.setdefault(record.case, []).append(record.passed)
    return {
        "pass@1": sum(first) / len(first),
        "pass^k": sum(all(v) for v in by_case.values()) / len(by_case),
        "avg_steps": sum(r.steps for r in records) / len(records),
        "avg_tokens": sum(r.tokens for r in records) / len(records),
    }


def evaluate(cases: Optional[List[EvalCase]] = None, runs: int = 3) -> Dict[str, Any]:
    cases = cases or _default_cases()
    records: List[RunRecord] = []
    for case in cases:
        records.extend(run_case(case, runs=runs))
    return {
        "cases": cases,
        "records": records,
        "summary": summarize(records, runs),
    }


def _print_report(result: Dict[str, Any], runs: int) -> None:
    records: List[RunRecord] = result["records"]
    print(f"cases={len(result['cases'])} runs={runs}")
    print(f"{'case':<20} {'pass@1':>7} {'pass^k':>7} {'steps':>6} {'tokens':>7}")
    for case in result["cases"]:
        rows = [r for r in records if r.case == case.name]
        first = rows[0].passed if rows else False
        all_pass = all(r.passed for r in rows)
        print(
            f"{case.name:<20} {str(first):>7} {str(all_pass):>7} "
            f"{rows[0].steps if rows else 0:>6} {rows[0].tokens if rows else 0:>7}"
        )
    summary = result["summary"]
    print(
        f"\noverall: pass@1={summary['pass@1']:.2f} "
        f"pass^{runs}={summary['pass^k']:.2f} "
        f"avg_steps={summary['avg_steps']:.2f} "
        f"avg_tokens={summary['avg_tokens']:.0f}"
    )
    failures = [r for r in records if not r.passed]
    if failures:
        print("\nfailures:")
        for failure in failures:
            print(f"- {failure.case} run={failure.run} status={failure.status}")
            print("  " + failure.output.replace("\n", "\n  ")[:500])


def main() -> int:
    result = evaluate(runs=3)
    _print_report(result, runs=3)
    return 0 if result["summary"]["pass@1"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
