"""Topology 1b: Peer.

```
  +---------+      message      +---------+
  | Agent A | <---------------> | Agent B |
  +---------+                   +---------+
       \___________________________/
            shared conversation
```

Control flow: agents take turns speaking to each other as equals.  There is no
leader deciding who speaks next — the *conversation* is the control flow.

This is the topology most likely to be adopted for the wrong reason ("it feels
more collaborative").  The handbook is blunt about it: peers are flexible but

* **drift** — nobody owns the objective, so the topic wanders;
* **unclear responsibility** — when the result is wrong, whose fault is it?
* **livelock** — two agents can keep handing the same question back.

So this implementation adds two mechanisms that everyone forgets to add:

1. a **turn cap** (``max_rounds``), because "until it converges" is not a
   termination condition;
2. a **termination check on substance**, not on politeness — the conversation
   stops when a round produces no new evidence or when both sides restate
   agreement without adding anything.

The deliverable is still a ``TopologyResult`` produced by choosing the *last
substantive* message, not "whatever was said last".
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
    collect_failures,
    merge_confidence,
    new_task,
)


@dataclass
class PeerTopology:
    name: str = "peer"
    peers: List[AgentRole] = field(default_factory=list)
    max_rounds: int = 3  # hard cap: convergence is not a termination condition

    def __post_init__(self) -> None:
        if not self.peers:
            self.peers = [
                AgentRole("draft", "先给出对 {goal} 的初稿", confidence=0.6),
                AgentRole("review", "指出 {goal} 初稿里的风险与遗漏", confidence=0.65),
            ]

    def run(self, goal: str, *, budget: Optional[BudgetTree] = None) -> TopologyResult:
        started = time.monotonic()
        budget = budget or BudgetTree(max_tokens=24_000, max_steps=10)
        trace: List[Dict[str, Any]] = []
        results: List[SubResult] = []
        conversation: List[SubResult] = []
        seen_evidence: set = set()
        stagnant_rounds = 0

        for round_index in range(self.max_rounds):
            round_results: List[SubResult] = []
            for role in self.peers:
                task = new_task(
                    role.name,
                    role.render(goal),
                    parent_id="peer-round-%d" % round_index,
                    depth=1,
                    constraints=["回应对方观点", "不要重复已说过的话"],
                )
                reserved = budget.reserve(1)
                if not reserved:
                    trace.append({"step": "stop", "reason": "budget", "round": round_index})
                    break
                child_budget = reserved[0]
                # A peer sees the conversation so far — that is what makes it a
                # peer and not a supervisor's worker.  The context goes into the
                # task packet's constraints, NOT into the goal string: appending
                # it to the goal makes each turn nest the previous answer inside
                # the next prompt (a feedback loop that explodes token count).
                context = " | ".join(r.summary for r in conversation[-2:])
                if context:
                    task.constraints = task.constraints + ["先前发言：%s" % context]
                from .contract import build_worker  # local import keeps module small

                result = build_worker(role)(task, child_budget)
                budget.settle(child_budget)
                round_results.append(result)
                results.append(result)
                conversation.append(result)
                trace.append({
                    "step": "turn",
                    "round": round_index,
                    "role": role.name,
                    "ok": result.ok,
                    "tokens": result.tokens,
                })

            # Termination on substance: did this round add new evidence?
            new_evidence = {e for r in round_results for e in r.evidence} - seen_evidence
            seen_evidence |= {e for r in round_results for e in r.evidence}
            if not new_evidence:
                stagnant_rounds += 1
                trace.append({"step": "stop", "reason": "no_new_evidence", "round": round_index})
                break
            if round_index > 0 and self._restating(round_results, conversation[:-len(round_results)]):
                trace.append({"step": "stop", "reason": "restating_agreement", "round": round_index})
                break
            if budget.remaining_tokens() <= 0 or budget.steps >= budget.max_steps:
                trace.append({"step": "stop", "reason": "budget", "round": round_index})
                break

        # Pick the last substantive turn, not simply the last message.
        substantive = [r for r in conversation if r.usable]
        answer_result = substantive[-1] if substantive else None
        if answer_result is None:
            answer = "对等协商未能产出可用结论（可能陷入互相等待）。"
            ok = False
        else:
            answer = answer_result.summary
            ok = True

        return TopologyResult(
            topology=self.name,
            goal=goal,
            answer=answer,
            ok=ok,
            confidence=merge_confidence(results),
            subtasks=len(results),
            tokens_used=budget.tokens,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            failures=collect_failures(results),
            trace=trace,
            results=results,
        )

    @staticmethod
    def _restating(current: List[SubResult], previous: List[SubResult]) -> bool:
        """Detect livelock: this round's summaries equal the previous round's."""
        if not previous or len(current) != len(previous):
            return False
        return [r.summary for r in current] == [r.summary for r in previous]
