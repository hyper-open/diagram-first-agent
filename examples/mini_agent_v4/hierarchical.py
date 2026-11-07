"""Topology 1c: Hierarchical.

```
  Lead
   ├─ Sub-lead A
   │    ├─ Worker A1
   │    └─ Worker A2
   └─ Sub-lead B
        └─ Worker B1
```

Control flow: delegation goes down levels and results come back up.  Used for
very large tasks where one supervisor cannot hold the whole plan in context.

The two things that break hierarchical systems — both handled here:

1. **Depth explosion.** Each level can delegate again.  Without ``max_depth`` a
   task fans out exponentially.  ``SubTask.depth`` is checked *before* delegating.
2. **Result laundromat.** A sub-lead that summarizes its workers again and again
   loses evidence.  Here a sub-lead merges its children's evidence lists rather
   than rewriting them, so the lead can still trace a claim to the worker that
   produced it.

Cost note: every level is a hop, so latency is additive.  The handbook's
guidance is to use this only when the plan genuinely does not fit in one
context — not merely because the org chart has managers.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

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
class Branch:
    """A sub-lead and its workers.  One level of the tree."""

    lead: AgentRole
    workers: List[AgentRole] = field(default_factory=list)


@dataclass
class HierarchicalTopology:
    name: str = "hierarchical"
    branches: List[Branch] = field(default_factory=list)
    max_depth: int = 2  # lead(0) -> sub-lead(1) -> worker(2)

    def __post_init__(self) -> None:
        if not self.branches:
            self.branches = [
                Branch(
                    lead=AgentRole("lead-retrieval", "统筹 {goal} 的信息收集"),
                    workers=[
                        AgentRole("search", "检索 {goal} 的背景资料"),
                        AgentRole("verify", "核对 {goal} 相关事实"),
                    ],
                ),
                Branch(
                    lead=AgentRole("lead-synthesis", "统筹 {goal} 的结论合成"),
                    workers=[AgentRole("draft", "起草 {goal} 的答复")],
                ),
            ]

    def run(self, goal: str, *, budget: Optional[BudgetTree] = None) -> TopologyResult:
        started = time.monotonic()
        budget = budget or BudgetTree(max_tokens=40_000, max_steps=16)
        trace: List[Dict[str, Any]] = []
        all_results: List[SubResult] = []

        top_task = new_task("lead", goal, depth=0, max_depth=self.max_depth)
        branch_budgets = budget.reserve(len(self.branches))
        trace.append({
            "step": "plan",
            "lead": "lead",
            "branches": [b.lead.name for b in self.branches],
            "depth_budget": [c.max_tokens for c in branch_budgets],
        })

        branch_summaries: List[Tuple[str, SubResult]] = []
        for branch, branch_budget in zip(self.branches, branch_budgets):
            subtask = new_task(
                branch.lead.name,
                branch.lead.render(goal),
                parent_id="lead",
                depth=1,
                max_depth=self.max_depth,
            )
            merged = self._run_branch(branch, subtask, branch_budget, trace, all_results, goal)
            budget.settle(branch_budget)
            branch_summaries.append((branch.lead.name, merged))

        # The lead merges branch results and keeps every branch's evidence.
        usable = [r for _, r in branch_summaries if r.usable]
        failures = collect_failures([r for _, r in branch_summaries])
        if not usable:
            answer = "无法完成：所有分支都没有产出可用结论。"
            ok = False
        else:
            ok = True
            lines = ["针对「%s」，分层汇总如下：" % goal]
            for lead_name, result in branch_summaries:
                if result.usable:
                    lines.append("- [%s] %s" % (lead_name, result.summary))
                else:
                    lines.append("- [%s] （失败：%s）" % (lead_name, result.reason or "无结论"))
            answer = "\n".join(lines)

        trace.append({"step": "merge", "lead": "lead", "branches_ok": len(usable)})
        return TopologyResult(
            topology=self.name,
            goal=goal,
            answer=answer,
            ok=ok,
            confidence=merge_confidence([r for _, r in branch_summaries]),
            subtasks=len(all_results),
            tokens_used=budget.tokens,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            failures=failures,
            trace=trace,
            results=all_results,
        )

    def _run_branch(
        self,
        branch: Branch,
        subtask: Any,
        budget: BudgetTree,
        trace: List[Dict[str, Any]],
        all_results: List[SubResult],
        goal: str,
    ) -> SubResult:
        """A sub-lead fans out to its workers, then merges *their* results.

        Depth guard: if this sub-lead is already at ``max_depth`` it must do the
        work itself instead of delegating again.
        """
        if subtask.depth >= subtask.max_depth:
            trace.append({"step": "depth_guard", "lead": branch.lead.name,
                          "reason": "at_max_depth", "depth": subtask.depth})
            child_budget = budget.reserve(1)[0]
            result = build_worker(branch.lead)(subtask, child_budget)
            budget.settle(child_budget)
            all_results.append(result)
            return result

        if not branch.workers:
            child_budget = budget.reserve(1)[0]
            result = build_worker(branch.lead)(subtask, child_budget)
            budget.settle(child_budget)
            all_results.append(result)
            return result

        worker_budgets = budget.reserve(len(branch.workers))
        worker_results: List[SubResult] = []
        evidence: List[str] = []
        for role, child_budget in zip(branch.workers, worker_budgets):
            # The worker template should be rendered against the *original*
            # goal.  ``subtask.goal`` is the sub-lead's own rendered sentence
            # ("统筹 ... 的信息收集"), and rendering a template against that
            # stacks the two templates on top of each other.
            task = new_task(
                role.name,
                role.render(goal),
                parent_id=subtask.task_id,
                depth=subtask.depth + 1,
                max_depth=subtask.max_depth,
            )
            result = build_worker(role)(task, child_budget)
            budget.settle(child_budget)
            worker_results.append(result)
            all_results.append(result)
            evidence.extend(result.evidence)
            trace.append({
                "step": "worker",
                "lead": branch.lead.name,
                "role": role.name,
                "depth": task.depth,
                "ok": result.ok,
                "tokens": result.tokens,
            })

        usable = [r for r in worker_results if r.usable]
        if not usable:
            return SubResult.failed(subtask, "all_workers_failed", tokens=budget.tokens)

        # The sub-lead merges *evidence*, not just text: this is what keeps the
        # top level able to trace a claim to its origin.
        summary = "%s：%s" % (
            branch.lead.name,
            "；".join(r.summary for r in usable),
        )
        return SubResult(
            task_id=subtask.task_id,
            role=branch.lead.name,
            ok=True,
            summary=summary,
            evidence=evidence,
            confidence=merge_confidence(worker_results),
            open_questions=[q for r in usable for q in r.open_questions],
            tokens=budget.tokens,
            steps=len(worker_results),
        )
