"""Shared contract for every multi-agent topology.

Read this file before the four topology files.  The topologies differ only in
*control flow*; everything in this module is what they have in common, and
getting it right is what keeps a multi-agent system debuggable.

Three ideas:

1. **SubTask / SubResult** — the task packet and the result envelope.  A parent
   only ever sees ``SubResult``; it never sees the child's transcript.
2. **BudgetTree** — budget is *inherited and split*, not shared freely.  A
   sub-agent that can spend the parent's whole budget turns one runaway loop
   into N.
3. **AgentRole** — a worker is a function over a goal, not a personality
   prompt.  It returns ``SubResult`` even when it fails, so failures can be
   merged instead of crashing the supervisor.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

# A worker is any callable that turns a goal (+ its own budget) into a result.
WorkerFn = Callable[["SubTask", "BudgetTree"], "SubResult"]


# ---------------------------------------------------------------------------
# Task packet and result envelope
# ---------------------------------------------------------------------------


@dataclass
class SubTask:
    """The only thing a parent hands to a child.  Mirrors the handbook's
    ``agent_task_packet.json``: task, constraints, success criteria, budget."""

    task_id: str
    role: str
    goal: str
    constraints: List[str] = field(default_factory=list)
    success_criteria: str = ""
    parent_id: str = ""
    depth: int = 0
    max_depth: int = 2

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "role": self.role,
            "goal": self.goal,
            "constraints": list(self.constraints),
            "success_criteria": self.success_criteria,
            "parent_id": self.parent_id,
            "depth": self.depth,
        }


@dataclass
class SubResult:
    """The result envelope.  A *failed* sub-agent also returns one of these.

    ``ok=False`` with a ``reason`` is a first-class outcome: the supervisor
    needs to distinguish "this worker has no answer" from "the process died".
    """

    task_id: str
    role: str
    ok: bool
    summary: str = ""
    evidence: List[str] = field(default_factory=list)
    confidence: float = 0.0
    open_questions: List[str] = field(default_factory=list)
    reason: str = ""
    steps: int = 0
    tokens: int = 0
    elapsed_ms: int = 0
    from_cache: bool = False

    @property
    def usable(self) -> bool:
        """Can a parent build on this?  High confidence alone is not enough:
        a worker that says nothing but claims certainty is not usable."""
        return self.ok and bool(self.summary.strip()) and self.confidence > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "role": self.role,
            "ok": self.ok,
            "summary": self.summary,
            "evidence": list(self.evidence),
            "confidence": round(self.confidence, 3),
            "open_questions": list(self.open_questions),
            "reason": self.reason,
            "steps": self.steps,
            "tokens": self.tokens,
            "elapsed_ms": self.elapsed_ms,
            "usable": self.usable,
        }

    @classmethod
    def failed(cls, task: SubTask, reason: str, *, tokens: int = 0, steps: int = 0) -> "SubResult":
        return cls(task_id=task.task_id, role=task.role, ok=False,
                   reason=reason, tokens=tokens, steps=steps)


@dataclass
class TopologyResult:
    """Uniform output so all four topologies can be compared and tested."""

    topology: str
    goal: str
    answer: str
    ok: bool
    confidence: float
    subtasks: int
    tokens_used: int
    elapsed_ms: int
    failures: List[str] = field(default_factory=list)
    trace: List[Dict[str, Any]] = field(default_factory=list)
    results: List[SubResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "topology": self.topology,
            "goal": self.goal,
            "answer": self.answer,
            "ok": self.ok,
            "confidence": round(self.confidence, 3),
            "subtasks": self.subtasks,
            "tokens_used": self.tokens_used,
            "elapsed_ms": self.elapsed_ms,
            "failures": list(self.failures),
            "results": [r.to_dict() for r in self.results],
            "trace": list(self.trace),
        }


# ---------------------------------------------------------------------------
# Budget inheritance
# ---------------------------------------------------------------------------


class BudgetExhausted(RuntimeError):
    """Raised when a sub-agent tries to spend past its share."""


@dataclass
class BudgetTree:
    """A budget that is *divided*, not merely shared.

    The invariant: **the sum of what children spend never exceeds the parent's
    cap.**  Getting this wrong is the most common multi-agent cost bug, so the
    mechanism is explicit:

    * ``reserve(n)`` carves out n child budgets whose total <= remaining;
    * a child that cannot afford even one call is not created (the parent keeps
      the budget and reports "insufficient budget" at its own level);
    * ``settle(child)`` refunds what the child did not spend.

    This is a toy version of what a real system does with a shared quota ledger,
    but the invariant is the same one you need in production.
    """

    max_tokens: int = 60_000
    max_steps: int = 20
    tokens: int = 0
    steps: int = 0
    label: str = "root"
    min_call_tokens: int = 800  # cost of the smallest meaningful unit of work

    def remaining_tokens(self) -> int:
        return max(0, self.max_tokens - self.tokens)

    def remaining_steps(self) -> int:
        return max(0, self.max_steps - self.steps)

    def spend(self, *, tokens: int = 0, steps: int = 1) -> None:
        if self.tokens + tokens > self.max_tokens:
            raise BudgetExhausted(
                "%s: token limit %d exceeded (used %d + %d)"
                % (self.label, self.max_tokens, self.tokens, tokens)
            )
        if self.steps + steps > self.max_steps:
            raise BudgetExhausted(
                "%s: step limit %d exceeded (used %d + %d)"
                % (self.label, self.max_steps, self.steps, steps)
            )
        self.tokens += tokens
        self.steps += steps

    def reserve(self, n: int) -> List["BudgetTree"]:
        """Carve out up to ``n`` child budgets from what is left.

        Returns fewer than ``n`` if there is not enough budget to give every
        child even one call.  Returning fewer is the *correct* behaviour: it
        surfaces "we cannot afford that many workers" at the planner, instead of
        spawning workers that all fail with a token error.
        """
        if n <= 0:
            return []
        affordable = min(n, self.remaining_tokens() // self.min_call_tokens)
        affordable = min(affordable, max(1, self.remaining_steps()))
        if affordable <= 0:
            return []
        share = self.remaining_tokens() // affordable
        step_share = max(1, self.remaining_steps() // affordable)
        children = [
            BudgetTree(
                max_tokens=share,
                max_steps=step_share,
                label="%s/%d" % (self.label, i + 1),
                min_call_tokens=self.min_call_tokens,
            )
            for i in range(affordable)
        ]
        # Immediately deduct the reserved capacity; settling refunds the rest.
        self.tokens += share * affordable
        self.steps += 1
        return children

    def settle(self, child: "BudgetTree") -> None:
        """Refund whatever the child reserved but did not spend."""
        refund = max(0, child.max_tokens - child.tokens)
        self.tokens = max(0, self.tokens - refund)


# ---------------------------------------------------------------------------
# Roles and the default worker
# ---------------------------------------------------------------------------


@dataclass
class AgentRole:
    """A worker's identity.  Deliberately has no "personality" field: the
    handbook's point is that a contract beats a role prompt."""

    name: str
    goal_template: str
    confidence: float = 0.7
    fail_above: Optional[int] = None  # for tests: fail after N calls
    fail_reason: str = "no answer"
    _calls: int = field(default=0, init=False, repr=False)

    def render(self, goal: str) -> str:
        return self.goal_template.format(goal=goal)


