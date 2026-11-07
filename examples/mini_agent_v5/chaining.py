"""Pattern 1: Prompt Chaining (链式).

```
  goal -> [generate] -> [check] -> [revise] -> [format] -> answer
                          |
                          +--(fail)--> PARTIAL
```

A fixed sequence with optional checkpoints between stages.  The control flow is
written by the developer; the model only fills each stage.

Why it is worth having even though it looks trivial:

* **quality checks become flow, not hope** — "don't ask the model to check
  itself; put a check stage in" is one of the most useful moves in the book;
* each stage is independently testable;
* a failed checkpoint gives a *meaningful* terminal state (PARTIAL), not a
  silent bad answer.

The cost: **errors amplify downstream** and latency is linear in stage count.
That is why a check stage should sit *before* the expensive stages.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List

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


def default_stages() -> List[Stage]:
    return [
        Stage("generate", "初稿：{input}"),
        Stage("check", "检查：{input}", role="check",
              passes_when=lambda s: "初稿" in s),
        Stage("revise", "修订后的终稿：{input}"),
        Stage("format", "格式化输出：{input}"),
    ]


@dataclass
class PromptChaining:
    name: str = "chaining"
    stages: List[Stage] = field(default_factory=list)
    fail_fast: bool = True  # stop at the first failed checkpoint

    def __post_init__(self) -> None:
        if not self.stages:
            self.stages = default_stages()

    def run(self, goal: str, *, budget=None) -> PatternResult:
        started = time.monotonic()
        ctx = RunContext(goal=goal, budget=budget or new_budget())
        results: List[StageResult] = []
        current = goal

        for stage in self.stages:
            result = safe_call(stage, current, ctx)
            results.append(result)
            ctx.log(stage=result.stage, passed=result.passed, tokens=result.tokens)

            if result.reason.startswith("error:"):
                # A stage that raised is not a failed checkpoint; it is a broken
                # pipeline.  Report FAILED, don't pretend the draft is the answer.
                return build_summary(
                    self.name, ctx, answer=current,
                    stop_reason=StopReason.FAILED, stage_results=results,
                    started=started,
                )
            if result.passed is False:
                if self.fail_fast:
                    # A failed checkpoint is a real terminal state: return what
                    # we have as PARTIAL instead of fabricating a final answer.
                    return build_summary(
                        self.name, ctx, answer=current,
                        stop_reason=StopReason.PARTIAL, stage_results=results,
                        started=started,
                    )
                ctx.set("had_failure", True)
                continue
            # A *check* stage verdicts, it does not transform the text.  Its
            # output is a judgement, so feeding it into the next stage would
            # corrupt the content ("格式化输出：检查：初稿：...").  Only
            # transform stages update ``current``.
            if stage.role != "check":
                current = result.output

        reason = StopReason.PARTIAL if ctx.get("had_failure") else StopReason.SUCCESS
        return build_summary(
            self.name, ctx, answer=current,
            stop_reason=reason, stage_results=results, started=started,
        )
