"""Pattern 4: Orchestrator-Workers (编排者—工人).

```
        +--------------------------------------+
        |            Orchestrator              |
        |  plan -> dispatch -> merge -> re-plan|
        +--------------------------------------+
             |          |            |
          [worker]   [worker]    [worker]
             |          |            |
             +----------+------------+--> results
```

The difference from a fixed fan-out: the orchestrator **plans**, and after
merging it may decide the plan was incomplete and **loop once more**.  This is
the pattern behind research agents and coding agents — you cannot enumerate the
steps up front.

The three things that go wrong, handled explicitly:

1. **Unbounded planning loops.**  A re-plan can itself re-plan.  ``max_rounds``
   plus a "no new subtasks" check stops it.
2. **Merge without evidence.**  The orchestrator merges *results*, and a worker
   that produced no evidence is not counted as progress.
3. **Cost explosion.**  Every round re-delegates; budget is reserved per round,
   not per worker, so round 2 cannot silently double the spend.

Differences from v4's Supervisor: the supervisor splits once; the orchestrator
re-plans.  Same contract, different loop.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

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
class OrchestratorWorkers:
    name: str = "orchestrator"
    max_rounds: int = 2
    # The planner maps (goal, round, done-subtasks) -> list of new sub-task names.
    # Returning [] means "nothing more to do".
    planner: Optional[Callable[[str, int, List[str]], List[str]]] = None
    workers: Dict[str, Stage] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.workers:
            self.workers = {
                "retrieve": Stage("retrieve", "检索：{input}"),
                "read_code": Stage("read_code", "阅读代码：{input}"),
                "run_tests": Stage("run_tests", "运行测试：{input}"),
            }
        if self.planner is None:
            # Default plan: round 0 does the first two, round 1 adds a third
            # only if the first two produced anything — a minimal "re-plan".
            def planner(goal: str, round_index: int, done: List[str]) -> List[str]:
                if round_index == 0:
                    return ["retrieve", "read_code"]
                if round_index == 1 and "test" not in done:
                    return ["run_tests"]
                return []

            self.planner = planner

    def run(self, goal: str, *, budget=None) -> PatternResult:
        started = time.monotonic()
        ctx = RunContext(goal=goal, budget=budget or new_budget())
        results: List[StageResult] = []
        done: List[str] = []
        collected: List[str] = []

        for round_index in range(self.max_rounds):
            planned = self.planner(goal, round_index, done)  # type: ignore[misc]
            ctx.log(step="plan", round=round_index, planned=planned)
            if not planned:
                ctx.log(step="plan_empty", round=round_index)
                break

            # Budget is reserved for this round's workers as a group.
            children = ctx.budget.reserve(len(planned))
            if not children:
                ctx.log(step="budget_stop", round=round_index)
                return build_summary(
                    self.name, ctx,
                    answer=self._merge(goal, collected) + "\n（预算耗尽，未完成全部计划）",
                    stop_reason=StopReason.PARTIAL, stage_results=results, started=started,
                )

            produced_new = False
            for worker_name, child_budget in zip(planned, children):
                stage = self.workers.get(worker_name)
                if stage is None:
                    ctx.log(step="unknown_worker", worker=worker_name)
                    continue
                sub_ctx = RunContext(goal=goal, budget=child_budget)
                result = safe_call(stage, goal, sub_ctx)
                ctx.budget.settle(child_budget)
                results.append(result)
                done.append(worker_name)
                if result.ok:
                    collected.append("%s: %s" % (worker_name, result.output))
                    produced_new = True
                ctx.log(stage=result.stage, round=round_index,
                        passed=result.passed, tokens=child_budget.tokens)

            # Termination on substance: if a round produced nothing usable,
            # re-planning again will not help.  Stop as PARTIAL, not SUCCESS.
            if not produced_new:
                ctx.log(step="stop", reason="no_progress", round=round_index)
                return build_summary(
                    self.name, ctx,
                    answer=self._merge(goal, collected) + "\n（本轮无有效产出，提前结束）",
                    stop_reason=StopReason.PARTIAL, stage_results=results, started=started,
                )

        if not collected:
            return build_summary(
                self.name, ctx, answer="编排未产出可用结果。",
                stop_reason=StopReason.FAILED, stage_results=results, started=started,
            )
        return build_summary(
            self.name, ctx, answer=self._merge(goal, collected),
            stop_reason=StopReason.SUCCESS, stage_results=results, started=started,
        )

    @staticmethod
    def _merge(goal: str, collected: Sequence[str]) -> str:
        if not collected:
            return "「%s」暂无可用结果。" % goal
        lines = ["「%s」编排结果：" % goal]
        lines.extend("- " + item for item in collected)
        return "\n".join(lines)