def build_worker(role: AgentRole, *, tokens_per_call: int = 800) -> WorkerFn:
    """A deterministic stand-in worker.

    It produces a ``SubResult`` from the role's template.  ``tokens_per_call``
    is charged to the child's budget so budget accounting is exercised.  A
    role can be told to fail (``fail_above``) to simulate a broken sub-agent —
    which is exactly what the error-propagation examples need.
    """

    def worker(task: SubTask, budget: BudgetTree) -> SubResult:
        started = time.monotonic()
        role._calls += 1
        try:
            budget.spend(tokens=tokens_per_call, steps=1)
        except BudgetExhausted as exc:
            return SubResult(
                task_id=task.task_id, role=task.role, ok=False,
                reason="budget_exhausted: %s" % exc,
                tokens=budget.tokens,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        if role.fail_above is not None and role._calls > role.fail_above:
            return SubResult(
                task_id=task.task_id, role=task.role, ok=False,
                reason=role.fail_reason,
                tokens=budget.tokens, steps=1,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        # A "worker" produces a conclusion plus the evidence it based it on.
        # ``task.goal`` is already rendered by the caller; rendering again here
        # would nest the template ("检索与 检索与 ... 相关的事实") — a real bug we
        # hit while writing the examples.
        summary = task.goal
        return SubResult(
            task_id=task.task_id,
            role=task.role,
            ok=True,
            summary=summary,
            evidence=["%s::%s" % (role.name, task.task_id)],
            confidence=role.confidence,
            open_questions=[],
            tokens=budget.tokens,
            steps=1,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    return worker


def new_task(
    role: str,
    goal: str,
    *,
    parent_id: str = "",
    depth: int = 0,
    max_depth: int = 2,
    constraints: Optional[Sequence[str]] = None,
    success_criteria: str = "",
) -> SubTask:
    return SubTask(
        task_id="sub_" + uuid.uuid4().hex[:8],
        role=role,
        goal=goal,
        constraints=list(constraints or []),
        success_criteria=success_criteria,
        parent_id=parent_id,
        depth=depth,
        max_depth=max_depth,
    )


# ---------------------------------------------------------------------------
# Merging helpers shared by topologies
# ---------------------------------------------------------------------------


def merge_confidence(results: Sequence[SubResult]) -> float:
    """Confidence of a merged answer.

    Deliberately the *minimum* of the usable results, not the average: a merge
    is only as trustworthy as its weakest accepted input.  Averaging would let
    three confident workers hide one that had no idea.
    """
    usable = [r for r in results if r.usable]
    if not usable:
        return 0.0
    return min(r.confidence for r in usable)


def collect_failures(results: Sequence[SubResult]) -> List[str]:
    return [
        "%s/%s: %s" % (r.role, r.task_id, r.reason or "unknown")
        for r in results
        if not r.ok
    ]
