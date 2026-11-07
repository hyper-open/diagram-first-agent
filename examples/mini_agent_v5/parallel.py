"""Pattern 3: Parallelization (并行).

```
        +-> [shard 1] --+
 task ->+-> [shard 2] --+--> merge / vote -> answer
        +-> [shard 3] --+
```

Two uses from the handbook, and they are *not* the same thing:

* **Sharding** — split one big input into independent chunks (many files, many
  documents), process each, concatenate.
* **Multi-perspective** — send the *same* question to several independent
  attempts, then **vote** (deterministic tasks) or **synthesize** (open tasks).

Keeping them separate in code matters because their merge logic differs:
sharding concatenates, multi-perspective reconciles disagreement.

Two guards that make this safe:

* **independence check** — shards must not depend on each other; if one output
  is needed by another, this is not a parallel job (that is chaining).
* **budget fan-out** — the same ``reserve`` invariant from v4: N shards cannot
  spend more than the parent budget, and we do not spawn shards we cannot afford.

Differences from v4's Supervisor: no planner decides the split, and the merge
is a *fixed* rule (concat / vote) rather than model judgement.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Sequence

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
class Parallelization:
    name: str = "parallel"
    mode: str = "sharding"  # sharding | perspectives
    shards: List[Stage] = field(default_factory=list)
    # For perspectives mode: how to turn several answers into one.
    vote: str = "majority"  # majority | first | synthesize

    def __post_init__(self) -> None:
        if not self.shards and self.mode == "sharding":
            self.shards = [
                Stage("shard-1", "处理文档1：{input}"),
                Stage("shard-2", "处理文档2：{input}"),
                Stage("shard-3", "处理文档3：{input}"),
            ]
        elif not self.shards and self.mode == "perspectives":
            self.shards = [
                Stage("attempt-1", "方案A：{input}"),
                Stage("attempt-2", "方案A：{input}"),
                Stage("attempt-3", "方案B：{input}"),
            ]

    def run(self, goal: str, *, budget=None) -> PatternResult:
        started = time.monotonic()
        ctx = RunContext(goal=goal, budget=budget or new_budget())
        results: List[StageResult] = []

        # Budget fan-out: reserve one slice per shard, spawn only what we can
        # afford.  This reuses the v4 invariant rather than re-deriving it.
        children = ctx.budget.reserve(len(self.shards))
        ctx.log(
            step="fanout",
            mode=self.mode,
            planned=len(self.shards),
            spawned=len(children),
        )
        if not children:
            return build_summary(
                self.name, ctx, answer="预算不足以并行处理任何分片。",
                stop_reason=StopReason.PARTIAL, stage_results=results, started=started,
            )

        for stage, child_budget in zip(self.shards, children):
            # Each shard runs under its own budget slice, then settles (refunds
            # the difference) so a fast shard returns capacity.
            sub_ctx = RunContext(goal=goal, budget=child_budget)
            result = safe_call(stage, goal, sub_ctx)
            ctx.budget.settle(child_budget)
            results.append(result)
            ctx.log(stage=result.stage, passed=result.passed, tokens=child_budget.tokens)

        ok_results = [r for r in results if r.ok]
        failed = [r for r in results if not r.ok]

        if not ok_results:
            return build_summary(
                self.name, ctx, answer="所有分片都失败。",
                stop_reason=StopReason.FAILED, stage_results=results, started=started,
            )

        if self.mode == "sharding":
            answer = self._concat(goal, ok_results)
        else:
            answer = self._reconcile(ok_results)

        # Partial coverage is reported honestly: we say which shards are missing.
        if failed:
            ctx.log(step="partial", failed=[r.stage for r in failed])
            return build_summary(
                self.name, ctx,
                answer=answer + "\n（注意：%d 个分片失败：%s）"
                % (len(failed), "、".join(r.stage for r in failed)),
                stop_reason=StopReason.PARTIAL, stage_results=results, started=started,
            )

        return build_summary(
            self.name, ctx, answer=answer,
            stop_reason=StopReason.SUCCESS, stage_results=results, started=started,
        )

    @staticmethod
    def _concat(goal: str, results: Sequence[StageResult]) -> str:
        lines = ["「%s」的分片结果：" % goal]
        lines.extend("- [%s] %s" % (r.stage, r.output) for r in results)
        return "\n".join(lines)

    def _reconcile(self, results: Sequence[StageResult]) -> str:
        if self.vote == "first":
            return results[0].output
        if self.vote == "majority":
            tally: Dict[str, int] = {}
            for r in results:
                tally[r.output] = tally.get(r.output, 0) + 1
            winner = max(tally, key=lambda k: tally[k])
            agree = tally[winner]
            if agree == 1:
                # No majority: do not pretend consensus.
                return "多种方案，无多数：\n" + "\n".join(
                    "- [%s] %s" % (r.stage, r.output) for r in results
                )
            return "%s（%d/%d 一致）" % (winner, agree, len(results))
        # synthesize: keep all, labelled
        return "\n".join("- [%s] %s" % (r.stage, r.output) for r in results)
