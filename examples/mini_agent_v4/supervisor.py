"""Topology 1a: Supervisor.

```
          +-----------+
  goal -->| Supervisor|<-- child results
          +-----------+
            |     |
        Worker A Worker B     (independent subtasks)
            |     |
            +--+--+
               v
           merged answer
```

Control flow: the supervisor *decides* the split up front, fans out to N
workers that never talk to each other, then merges.

Why this is the default choice (and the handbook's recommendation):

* responsibility is clear — one place decides and one place merges;
* workers are isolated, so one worker's bad context cannot poison another;
* failure is visible — a failed worker is a missing input, not a corrupted one.

The cost is the supervisor itself: it is both a latency bottleneck and the
single point where merges can go wrong.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .contract import (
    AgentRole,
    BudgetTree,
    SubResult,
    TopologyResult,
    build_worker,
    collect_failures,
    merge_confidence,
    new_task,
)


@dataclass
class SupervisorTopology:
    """Split -> fan out -> merge.  One leader, N isolated workers."""

    name: str = "supervisor"
    workers: List[AgentRole] = field(default_factory=list)
    # How the supervisor turns child summaries into one answer.
    merge: str = "bullets"  # bullets | first_usable

    def __post_init__(self) -> None:
        if not self.workers:
            self.workers = [
                AgentRole("researcher", "检索与 {goal} 相关的事实"),
                AgentRole("analyzer", "分析 {goal} 的关键取舍"),
                AgentRole("writer", "把 {goal} 整理成结论"),
            ]

    def run(self, goal: str, *, budget: Optional[BudgetTree] = None) -> TopologyResult:
        started = time.monotonic()
        budget = budget or BudgetTree(max_tokens=30_000, max_steps=12)
        trace: List[Dict[str, Any]] = []

        # 1. The supervisor plans the split (this is the only "planning" step).
        children = budget.reserve(len(self.workers))
        trace.append({
            "step": "plan",
            "supervisor": "lead",
            "planned": [w.name for w in self.workers],
            "spawned": len(children),
            "budget_each": [c.max_tokens for c in children],
        })

        # 2. Fan out.  Workers are isolated: each gets its own task packet and
        #    its own budget slice, and they do not see each other's output.
        results: List[SubResult] = []
        for role, child_budget in zip(self.workers, children):
            task = new_task(
                role.name,
                role.render(goal),
                parent_id="lead",
                depth=1,
                constraints=["只回答本题", "给出证据"],
                success_criteria="有结论且能指出依据",
            )
            worker = build_worker(role)
            result = worker(task, child_budget)
            results.append(result)
            budget.settle(child_budget)
            trace.append({
                "step": "worker",
                "role": role.name,
                "task_id": task.task_id,
                "ok": result.ok,
                "tokens": result.tokens,
                "reason": result.reason,
            })

        # 3. Merge.  Only usable results contribute; failures are reported.
        usable = [r for r in results if r.usable]
        failures = collect_failures(results)
        if not usable:
            answer = "无法完成：所有子任务都失败或没有可用结论。"
            ok = False
        else:
            answer = self._merge(goal, usable)
            ok = True
        trace.append({
            "step": "merge",
            "accepted": len(usable),
            "rejected": len(results) - len(usable),
        })

        return TopologyResult(
            topology=self.name,
            goal=goal,
            answer=answer,
            ok=ok,
            confidence=merge_confidence(results),
            subtasks=len(results),
            tokens_used=budget.tokens,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            failures=failures,
            trace=trace,
            results=results,
        )

    def _merge(self, goal: str, usable: Sequence[SubResult]) -> str:
        if self.merge == "first_usable":
            return usable[0].summary
        lines = ["针对「%s」，综合各子任务结论：" % goal]
        for result in usable:
            lines.append("- [%s] %s" % (result.role, result.summary))
        return "\n".join(lines)

