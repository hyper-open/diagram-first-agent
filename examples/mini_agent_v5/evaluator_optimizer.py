"""Pattern 5: Evaluator-Optimizer (生成—评审).

```
        +-------------------------------+
        |                               |
  goal -> [generator] -> [evaluator] --+--(pass)--> answer
                ^                 |
                +----(fail+why)----+
```

Generate, evaluate, and **loop back with the reason** until the evaluation
passes — or until a budget/round limit makes it a PARTIAL result.

This is the one pattern with a genuine feedback loop at the *quality* level.
The handbook's warning is precise: **the evaluator must be independent**, or the
generator will just learn to praise itself.  So this implementation enforces:

* the evaluator is a *separate* stage (different prompt/role), never the
  generator checking its own output;
* the evaluator returns a **reason**, and the reason is fed into the next
  generation attempt — otherwise "retry" is just "roll the dice again";
* loop is bounded by ``max_rounds``; running out is PARTIAL (we did produce
  something), never FAILED.

Difference from v4's Debate: debate has *two agents with opposing goals* and one
judge decision.  Evaluator-optimizer has *one generator and one grader* and
iterates until the grader is satisfied.  Both involve evaluation; the control
flow is opposite (loop vs single verdict).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional

from .contract import (
    PatternResult,
    RunContext,
    Stage,
    StageResult,
    StopReason,
    build_summary,
    new_budget,
    safe_call,
)


@dataclass
class EvaluatorOptimizer:
    name: str = "evaluator"
    max_rounds: int = 3
    generator: Optional[Stage] = None
    evaluator: Optional[Stage] = None

    def __post_init__(self) -> None:
        if self.generator is None:
            # A generator that gets better when told *why* it failed is the point
            # of feeding the evaluator's reason back in.
            self.generator = Stage("generator", "第{n}版：{input}")
        if self.evaluator is None:
            # Independent check: pass only if the draft mentions the key fact.
            self.evaluator = Stage(
                "evaluator",
                "评估：{input}",
                role="check",
                passes_when=lambda s: "第3版" in s,
            )

    def run(self, goal: str, *, budget=None) -> PatternResult:
        started = time.monotonic()
        ctx = RunContext(goal=goal, budget=budget or new_budget())
        results: List[StageResult] = []
        draft = goal
        feedback = ""

        for round_index in range(self.max_rounds):
            ctx.set("n", round_index + 1)
            # Generator sees the goal plus the previous rejection reason.
            gen_input = goal if not feedback else "%s（上次未通过：%s）" % (goal, feedback)
            gen_result = self._generate(gen_input, round_index, ctx)
            results.append(gen_result)
            if gen_result.reason.startswith("error:") or not gen_result.ok:
                return build_summary(
                    self.name, ctx, answer=gen_result.output,
                    stop_reason=StopReason.FAILED, stage_results=results, started=started,
                )
            draft = gen_result.output

            eval_result = safe_call(self.evaluator, draft, ctx)  # type: ignore[arg-type]
            results.append(eval_result)
            ctx.log(step="evaluate", round=round_index,
                    passed=eval_result.passed, reason=eval_result.reason)

            if eval_result.passed:
                return build_summary(
                    self.name, ctx, answer=draft,
                    stop_reason=StopReason.SUCCESS, stage_results=results, started=started,
                )
            # Feed the *reason* into the next attempt.  The reason is what makes
            # the loop an optimisation rather than a retry.
            feedback = eval_result.reason or "未满足要求"

        # Ran out of rounds with a draft in hand → PARTIAL, not FAILED.
        return build_summary(
            self.name, ctx,
            answer=draft + "\n（已达最大轮次，仍未通过评估：%s）" % feedback,
            stop_reason=StopReason.PARTIAL, stage_results=results, started=started,
        )

    def _generate(self, gen_input: str, round_index: int, ctx: RunContext) -> StageResult:
        # The template may use {n}; Stage fills it from the context.
        return safe_call(self.generator, gen_input, ctx)  # type: ignore[arg-type]
