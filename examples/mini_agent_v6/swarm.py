"""Topology: Swarm / Handoff — decentralised control transfer.

```
  user --> [triage] --handoff--> [refund] --handoff--> [human]
                    \\--handoff--> [tech support] --> answer
```

There is **no supervisor**.  Whichever agent is currently "active" decides
whether to answer or to hand off to a named peer, and the control transfers.
The run ends when an agent answers, or hands off to a terminal agent (usually a
human), or the hop limit is reached.

How Swarm differs from Peer (v4) — the distinction the handbook cares about:

| | Peer | Swarm / Handoff |
| --- | --- | --- |
| Who is active | all participants, taking turns | exactly one at a time |
| Mechanism | conversation | control transfer |
| Ends when | content converges / round cap | an agent answers or reaches terminal |
| Risk | livelock | **handoff loops** (A→B→A→B) |

Two guards that a naive implementation always forgets:

1. **No back-and-forth.** A handoff target that has already been visited is
   rejected, so A→B→A cannot happen.  This turns the classic infinite handoff
   loop into a blocked result with a reason.
2. **Hop limit.** Even without revisits, a chain can be long; ``max_hops`` bounds
   it so latency and cost do not grow without limit.

A handoff to a terminal agent (``human``) is a *successful* outcome, not an
error: the handbook is explicit that escalation is correct behaviour, not
failure.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List

from ..mini_agent_v4.contract import BudgetTree

# An agent decides: either answer, or hand off to a named agent.
Decision = "answer"  # marker for readability


@dataclass
class Handoff:
    to: str
    reason: str

    def to_dict(self) -> Dict[str, str]:
        return {"to": self.to, "reason": self.reason}


@dataclass
class HandoffAgent:
    """One agent in the swarm.

    ``policy(goal, history)`` returns either:
      * ``("answer", text)`` — this agent can answer; the run ends successfully.
      * ``("handoff", HandoffAgent_name, reason)`` — transfer control.
      * ``("human", reason)`` — terminal handoff to a human.
    """

    name: str
    policy: Callable[[str, List["Step"]], tuple]
    terminal: bool = False


@dataclass
class Step:
    agent: str
    action: str  # answer | handoff | human | blocked
    detail: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {"agent": self.agent, "action": self.action, "detail": self.detail}


@dataclass
class Swarm:
    name: str = "swarm"
    agents: Dict[str, HandoffAgent] = field(default_factory=dict)
    entry: str = "triage"
    max_hops: int = 5

    def __post_init__(self) -> None:
        if not self.agents:
            self.agents = default_swarm()

    def run(self, goal: str, *, budget=None) -> Dict[str, object]:
        started = time.monotonic()
        budget = budget or BudgetTree(max_tokens=10_000, max_steps=self.max_hops, label="swarm")
        steps: List[Step] = []
        visited: List[str] = []

        current = self.entry
        while True:
            if len(steps) >= self.max_hops:
                return self._finish(
                    goal, steps, budget, started,
                    action="blocked", detail="超过最大跳数 %d，可能存在移交环。" % self.max_hops,
                )

            agent = self.agents.get(current)
            if agent is None:
                return self._finish(
                    goal, steps, budget, started,
                    action="blocked", detail="未知 Agent：%s" % current,
                )

            # Budget: every agent invocation is a paid call.
            try:
                budget.spend(tokens=500, steps=1)
            except Exception:
                return self._finish(
                    goal, steps, budget, started,
                    action="blocked", detail="预算耗尽，未能完成移交。",
                )

            visited.append(current)
            decision = agent.policy(goal, steps)

            # 1. Answer → success.
            if decision[0] == "answer":
                answer_text = decision[1]
                steps.append(Step(current, "answer", answer_text))
                return self._finish(
                    goal, steps, budget, started,
                    action="answer", detail=answer_text,
                )

            # 2. Terminal handoff to a human → success (escalation is not failure).
            if decision[0] == "human":
                reason = decision[1] if len(decision) > 1 else ""
                steps.append(Step(current, "human", reason))
                return self._finish(
                    goal, steps, budget, started,
                    action="human", detail=reason,
                )

            # 3. Handoff to a peer.
            if decision[0] == "handoff":
                target = decision[1]
                reason = decision[2] if len(decision) > 2 else ""
                steps.append(Step(current, "handoff", "%s→%s：%s" % (current, target, reason)))

                # Guard 1: no revisit.  A→B→A is the canonical handoff loop.
                if target in visited:
                    return self._finish(
                        goal, steps, budget, started,
                        action="blocked",
                        detail="检测到移交环：%s 已经处理过，拒绝再次移交（避免 A→B→A）。"
                        % target,
                    )
                current = target
                continue

            return self._finish(
                goal, steps, budget, started,
                action="blocked", detail="未知决策：%r" % (decision,),
            )

    def _finish(self, goal, steps, budget, started, *, action, detail) -> Dict[str, object]:
        human = [s for s in steps if s.action == "human"]
        if action == "answer":
            answer = detail
            ok = True
        elif action == "human":
            answer = "已移交人工处理（%s）。" % (detail or "需要人工确认")
            ok = True  # escalation is a correct outcome, not a failure
        else:
            answer = "移交未能完成：%s" % detail
            ok = False
        return {
            "topology": "swarm",
            "goal": goal,
            "ok": ok,
            "answer": answer,
            "ended_at": steps[-1].agent if steps else None,
            "steps": [s.to_dict() for s in steps],
            "path": [s.agent for s in steps],
            "escalated": bool(human),
            "tokens_used": budget.tokens,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }


def default_swarm() -> Dict[str, HandoffAgent]:
    """A triage → {refund | tech | human} swarm with one deliberate trap.

    ``refund`` hands back to ``triage`` when the request is ambiguous.  Without
    the revisit guard this loops forever; the guard turns it into a BLOCKED
    result, which is the correct behaviour.
    """

    def triage(goal: str, history: List[Step]):
        if "退款" in goal or "退钱" in goal:
            return ("handoff", "refund", "涉及退款")
        if "报错" in goal or "打不开" in goal:
            return ("handoff", "tech", "技术问题")
        if "投诉" in goal:
            return ("human", "投诉需人工")
        return ("human", "无法归类，转人工")

    def refund(goal: str, history: List[Step]):
        if "订单" in goal or "单号" in goal:
            return ("answer", "已按退款政策处理：%s" % goal)
        # Ambiguous: the natural (wrong) instinct is to ask triage again.
        return ("handoff", "triage", "信息不足，退回分诊")

    def tech(goal: str, history: List[Step]):
        return ("answer", "技术排查完成：%s" % goal)

    return {
        "triage": HandoffAgent("triage", triage),
        "refund": HandoffAgent("refund", refund),
        "tech": HandoffAgent("tech", tech),
        "human": HandoffAgent("human", lambda g, h: ("human", "人工"), terminal=True),
    }
