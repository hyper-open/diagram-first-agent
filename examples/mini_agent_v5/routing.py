"""Pattern 2: Routing (路由).

```
                 +-- 退款流程 --+
  request -> C{分类器} -- 技术问题 --+--> answer
                 +-- 投诉 -> 人工 --+
                 +-- 不确定 -> 兜底 --+
```

One request, one branch.  The whole value is in *choosing* correctly and in
having somewhere to go when you cannot choose.

The handbook insists on something most implementations skip: **a low-confidence
path**.  A classifier that must return one of N branches will confidently send a
garbage request into the wrong workflow.  So the router here has three outcomes:

* a confident branch → that branch;
* below ``confidence_floor`` → the **fallback** branch (often "human");
* a branch whose required input is missing → ``BLOCKED`` with the question.

That last one matters: "查订单" with no order id is not a failure, it is a
request for information.  Returning ``BLOCKED`` + questions is a *correct*
terminal state (see the orchestration chapter's terminal-state table).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .contract import (
    PatternResult,
    RunContext,
    Stage,
    StageResult,
    StopReason,
    build_summary,
    new_budget,
    safe_call,
)


@dataclass
class Branch:
    """A route and how to take it.

    ``matcher`` returns a confidence in [0, 1] for this branch (0 = not this
    branch).  ``handler`` turns the request into an answer.  ``requires`` names
    inputs the branch needs; if missing, the router reports BLOCKED.
    """

    name: str
    handler: Stage
    matcher: Callable[[str], float] = lambda text: 0.0
    requires: List[str] = field(default_factory=list)


def keyword_matcher(words: Sequence[str], base: float = 0.9) -> Callable[[str], float]:
    lowered = [w.lower() for w in words]

    def match(text: str) -> float:
        t = text.lower()
        hits = [w for w in lowered if w in t]
        if not hits:
            return 0.0
        # More distinct keyword hits ⇒ higher confidence, capped.
        return min(0.99, base + 0.03 * (len(hits) - 1))

    return match


@dataclass
class Routing:
    name: str = "routing"
    branches: List[Branch] = field(default_factory=list)
    fallback: Optional[str] = None  # branch name used when confidence is too low
    confidence_floor: float = 0.5

    def __post_init__(self) -> None:
        if not self.branches:
            self.branches = [
                Branch("refund", Stage("refund", "已进入退款流程：{input}"),
                       keyword_matcher(["退款", "退钱"])),
                Branch("tech", Stage("tech", "技术排查：{input}"),
                       keyword_matcher(["报错", "打不开", "404", "闪退"])),
                Branch("complaint", Stage("complaint", "投诉已升级人工：{input}"),
                       keyword_matcher(["投诉", "起诉", "媒体"])),
                # A dedicated fallback.  Do NOT reuse "complaint" as the default:
                # sending "今天天气怎么样" to the complaint team is both wrong
                # and costly.  The fallback should be a neutral, low-risk branch
                # (here: hand to a human), not a specific business flow.
                Branch("human", Stage("human", "已转人工，请稍候：{input}"),
                       keyword_matcher([])),
            ]
            self.fallback = "human"

    def run(self, goal: str, *, budget=None) -> PatternResult:
        started = time.monotonic()
        ctx = RunContext(goal=goal, budget=budget or new_budget())
        results: List[StageResult] = []

        # 1. Score every branch.  Deterministic, inspectable, testable.
        scores: Dict[str, float] = {}
        for branch in self.branches:
            scores[branch.name] = round(branch.matcher(goal), 3)
        ctx.log(step="classify", scores=scores, floor=self.confidence_floor)

        best_name = max(scores, key=lambda k: scores[k])
        best_score = scores[best_name]

        # 2. Confidence gate: never send an uncertain request into a workflow.
        if best_score < self.confidence_floor:
            if self.fallback is None:
                # Two different situations that must not be conflated:
                #  * no fallback configured  -> we cannot safely route at all,
                #    so a human must take over (NEEDS_HUMAN);
                #  * fallback configured     -> route there (below).
                # Returning BLOCKED here would be wrong: BLOCKED means "we know
                # what to do but lack an input"; here we do not know what to do.
                return build_summary(
                    self.name, ctx,
                    answer="无法判断请求类型，且未配置兜底分支；已转人工。",
                    stop_reason=StopReason.NEEDS_HUMAN, stage_results=results,
                    started=started,
                    questions=["请人工确认该请求应走哪条业务流程。"],
                )
            chosen = self.fallback
            ctx.log(step="fallback", chosen=chosen, because="low_confidence")
        else:
            chosen = best_name

        branch = next(b for b in self.branches if b.name == chosen)

        # 3. Missing required input → BLOCKED, not a wrong answer.
        missing = [key for key in branch.requires if key not in goal]
        if missing:
            ctx.log(step="blocked", branch=chosen, missing=missing)
            return build_summary(
                self.name, ctx,
                answer="已识别为「%s」，但还缺少必要信息。" % chosen,
                stop_reason=StopReason.BLOCKED, stage_results=results,
                started=started,
                questions=["请提供：%s" % "、".join(missing)],
            )

        # 4. Execute exactly one branch.
        result = safe_call(branch.handler, goal, ctx)
        results.append(result)
        ctx.log(step="branch", name=chosen, confidence=best_score, tokens=result.tokens)
        if result.failed:
            return build_summary(
                self.name, ctx, answer=result.output,
                stop_reason=StopReason.FAILED, stage_results=results, started=started,
            )
        return build_summary(
            self.name, ctx, answer=result.output,
            stop_reason=StopReason.SUCCESS, stage_results=results, started=started,
        )
