"""A Plan-Execute agent written as a harness.

This is deliberately a *harness*, not "a plan-execute demo": the point is that
the loop, budget, termination, tracing and retry live in the harness, while the
two things that vary — **planning** and **executing one step** — are injected
callables.

Shape (same skeleton as ReAct, different inner steps):

    plan   ->  [ execute step  ->  verify  ]*  ->  finish / re-plan

```
                 ┌───────────────────────────────┐
   goal ────────▶│  PLANNER  (injected)          │──▶ Plan = [Step, Step, ...]
                 └───────────────────────────────┘
   plan ────────▶┌───────────────────────────────┐
                 │  HARNESS  (this file)         │
                 │  for step in plan:            │  ← budget / retry / trace
                 │      result = EXECUTOR(step)  │──▶ ExecResult
                 │      if verify(result) fail:  │
                 │          re-plan (bounded)    │
                 └───────────────────────────────┘
                                  │
                                  ▼
                         RunResult(status, output, trace)
```

Why this is a harness and not just a loop:

1. **Budget** — every plan/execute/verify call is charged; over budget is a
   normal terminal state (`partial`), never a crash.
2. **Termination** — three independent stops: plan exhausted, verification
   passed, or re-plan rounds hit the cap.
3. **Trace** — every step records what was planned, executed, charged, and why
   it stopped, so a failure can be replayed.
4. **Retry policy** — a step that fails *retryably* is retried a bounded number
   of times; a non-retryable failure short-circuits to `failed`.
5. **Isolation of concerns** — the planner may be a hand-written list (tests)
   or a model call (production); the executor may be a tool, a function, or an
   LLM. Neither knows about budget or tracing.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..mini_agent.types import Budget, BudgetExceeded
from .config import HarnessConfig


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------


@dataclass
class Step:
    """One unit of the plan.  Kept dumb on purpose: the planner decides content,
    the executor decides behaviour, the harness decides everything around it."""

    name: str
    input: str = ""
    retryable: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "input": self.input, "retryable": self.retryable}


@dataclass
class ExecResult:
    ok: bool
    output: str = ""
    error: str = ""
    retryable: bool = False
    tokens: int = 0

    @classmethod
    def failure(cls, error: str, *, retryable: bool = False, tokens: int = 0) -> "ExecResult":
        """Explicit failure constructor.

        ``ExecResult(False, "x")`` puts "x" in *output*, not *error* — an easy
        mistake.  Use this when the point is the error message.
        """
        return cls(ok=False, error=error, retryable=retryable, tokens=tokens)


@dataclass
class VerifyResult:
    passed: bool
    reason: str = ""


@dataclass
class Plan:
    steps: List[Step] = field(default_factory=list)
    reason: str = ""


# Injected callables.  This is the whole extension surface of the harness.
Planner = Callable[[str, List[Step], int], Plan]        # (goal, done, round) -> Plan
Executor = Callable[[Step], ExecResult]                 # (step) -> ExecResult
Verifier = Callable[[str, List[Step]], VerifyResult]    # (draft, done) -> VerifyResult


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    status: str  # success | partial | failed | blocked
    output: str
    steps_done: List[str]
    rounds: int
    tokens: int
    elapsed_ms: int
    trace: List[Dict[str, Any]]
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "output": self.output,
            "steps_done": list(self.steps_done),
            "rounds": self.rounds,
            "tokens": self.tokens,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "trace": self.trace,
        }


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


class PlanExecuteHarness:
    """Runs a plan to completion with budget, retries, tracing and re-planning.

    The harness never *decides content*.  It decides:
      * whether there is budget left for one more call,
      * whether a failed step may be retried,
      * whether to re-plan or stop,
      * what to record.
    """

    def __init__(
        self,
        planner: Planner,
        executor: Executor,
        verifier: Optional[Verifier] = None,
        *,
        config: Optional[HarnessConfig] = None,
        budget: Optional[Budget] = None,
    ):
        # All control knobs come from config — one place to read, no magic
        # numbers scattered in the loop.  Callers may still inject a pre-built
        # Budget (e.g. a shared quota ledger); otherwise it is built from config.
        self.config = config or HarnessConfig()
        self.config.validate()
        self.planner = planner
        self.executor = executor
        self.verifier = verifier or (lambda draft, done: VerifyResult(True))
        c = self.config
        self.budget = budget or Budget(
            max_steps=c.max_steps,
            max_tool_calls=c.max_tool_calls,
            max_tokens=c.max_tokens,
            max_seconds=c.max_seconds,
        )
        # Mirror config onto attributes the loop already uses.
        self.max_replans = c.max_replans
        self.max_attempts_per_step = c.max_attempts_per_step
        self.early_stop = c.early_stop
        self.trace_enabled = c.trace_enabled

    # ------------------------------------------------------------------

    def run(self, goal: str) -> RunResult:
        started = time.monotonic()
        trace: List[Dict[str, Any]] = []
        done: List[Step] = []
        outputs: List[str] = []
        rounds = 0
        reparse_feedback = ""

        self._reset_budget()

        while True:
            rounds += 1
            # 1. Plan (charged as a model call in production; cheap here).
            try:
                self.budget.spend_tokens({"input_tokens": max(1, len(goal) // 4)})
                self.budget.start_step()
            except BudgetExceeded as exc:
                return self._finish("partial", outputs, done, rounds, trace, started,
                                    error=str(exc))
            plan = self.planner(goal, done, rounds)
            self._trace(trace, {"phase": "plan", "round": rounds, "steps": [s.name for s in plan.steps],
                          "reason": plan.reason})
            if not plan.steps:
                # Nothing more to do: verify what we have.
                result = self._verify(goal, outputs, done, trace)
                return self._finish("success" if result.passed else "partial",
                                    outputs, done, rounds, trace, started,
                                    error="" if result.passed else result.reason)

            # 2. Execute each planned step.
            for step in plan.steps:
                outcome = self._execute_with_retry(step, trace)
                if outcome is None:  # budget exhausted
                    return self._finish("partial", outputs, done, rounds, trace, started,
                                        error="budget_exhausted")
                result, status = outcome
                if not result.ok:
                    if status == "failed":
                        # Non-retryable: stop honestly, keep what we have.
                        return self._finish("failed", outputs, done, rounds, trace, started,
                                            error="%s: %s" % (step.name, result.error))
                    # Retryable and out of attempts -> partial.
                    return self._finish("partial", outputs, done, rounds, trace, started,
                                        error="step_failed: %s: %s" % (step.name, result.error))
                done.append(step)
                outputs.append("%s→%s" % (step.name, result.output))

                # 3. Cheap check after each step: if the verifier already passes,
                #    we can stop early instead of running the rest of the plan.
                verify = self._verify(goal, outputs, done, trace, record=False)
                if verify.passed and self.early_stop:
                    self._trace(trace, {"phase": "early_stop", "after": step.name,
                                  "reason": verify.reason})
                    return self._finish("success", outputs, done, rounds, trace, started)

            # 4. End of plan: verify.  Fail -> re-plan (bounded) or stop.
            verify = self._verify(goal, outputs, done, trace)
            if verify.passed:
                return self._finish("success", outputs, done, rounds, trace, started)
            reparse_feedback = verify.reason
            if rounds > self.max_replans:
                return self._finish("partial", outputs, done, rounds, trace, started,
                                    error="verify_failed: %s" % reparse_feedback)
            self._trace(trace, {"phase": "replan", "round": rounds, "reason": reparse_feedback})
            # Give the planner a chance to add corrective steps next round.

    # ------------------------------------------------------------------
    # Internals: retry, verify, finish
    # ------------------------------------------------------------------

    def _execute_with_retry(self, step: Step, trace: List[Dict[str, Any]]):
        """Execute one step, retrying only *retryable* failures, bounded.

        Returns (ExecResult, "ok"|"failed"|"exhausted") or None if budget is gone.
        """
        last: Optional[ExecResult] = None
        for attempt in range(1, self.max_attempts_per_step + 1):
            try:
                # A step costs one budget "step" AND one tool call.  Counting
                # both means `max_steps` bounds real work, not just plan/verify
                # turns — otherwise a long plan could exceed the obvious limit.
                self.budget.start_step()
                self.budget.spend_tool_call()
            except BudgetExceeded:
                return None
            result = self.executor(step)
            self._charge_tokens(result.tokens)
            self._trace(trace, {
                "phase": "execute", "step": step.name, "attempt": attempt,
                "ok": result.ok, "retryable": result.retryable,
                "error": result.error, "tokens": result.tokens,
            })
            if result.ok:
                return result, "ok"
            last = result
            # Only retry if BOTH the step and the result say retryable.
            can_retry = result.retryable or step.retryable
            if not can_retry:
                return result, "failed"
            if attempt == self.max_attempts_per_step:
                break
        return last or ExecResult(False, error="no_attempt"), "exhausted"

    def _verify(self, goal, outputs, done, trace, record=True) -> VerifyResult:
        try:
            self.budget.start_step()
        except BudgetExceeded:
            # Do NOT treat "no budget to verify" as "verified".  That would
            # report an unfinished run as success — the exact silent failure
            # this harness exists to prevent.  Return a *failed* verdict so the
            # caller ends as PARTIAL with a truthful reason.
            return VerifyResult(False, "budget exhausted before verification")
        draft = "\n".join(outputs)
        verdict = self.verifier(draft, done)
        if record:
            self._trace(trace, {"phase": "verify", "passed": verdict.passed,
                          "reason": verdict.reason})
        return verdict

    def _trace(self, trace: List[Dict[str, Any]], event: Dict[str, Any]) -> None:
        """Single write path for trace events, gated by config.

        Every phase goes through here so ``trace_enabled`` is honoured in one
        place rather than sprinkled as ``if`` checks around the loop.
        """
        if self.trace_enabled:
            trace.append(event)

    def _charge_tokens(self, tokens: int) -> None:
        if tokens:
            try:
                self.budget.spend_tokens({"total_tokens": tokens})
            except BudgetExceeded:
                pass  # budget check already gates further steps

    def _reset_budget(self) -> None:
        self.budget.steps = 0
        self.budget.tool_calls = 0
        self.budget.tokens = 0
        self.budget.started_at = time.monotonic()

    def _finish(self, status, outputs, done, rounds, trace, started, error="") -> RunResult:
        return RunResult(
            status=status,
            output="\n".join(outputs) if outputs else "",
            steps_done=[s.name for s in done],
            rounds=rounds,
            tokens=self.budget.tokens,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            trace=trace,
            error=error,
        )


# ---------------------------------------------------------------------------
# Ready-made planners / executors (examples + tests)
# ---------------------------------------------------------------------------


def list_planner(steps: List[Step], also_after_round0: Optional[List[Step]] = None) -> Planner:
    """A deterministic planner: returns ``steps`` on the first round, then
    ``also_after_round0`` on a re-plan. This is the "encoded plan" mode."""

    def planner(goal: str, done: List[Step], round_index: int) -> Plan:
        if round_index == 1:
            return Plan(list(steps), "initial")
        if also_after_round0:
            return Plan(list(also_after_round0), "replan")
        return Plan([], "nothing_more")

    return planner


def dict_executor(table: Dict[str, ExecResult], default: Optional[ExecResult] = None) -> Executor:
    """Execute by looking up a step name in a table.  Deterministic for tests."""

    def executor(step: Step) -> ExecResult:
        if step.name in table:
            return table[step.name]
        return default or ExecResult(False, error="unknown step: %s" % step.name)

    return executor


def contains_verifier(needles: List[str]) -> Verifier:
    """Pass only if every needle appears in the draft."""

    def verifier(draft: str, done: List[Step]) -> VerifyResult:
        missing = [n for n in needles if n not in draft]
        if missing:
            return VerifyResult(False, "missing: " + ", ".join(missing))
        return VerifyResult(True, "all required content present")

    return verifier
