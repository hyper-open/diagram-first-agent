"""Topology 1d: Debate.

```
  Proposer ──arguemnt──┐
                       ├──> Judge ──> verdict (+ why)
  Critic  ──rebuttal───┘
```

Control flow: two agents argue opposite sides; a judge decides.  Used for
*judgement* tasks where a single pass is unreliable: policy edge cases, risk
triage, "is this answer actually correct?".

The handbook warns this is expensive and prone to **false consensus**.  Two
mechanisms here target that directly:

1. **The judge must cite which argument won and why.** A verdict with no
   rationale cannot be reviewed, so it is treated as a failure.
2. **Disagreement is a valid, reported outcome.** If the critic's objection
   reduces the proposer's confidence below a bar, the result is ``ok=False``
   with the objection attached — better than a confident wrong answer.

Cost: at least three model calls (proposer, critic, judge), usually more when
there are multiple rounds.  Do not debate every request; debate the ones where
the cost of being wrong is high.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .contract import (
    AgentRole,
    BudgetTree,
    SubResult,
    TopologyResult,
    build_worker,
    collect_failures,
    new_task,
)


@dataclass
class DebateTopology:
    name: str = "debate"
    proposer: AgentRole = field(
        default_factory=lambda: AgentRole("proposer", "主张：对 {goal} 给出一个明确结论", confidence=0.75)
    )
    critic: AgentRole = field(
        default_factory=lambda: AgentRole("critic", "反驳：找出对 {goal} 结论的反例与风险", confidence=0.7)
    )
    judge: AgentRole = field(
        default_factory=lambda: AgentRole("judge", "裁决 {goal}：采纳哪一方，并说明理由", confidence=0.8)
    )
    rounds: int = 1
    confidence_floor: float = 0.5  # below this, report disagreement instead of an answer

    def run(self, goal: str, *, budget: Optional[BudgetTree] = None) -> TopologyResult:
        started = time.monotonic()
        budget = budget or BudgetTree(max_tokens=30_000, max_steps=12)
        trace: List[Dict[str, Any]] = []
        results: List[SubResult] = []

        proposer_result: Optional[SubResult] = None
        critic_result: Optional[SubResult] = None
        for round_index in range(self.rounds):
            p_budget = budget.reserve(1)[0]
            p_task = new_task(self.proposer.name, self.proposer.render(goal),
                              parent_id="judge", depth=1,
                              constraints=["给出结论", "给出依据"])
            proposer_result = build_worker(self.proposer)(p_task, p_budget)
            budget.settle(p_budget)
            results.append(proposer_result)
            trace.append({"step": "propose", "round": round_index,
                          "ok": proposer_result.ok, "tokens": proposer_result.tokens})

            c_budget = budget.reserve(1)[0]
            c_task = new_task(
                self.critic.name,
                self.critic.render(goal) + "（针对主张：%s）" % (proposer_result.summary or "无"),
                parent_id="judge", depth=1,
                constraints=["必须具体", "指出可验证的风险"],
            )
            critic_result = build_worker(self.critic)(c_task, c_budget)
            budget.settle(c_budget)
            results.append(critic_result)
            trace.append({"step": "critique", "round": round_index,
                          "ok": critic_result.ok, "tokens": critic_result.tokens})

        # Judge decides, but must give a rationale and an explicit choice.
        j_budget = budget.reserve(1)[0]
        j_task = new_task(
            self.judge.name,
            self.judge.render(goal),
            parent_id="judge", depth=1,
            success_criteria="必须说明采纳哪一方以及理由",
        )
        judge_result = build_worker(self.judge)(j_task, j_budget)
        budget.settle(j_budget)
        results.append(judge_result)
        trace.append({"step": "judge", "ok": judge_result.ok, "tokens": judge_result.tokens})

        failures = collect_failures(results)

        # A judge verdict without a rationale is not reviewable → failure.
        if not judge_result.ok or not judge_result.summary.strip():
            return self._finish(
                goal, False, "裁决失败：未能给出可复核的裁决。", 0.0,
                results, failures, budget, trace, started,
            )

        # Disagreement handling: if the critic's objection is stronger than the
        # proposer's confidence, do not pretend consensus.
        debate_confidence = min(
            proposer_result.confidence if proposer_result and proposer_result.usable else 0.0,
            critic_result.confidence if critic_result and critic_result.usable else 0.0,
            judge_result.confidence,
        )
        if debate_confidence < self.confidence_floor:
            objection = (critic_result.summary if critic_result else "") or "反方提出未解决异议"
            return self._finish(
                goal, False,
                "裁决：存在未解决的异议，不给出结论。异议：%s" % objection,
                debate_confidence, results, failures, budget, trace, started,
            )

        answer = "%s（裁决：%s）" % (judge_result.summary, judge_result.role)
        return self._finish(
            goal, True, answer, debate_confidence,
            results, failures, budget, trace, started,
        )

    @staticmethod
    def _finish(goal, ok, answer, confidence, results, failures, budget, trace, started) -> TopologyResult:
        return TopologyResult(
            topology="debate",
            goal=goal,
            answer=answer,
            ok=ok,
            confidence=confidence,
            subtasks=len(results),
            tokens_used=budget.tokens,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            failures=failures,
            trace=trace,
            results=results,
        )
