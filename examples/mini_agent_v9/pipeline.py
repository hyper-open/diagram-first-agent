"""Wire v2 multi-intent routing into v9 topology selection + execution.

This is the end-to-end demo path:

    text -> route_multi (v2) -> IntentItem[] -> select_topology -> execute

We annotate each routed clause with a little "complexity" inference so the
selector can choose plan_execute for a hard single intent.  That inference is
deliberately a simple keyword check — the *structure* (route -> choose topology
-> execute) is what this demo is about, not the quality of the heuristic.
"""
from __future__ import annotations

from typing import List

from ..mini_agent_v2.router import AGENT, IntentRouter, default_intents
from .select_topology import ExecOutcome, IntentItem, TopologyChoice, execute, select_topology

_COMPLEX_MARKERS = ("整理成", "汇总", "分析一下", "对比一下", "一步一步", "拆")


def text_to_intents(text: str, router: IntentRouter = None) -> tuple:
    """Route a (possibly multi-intent) request into IntentItem[]."""
    router = router or IntentRouter(default_intents())
    multi = router.route_multi(text)
    items: List[IntentItem] = []
    for sc in multi.scenarios:
        complexity = "complex" if any(m in sc.text for m in _COMPLEX_MARKERS) else "simple"
        items.append(IntentItem(
            text=sc.text,
            route=sc.route.route,
            intent=sc.route.intent,
            confidence=sc.route.confidence,
            complexity=complexity,
        ))
    # If everything was low-confidence, keep one item so the selector can CLARIFY.
    if not items and multi.primary:
        items.append(IntentItem(
            text=text, route=multi.primary.route,
            intent=multi.primary.intent, confidence=multi.primary.confidence))
    return multi, items


def handle(text: str) -> dict:
    """One request all the way through: route -> choose topology -> execute."""
    multi, items = text_to_intents(text)
    choice = select_topology(items)
    outcome = execute(choice, items)
    return {
        "input": text,
        "primary_intent": multi.primary.intent or multi.primary.route,
        "is_multi": multi.is_multi,
        "intents": [{"intent": it.intent or it.route, "text": it.text,
                     "route": it.route, "confidence": round(it.confidence, 2),
                     "complexity": it.complexity} for it in items],
        "topology": choice.to_dict(),
        "result": outcome.to_dict(),
    }
