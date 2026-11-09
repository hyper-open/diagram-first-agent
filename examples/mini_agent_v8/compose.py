"""Harness + Graph: how the two layers compose.

The point of this demo is the **division of labour**, not "two orchestrators":

```
  Graph (this file's Graph)          Harness (mini_agent_v7)
  ─────────────────────────          ──────────────────────
  decides WHICH stage runs next      decides how ONE stage runs:
  (plan -> execute -> approve ->      plan -> execute steps ->
   summarize), with checkpoints,      verify -> retry -> re-plan
   interrupts, resume

  A node's body is *a harness run*.
```

So a graph node is not "an LLM call"; it is "run this harness to completion and
put its result in shared state". The graph never re-implements the loop, and the
harness never knows about stages or approvals.

This mirrors how you would use LangGraph in production: nodes are coarse units
(often an agent run), edges express control flow, the checkpointer enables pause
/ resume. Here the agent run inside a node is our own harness.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..mini_agent.types import Budget
from ..mini_agent_v7.config import HarnessConfig
from ..mini_agent_v7.plan_execute import (
    ExecResult,
    Plan,
    PlanExecuteHarness,
    Step,
    VerifyResult,
    contains_verifier,
    dict_executor,
    list_planner,
)
from .graph import Checkpointer, Graph, GraphInterrupt


# ---------------------------------------------------------------------------
# A "stage" = one harness run, wrapped as a graph node.
# ---------------------------------------------------------------------------


@dataclass
class StageSpec:
    """Everything a node needs to run one harness stage."""

    name: str
    plan: List[Step]
    exec_table: Dict[str, ExecResult]
    verify_needles: List[str] = field(default_factory=list)
    replan_steps: List[Step] = field(default_factory=list)
    requires_approval: bool = False
    approval_prompt: str = ""


def run_stage_as_node(spec: StageSpec, state_key: str):
    """Build a graph node that runs a Plan-Execute harness and stores its result.

    The node returns a partial state update; the graph merges it.  This keeps the
    node free of checkpointing / routing concerns — those belong to the graph.
    """

    def node(state: Dict[str, Any]) -> Dict[str, Any]:
        goal = state.get("goal", "")
        # Budget per stage comes from the graph state if present, else default.
        cfg = HarnessConfig(
            max_steps=state.get("max_steps", 16),
            max_tool_calls=state.get("max_tool_calls", 16),
            max_tokens=state.get("max_tokens", 60_000),
            early_stop=state.get("early_stop", True),
            max_replans=state.get("max_replans", 2),
        )
        harness = PlanExecuteHarness(
            planner=list_planner(spec.plan, also_after_round0=spec.replan_steps or None),
            executor=dict_executor(spec.exec_table),
            verifier=contains_verifier(spec.verify_needles),
            config=cfg,
            budget=Budget(
                max_steps=cfg.max_steps, max_tool_calls=cfg.max_tool_calls,
                max_tokens=cfg.max_tokens,
            ),
        )
        result = harness.run("%s:%s" % (goal, spec.name))

        # Approval gate: if this stage needs a human, halt the graph *after*
        # recording the harness result, so resume can continue past it.
        if spec.requires_approval and not state.get("approved"):
            state.setdefault("approvals_pending", []).append(spec.name)
            raise GraphInterrupt(spec.name, spec.approval_prompt or "需要人工审批")

        return {
            state_key: {
                "status": result.status,
                "output": result.output,
                "steps": result.steps_done,
                "tokens": result.tokens,
            },
            "stage_order": state.get("stage_order", []) + [spec.name],
            "tokens_total": state.get("tokens_total", 0) + result.tokens,
        }

    return node


# ---------------------------------------------------------------------------
# The demo workflow: plan -> execute -> approve -> summarize
# ---------------------------------------------------------------------------


def build_demo_graph() -> Graph:
    """A four-stage graph. Each stage is a harness run; edges are control flow.

    ``approve`` is a gate whose router diverts to ``summarize`` only when the
    human has approved — otherwise the graph interrupts (handled in the node).
    """

    cp = Checkpointer()
    g = Graph(checkpointer=cp)

    # Stage 1: gather facts (policy + order).
    gather = StageSpec(
        name="gather",
        plan=[Step("lookup_policy"), Step("lookup_order")],
        exec_table={
            "lookup_policy": ExecResult(True, "退款政策：7 天内未拆封可退", tokens=120),
            "lookup_order": ExecResult(True, "订单 A1024：已发货，运输中", tokens=120),
        },
        verify_needles=["7 天", "A1024"],
    )

    # Stage 2: draft a reply (depends on stage 1's output).
    def draft_plan_from_state(*_):
        return [Step("draft_reply")]

    draft = StageSpec(
        name="draft",
        plan=[Step("draft_reply")],
        exec_table={
            "draft_reply": ExecResult(True, "答复：订单可退，需提交审批", tokens=150),
        },
        verify_needles=["可退"],
        requires_approval=False,
    )

    # Stage 3: approval gate (interrupts unless already approved).
    approve = StageSpec(
        name="approve",
        plan=[Step("check_approval")],
        exec_table={"check_approval": ExecResult(True, "审批通过", tokens=20)},
        verify_needles=["审批通过"],
        requires_approval=True,
        approval_prompt="退款金额 58 元，是否批准？",
    )

    # Stage 4: summarize (terminal).
    summarize = StageSpec(
        name="summarize",
        plan=[Step("finalize")],
        exec_table={"finalize": ExecResult(True, "已生成最终答复并归档", tokens=80)},
        verify_needles=["最终"],
    )

    g.add_node("gather", run_stage_as_node(gather, "gather_result"))
    g.add_node("draft", run_stage_as_node(draft, "draft_result"))
    g.add_node("approve", run_stage_as_node(approve, "approve_result"))
    g.add_node("summarize", run_stage_as_node(summarize, "summarize_result"))

    g.set_entry("gather")
    g.add_edge("gather", "draft")
    g.add_edge("draft", "approve")
    g.add_edge("approve", "summarize")   # terminal

    return g


def run_to_approval(goal: str) -> Dict[str, Any]:
    """First pass: run until the approval gate interrupts."""
    g = build_demo_graph()
    result = g.run(
        {"goal": goal, "approved": False},
        max_steps=10,
    )
    return {"graph": g, "result": result}


def resume_after_approval(goal: str) -> Dict[str, Any]:
    """Full run where the human approved at the gate (no interrupt)."""
    g = build_demo_graph()
    result = g.run(
        {"goal": goal, "approved": True},
        max_steps=10,
    )
    return {"graph": g, "result": result}
