"""v9: after intent routing, decide *which topology* should execute.

The chain this demo makes explicit:

    user text (possibly multi-intent)
        -> intent routing          (reuse v2 route_multi)
        -> topology selection      (this file)   <-- the point of the demo
        -> execute with that topology

Topology selection is a decision, not a mode switch.  We choose based on two
things derived from the route:

  * **How many distinct intents?**  one vs several.
  * **Are the intents independent, dependent, or conflicting?**
      independent -> parallel fan-out (run all, concatenate)
      dependent   -> chaining / supervisor (ordered, needs upstream result)
      conflicting -> supervisor with adjudication (must reconcile, not concat)
  * **Single but complex?** -> plan-execute / orchestrator
  * **High risk anywhere?** -> escalate (handoff to human), regardless

The mapping table IS the deliverable here; the executors are deliberately small
stand-ins so the *structure* is visible rather than buried in business logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Route shapes (decoupled from v2 so this module is self-contained & testable)
# ---------------------------------------------------------------------------


@dataclass
class IntentItem:
    """One detected intent (a clause + its route label)."""

    text: str
    route: str          # faq | workflow | rag | agent | human
    intent: str = ""
    confidence: float = 0.8
    complexity: str = "simple"   # simple | complex   (does it need multi-step?)
    depends_on: List[str] = field(default_factory=list)  # intent names it needs first


@dataclass
class TopologyChoice:
    topology: str        # single | parallel | chaining | supervisor | debate | escalate | clarify
    reason: str
    intents: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"topology": self.topology, "reason": self.reason,
                "intents": list(self.intents)}


# ---------------------------------------------------------------------------
# The selection rules — this is the heart of the demo
# ---------------------------------------------------------------------------


HIGH_RISK_ROUTES = {"human"}
HIGH_RISK_INTENTS = {"risk.complaint", "compliance", "human"}


def select_topology(items: List[IntentItem], *, low_confidence: float = 0.34) -> TopologyChoice:
    """Map the detected intents to the topology that should execute them.

    Order matters: safety checks first, then structural decisions.
    """
    names = [it.intent or it.route for it in items]

    # 1. Nothing usable -> clarify FIRST.  A low-confidence match must not be
    #    escalated just because its *nearest* intent happens to be high-risk
    #    (e.g. "你好" is closest to complaint but is really unknown).  Clarify
    #    before we trust any label — including the risk label.
    if (not items
            or all(it.route == "clarify" for it in items)
            or all(it.confidence < low_confidence for it in items)):
        return TopologyChoice("clarify", "意图置信度过低/无法归类，先追问而不是硬路由", names)

    # 2. High-risk anywhere wins: never let a real complaint be buried under a
    #    refund.  Only trusted (confident enough) intents reach here.
    if any(it.route in HIGH_RISK_ROUTES or it.intent in HIGH_RISK_INTENTS for it in items):
        return TopologyChoice("escalate", "命中高风险意图（投诉/合规），必须转人工", names)

    # 3. Single intent.
    if len(items) == 1:
        it = items[0]
        if it.complexity == "complex":
            return TopologyChoice(
                "plan_execute",
                "单意图但需要多步：先规划再执行（%s）" % (it.intent or it.route), names)
        if it.route == "rag":
            return TopologyChoice("rag", "单意图知识问答：直接检索带引用作答", names)
        return TopologyChoice("single", "单意图简单请求：一个处理器直接完成", names)

    # 4. Multiple intents — decide by their relationship.
    has_dependency = any(it.depends_on for it in items)

    # 4a. dependent intents must be ordered -> chaining (or supervisor if >2)
    if has_dependency:
        topo = "chaining" if len(items) == 2 else "supervisor"
        return TopologyChoice(
            topo, "多意图且有依赖：必须按顺序执行（下游需要上游结果）", names)

    # 4b. conflicting intents need adjudication, not concatenation.
    if _conflicting(items):
        return TopologyChoice(
            "supervisor", "多意图且结论可能冲突：需要主管合并时做裁决，而不是简单拼接", names)

    # 4c. independent intents -> parallel fan-out.
    return TopologyChoice(
        "parallel", "多意图且相互独立：并行扇出后拼接，最快", names)


def _conflicting(items: List[IntentItem]) -> bool:
    """Heuristic: does any pair plausibly contradict?

    A refund intent + a policy/order lookup are NOT conflicting.  A refund +
    a "don't refund" style constraint, or two intents that write the same
    resource, would be.  For the demo we flag: refund vs a policy *explain*
    intent that could say otherwise, and any two 'write' workflows on the same
    order.  Keep it explicit and inspectable.
    """
    intents = {it.intent or it.route for it in items}
    write_workflows = [it for it in items if it.route == "workflow" and
                       (it.intent or "").startswith(("refund", "cancel", "change"))]
    # two write actions on the same order could conflict
    if len(write_workflows) >= 2:
        return True
    # a refund request combined with an explicit policy explanation may disagree
    if "refund.request" in intents and "doc.explain" in intents:
        return True
    return False


# ---------------------------------------------------------------------------
# Tiny executors, one per topology — enough to see the structure run.
# ---------------------------------------------------------------------------


@dataclass
class ExecOutcome:
    topology: str
    status: str
    output: str
    detail: List[str] = field(default_factory=list)
    tokens: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"topology": self.topology, "status": self.status,
                "output": self.output, "detail": self.detail, "tokens": self.tokens}


def execute(choice: TopologyChoice, items: List[IntentItem]) -> ExecOutcome:
    topo = choice.topology

    if topo == "clarify":
        return ExecOutcome(topo, "clarify",
                           "请问你具体想办理哪件事？" + " / ".join(choice.intents))

    if topo == "escalate":
        return ExecOutcome(topo, "escalated",
                           "已转人工处理（高风险诉求优先）。",
                           detail=["escalate: " + ", ".join(choice.intents)])

    if topo == "single":
        it = items[0]
        return ExecOutcome(topo, "success", "[single] 直接处理：%s" % it.text,
                           detail=[it.intent or it.route], tokens=120)

    if topo == "rag":
        it = items[0]
        return ExecOutcome(topo, "success",
                           "[rag] 检索作答：%s（带引用）" % it.text,
                           detail=[it.intent or it.route], tokens=300)

    if topo == "plan_execute":
        it = items[0]
        steps = ["规划", "查数据", "执行", "验证"]
        return ExecOutcome(topo, "success",
                           "[plan_execute] %s → %s" % ("→".join(steps), it.text),
                           detail=steps, tokens=600)

    if topo == "parallel":
        # run all intents (conceptually in parallel), then concatenate
        lines = ["[parallel] 并行执行 %d 个子意图后拼接：" % len(items)]
        detail = []
        for it in items:
            lines.append("  · [%s] %s → 完成" % (it.intent or it.route, it.text))
            detail.append(it.intent or it.route)
        return ExecOutcome(topo, "success", "\n".join(lines), detail=detail,
                           tokens=120 * len(items))

    if topo == "chaining":
        # ordered: each step's output feeds the next
        lines = ["[chaining] 按依赖顺序执行："]
        carry = ""
        detail = []
        for i, it in enumerate(items, 1):
            lines.append("  %d. [%s] %s%s" %
                         (i, it.intent or it.route, it.text,
                          ("（基于上一步：%s）" % carry) if carry else ""))
            carry = (it.intent or it.route)
            detail.append(it.intent or it.route)
        return ExecOutcome(topo, "success", "\n".join(lines), detail=detail,
                           tokens=150 * len(items))

    if topo == "supervisor":
        lines = ["[supervisor] 主管拆分 %d 个子任务 → 各自执行 → 合并裁决：" % len(items)]
        detail = []
        for it in items:
            lines.append("  · %s：%s" % (it.intent or it.route, it.text))
            detail.append(it.intent or it.route)
        lines.append("  → 合并时裁决冲突，输出一个一致答复")
        return ExecOutcome(topo, "success", "\n".join(lines), detail=detail,
                           tokens=150 * len(items))

    return ExecOutcome(topo, "failed", "未知拓扑：%s" % topo)
