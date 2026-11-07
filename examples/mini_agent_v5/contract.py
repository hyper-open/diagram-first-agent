"""Shared contract for the five orchestration patterns.

The handbook's orchestration chapter says the layer must answer *"when do we
stop?"* and lists five terminal states.  Those five states are the ``StopReason``
enum here — every pattern returns one, so "how did this end?" is never ambiguous.

We reuse ``mini_agent_v4.contract.BudgetTree`` for accounting: the two packages
describe different things (patterns vs topologies) but they must agree on what
a budget is, or costs become incomparable across the book.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..mini_agent_v4.contract import BudgetTree

# A stage is a pure-ish function: text in, a result out.  Real stages call the
# model; the pattern only cares about the interface.
StageFn = Callable[[str, "RunContext"], "StageResult"]


class StopReason(str, Enum):
    """The five terminal states from the orchestration chapter (§ 六)."""

    SUCCESS = "success"              # 验证通过
    PARTIAL = "partial"              # 预算耗尽但有进展
    BLOCKED = "blocked"              # 缺信息/缺权限
    FAILED = "failed"                # 不可恢复错误
    NEEDS_HUMAN = "needs_human"      # 高风险/低置信 → 人工接管

    @property
    def ok(self) -> bool:
        return self is StopReason.SUCCESS

    @property
    def escalated(self) -> bool:
        return self is StopReason.NEEDS_HUMAN


@dataclass
class StageResult:
    """Output of one stage.

    ``passed`` is tri-state: ``True``/``False`` for check stages, ``None`` for
    transform stages.  That is why "did this stage go wrong" cannot be
    ``passed is False`` alone: a transform stage that raised has ``passed=None``
    **and** a ``reason``.  ``ok`` is the single source of truth for "usable at
    all"; ``failed`` is kept for check-specific logic (a failed check is not an
    error, it is a verdict).
    """

    stage: str
    output: str
    passed: Optional[bool] = None
    reason: str = ""
    tokens: int = 0
    elapsed_ms: int = 0

    @property
    def failed(self) -> bool:
        """Only meaningful for check stages: the check did not pass."""
        return self.passed is False

    @property
    def ok(self) -> bool:
        """Did this stage produce a usable output?  A raised stage is not ok."""
        if self.passed is False:
            return False
        if self.reason.startswith("error:"):
            return False
        return bool(self.output.strip())


@dataclass
class RunContext:
    """What every stage may read.  Budget lives here so stages can be charged."""

    goal: str
    budget: BudgetTree
    variables: Dict[str, Any] = field(default_factory=dict)
    trace: List[Dict[str, Any]] = field(default_factory=list)

    def log(self, **event: Any) -> None:
        self.trace.append(event)

    def get(self, key: str, default: Any = None) -> Any:
        return self.variables.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.variables[key] = value


@dataclass
class PatternResult:
    """Uniform result so all five patterns are comparable — same as v4."""

    pattern: str
    goal: str
    answer: str
    stop_reason: StopReason
    stages: int
    tokens_used: int
    elapsed_ms: int
    trace: List[Dict[str, Any]] = field(default_factory=list)
    stage_results: List[StageResult] = field(default_factory=list)
    questions: List[str] = field(default_factory=list)  # when blocked

    @property
    def ok(self) -> bool:
        return self.stop_reason.ok

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pattern": self.pattern,
            "goal": self.goal,
            "answer": self.answer,
            "ok": self.ok,
            "stop_reason": self.stop_reason.value,
            "stages": self.stages,
            "tokens_used": self.tokens_used,
            "elapsed_ms": self.elapsed_ms,
            "questions": list(self.questions),
            "stage_results": [
                {
                    "stage": s.stage,
                    "output": s.output,
                    "passed": s.passed,
                    "reason": s.reason,
                    "tokens": s.tokens,
                }
                for s in self.stage_results
            ],
            "trace": list(self.trace),
        }


# ---------------------------------------------------------------------------
# Building deterministic stages for the examples/tests
# ---------------------------------------------------------------------------


@dataclass
class Stage:
    """A scripted stage: transform text with a template, optionally judge it.

    ``role="check"`` stages return ``passed``; ``role="transform"`` stages return
    output only.  ``cost_tokens`` is charged to the shared budget so budget
    behaviour is exercised in every pattern.
    """

    name: str
    template: str = "{input}"
    role: str = "transform"  # transform | check
    passes_when: Optional[Callable[[str], bool]] = None
    cost_tokens: int = 600

    def __call__(self, text: str, ctx: RunContext) -> StageResult:
        started = time.monotonic()
        ctx.budget.spend(tokens=self.cost_tokens, steps=1)
        # ``n`` is available for templates that vary by round (e.g. "第{n}版");
        # unknown placeholders fall back to empty rather than raising.
        rendered = self.template.format(input=text, goal=ctx.goal, n=ctx.get("n", ""))
        passed: Optional[bool] = None
        reason = ""
        if self.role == "check":
            passed = self.passes_when(rendered) if self.passes_when else True
            reason = "" if passed else "未通过检查"
        return StageResult(
            stage=self.name,
            output=rendered,
            passed=passed,
            reason=reason,
            tokens=self.cost_tokens,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def new_budget(max_tokens: int = 30_000, max_steps: int = 20) -> BudgetTree:
    return BudgetTree(max_tokens=max_tokens, max_steps=max_steps, label="pattern")


class StageError(RuntimeError):
    """A stage raised instead of returning a result — the pattern decides what
    that means (usually FAILED, never an unhandled crash)."""


def safe_call(stage: StageFn, text: str, ctx: RunContext) -> StageResult:
    try:
        return stage(text, ctx)
    except Exception as exc:  # noqa: BLE001 - surface as a failed stage
        return StageResult(
            stage=getattr(stage, "name", "stage"),
            output="",
            # A raised stage is an *error*, not a verdict.  Keep passed=None for
            # transforms so the distinction from "check failed" is preserved;
            # ``ok`` uses the "error:" prefix to make it unmistakably not ok.
            passed=None,
            reason="error: %s: %s" % (type(exc).__name__, exc),
        )


def build_summary(pattern: str, ctx: RunContext, answer: str,
                  stop_reason: StopReason, stage_results: Sequence[StageResult],
                  started: float, questions: Optional[Sequence[str]] = None) -> PatternResult:
    return PatternResult(
        pattern=pattern,
        goal=ctx.goal,
        answer=answer,
        stop_reason=stop_reason,
        stages=len(stage_results),
        tokens_used=ctx.budget.tokens,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        trace=list(ctx.trace),
        stage_results=list(stage_results),
        questions=list(questions or []),
    )
