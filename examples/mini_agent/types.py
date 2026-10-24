"""Core data types for the mini agent.

Only the standard library is used, so the examples run anywhere Python 3.8+
is available.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCall:
    """A structured request from the model to run a tool."""

    name: str
    arguments: Dict[str, Any]
    id: str = ""

    def signature(self) -> str:
        """Stable signature used for repeat/stall detection."""
        import json

        return self.name + ":" + json.dumps(
            self.arguments, sort_keys=True, ensure_ascii=False, default=str
        )


@dataclass
class Decision:
    """The model's decision for one step.

    A decision either contains tool calls (act) or a final answer (stop).
    A model may technically return both; the loop treats tool calls as the
    action and ignores content until the next step.
    """

    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    final: bool = False
    usage: Dict[str, int] = field(default_factory=dict)

    @property
    def has_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class ToolResult:
    """Normalized tool result, safe to put in the model context."""

    ok: bool
    data: Any = None
    error: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        if self.ok:
            return {"ok": True, "data": self.data}
        return {"ok": False, "error": self.error or {"type": "internal"}}

    @classmethod
    def success(cls, data: Any) -> "ToolResult":
        return cls(ok=True, data=data)

    @classmethod
    def failure(
        cls,
        error_type: str,
        message: str,
        retryable: bool = False,
        hint: str = "",
        **extra: Any,
    ) -> "ToolResult":
        error = {
            "type": error_type,
            "message": message,
            "retryable": retryable,
            "hint": hint,
        }
        error.update(extra)
        return cls(ok=False, error=error)


class BudgetExceeded(RuntimeError):
    """Raised when a hard budget limit is crossed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Budget:
    """Hard limits for a single task.

    Budget exhaustion is a normal terminal state, not an internal error.  The
    caller receives the partial result plus the reason.
    """

    max_steps: int = 8
    max_tool_calls: int = 12
    max_seconds: float = 60.0
    max_tokens: int = 60_000
    steps: int = 0
    tool_calls: int = 0
    tokens: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def start_step(self) -> None:
        self.check_time()
        if self.steps >= self.max_steps:
            raise BudgetExceeded("step_limit")
        self.steps += 1

    def spend_tool_call(self) -> None:
        if self.tool_calls >= self.max_tool_calls:
            raise BudgetExceeded("tool_call_limit")
        self.tool_calls += 1

    def spend_tokens(self, usage: Dict[str, int]) -> None:
        total = int(usage.get("total_tokens", 0) or 0)
        if total == 0:
            total = int(usage.get("input_tokens", 0) or 0) + int(
                usage.get("output_tokens", 0) or 0
            )
        self.tokens += total
        if self.tokens > self.max_tokens:
            raise BudgetExceeded("token_limit")

    def check_time(self) -> None:
        if time.monotonic() - self.started_at > self.max_seconds:
            raise BudgetExceeded("time_limit")

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at


@dataclass
class TraceStep:
    step: int
    decision: Decision
    results: List[Dict[str, Any]] = field(default_factory=list)
    latency_ms: int = 0
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "content": self.decision.content,
            "final": self.decision.final,
            "tool_calls": [
                {"name": call.name, "arguments": call.arguments, "id": call.id}
                for call in self.decision.tool_calls
            ],
            "results": self.results,
            "usage": self.decision.usage,
            "latency_ms": self.latency_ms,
            "note": self.note,
        }


@dataclass
class Trace:
    task: str
    steps: List[TraceStep] = field(default_factory=list)
    status: str = "running"
    output: str = ""
    error: str = ""

    def add(self, step: TraceStep) -> None:
        self.steps.append(step)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "steps": [s.to_dict() for s in self.steps],
            "total_steps": len(self.steps),
        }
