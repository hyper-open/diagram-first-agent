"""Topology: Arena — many candidates, pairwise battles, a ranking.

```
  candidates: [c1, c2, c3, c4]
        |
        v   round-robin (or single-elimination) pairwise battles
     c1 vs c2 --+
     c1 vs c3   +--> wins/losses --> Borda-like score --> ranked list
     c2 vs c3 --+
        |
        v
   answer = top-ranked candidate (+ the ranking)
```

How Arena differs from Debate (the one thing worth keeping straight):

| | Debate (v4) | Arena (here) |
| --- | --- | --- |
| Participants | 1 proposer + 1 critic | N candidates |
| Judge role | picks a side, gives a rationale | repeats per pair, gives a winner |
| Output | one answer or "disagreement" | an ordered ranking |
| Cost | 3 calls | O(N²) or O(N·logN) battles |

The classic failure of an arena is **intransitivity**: c1 beats c2, c2 beats c3,
but c3 beats c1.  Any pairwise method inherits this.  This implementation is
honest about it: it reports the score table *and* flags cycles, rather than
pretending the winner is unambiguous.

Second failure: **position bias** — judges prefer whichever candidate they see
first.  So every battle runs both orders (A vs B and B vs A) and only counts a
win if the candidate wins in *both* orders; otherwise it is recorded as a draw.
That doubles cost and is a deliberate trade for reliability.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from itertools import combinations
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..mini_agent_v4.contract import BudgetTree

# A judge takes (candidate_a, candidate_b) and returns +1 if a wins, -1 if b
# wins, 0 for a draw.  Real judges call a model; the interface is what matters.
Judge = Callable[["Candidate", "Candidate"], int]


@dataclass
class Candidate:
    """One candidate answer and where it came from."""

    id: str
    text: str
    source: str = "agent"  # which agent/model produced it
    tokens: int = 0

    def short(self, n: int = 40) -> str:
        return self.text[:n].replace("\n", " ")


@dataclass
class Battle:
    a: str
    b: str
    winner: str  # candidate id, or "draw"
    consistency: bool  # did both orders agree?
    order_scores: Tuple[int, int] = (0, 0)

    def to_dict(self) -> Dict[str, object]:
        return {
            "a": self.a,
            "b": self.b,
            "winner": self.winner,
            "consistent": self.consistency,
            "forward": self.order_scores[0],
            "reverse": self.order_scores[1],
        }


@dataclass
class Arena:
    name: str = "arena"
    candidates: List[Candidate] = field(default_factory=list)
    judge: Optional[Judge] = None
    # "round_robin" compares every pair; "single_elim" is cheaper but less fair.
    schedule: str = "round_robin"
    # If True, a candidate must win in both presentation orders to score a win.
    swap_orders: bool = True
    max_candidates: int = 6  # guards O(N^2) cost

    def __post_init__(self) -> None:
        if self.judge is None:
            self.judge = _length_and_hedge_judge

    def run(self, goal: str, *, budget=None) -> Dict[str, object]:
        started = time.monotonic()
        budget = budget or BudgetTree(max_tokens=40_000, max_steps=40, label="arena")

        candidates = self.candidates[: self.max_candidates]
        if len(candidates) < 2:
            return self._result(
                goal, candidates, [], {},
                winner=None, note="至少需要 2 个候选才能对战", started=started,
                budget=budget,
            )

        pairs = list(combinations(candidates, 2))
        if self.schedule == "single_elim":
            # Compare consecutive seeds only; cheaper, but a weak candidate can
            # survive by never meeting a strong one.
            pairs = [(candidates[i], candidates[i + 1]) for i in range(0, len(candidates) - 1, 2)]

        # Budget: each battle (and its swapped twin) is a paid model call.
        battles_per_pair = 2 if self.swap_orders else 1
        rounds = budget.reserve(len(pairs))
        if not rounds and pairs:
            return self._result(
                goal, candidates, [], {},
                winner=None, note="预算不足以完成任何一场对战", started=started,
                budget=budget,
            )

        battles: List[Battle] = []
        scores: Dict[str, float] = {c.id: 0.0 for c in candidates}
        wins: Dict[str, int] = {c.id: 0 for c in candidates}
        losses: Dict[str, int] = {c.id: 0 for c in candidates}
        draws: Dict[str, int] = {c.id: 0 for c in candidates}

        for pair, pair_budget in zip(pairs, rounds):
            a, b = pair
            battle = self._run_battle(a, b, pair_budget, battles_per_pair)
            budget.settle(pair_budget)
            battles.append(battle)

            if battle.winner == "draw":
                draws[a.id] += 1
                draws[b.id] += 1
                scores[a.id] += 0.5
                scores[b.id] += 0.5
            elif battle.winner == a.id:
                wins[a.id] += 1
                losses[b.id] += 1
                scores[a.id] += 1.0
            else:
                wins[b.id] += 1
                losses[a.id] += 1
                scores[b.id] += 1.0

        # Ranking: score desc, then wins, then id for determinism.
        ranking = sorted(
            candidates, key=lambda c: (scores[c.id], wins[c.id], c.id), reverse=True
        )
        cycles = self._find_cycles(battles)
        winner = ranking[0] if ranking else None

        note = ""
        if cycles:
            note = "检测到非传递性（A>B>C>A）：排名依赖对阵顺序，结论不可当作绝对优劣。"

        return self._result(
            goal, candidates, battles, scores,
            winner=winner, note=note, started=started, budget=budget,
            ranking=ranking, wins=wins, losses=losses, draws=draws, cycles=cycles,
        )

    # -- internals -------------------------------------------------------

    def _run_battle(self, a: Candidate, b: Candidate, budget: BudgetTree,
                    battles_per_pair: int) -> Battle:
        if not self.swap_orders or battles_per_pair == 1:
            score = self.judge(a, b)  # type: ignore[misc]
            budget.spend(tokens=500, steps=1)
            winner = a.id if score > 0 else (b.id if score < 0 else "draw")
            return Battle(a.id, b.id, winner, consistency=True, order_scores=(score, 0))

        # Both orders: a direct guard against position bias.
        forward = self.judge(a, b)  # type: ignore[misc]
        budget.spend(tokens=500, steps=1)
        reverse = self.judge(b, a)  # type: ignore[misc]
        budget.spend(tokens=500, steps=1)

        forward_winner = a.id if forward > 0 else (b.id if forward < 0 else "draw")
        # In the reverse call, a positive score now means *b* won.
        reverse_winner = b.id if reverse > 0 else (a.id if reverse < 0 else "draw")

        if forward_winner == reverse_winner and forward_winner != "draw":
            return Battle(a.id, b.id, forward_winner, True, (forward, reverse))
        # Disagreement across orders → a draw, not a coin flip.
        return Battle(a.id, b.id, "draw", False, (forward, reverse))

    @staticmethod
    def _find_cycles(battles: Sequence[Battle]) -> List[List[str]]:
        """Detect 3-cycles A>B>C>A among decisive battles."""
        edges: Dict[str, set] = {}
        for battle in battles:
            if battle.winner == "draw":
                continue
            loser = battle.b if battle.winner == battle.a else battle.a
            edges.setdefault(battle.winner, set()).add(loser)
        cycles = []
        nodes = list(edges)
        for a, b, c in combinations(nodes, 3):
            if (b in edges.get(a, set()) and c in edges.get(b, set()) and a in edges.get(c, set())) or \
               (c in edges.get(a, set()) and b in edges.get(c, set()) and a in edges.get(b, set())):
                cycles.append(sorted([a, b, c]))
        return cycles

    def _result(self, goal, candidates, battles, scores, *, winner, note,
                started, budget, ranking=None, wins=None, losses=None,
                draws=None, cycles=None) -> Dict[str, object]:
        ranking = ranking or []
        if winner is None:
            answer = note or "无可比候选。"
        else:
            lines = ["「%s」竞技场排名：" % goal]
            for i, c in enumerate(ranking, 1):
                lines.append("  %d. [%s] %s（%.1f 分）"
                             % (i, c.id, c.short(50), scores.get(c.id, 0.0)))
            if note:
                lines.append(note)
            answer = "\n".join(lines)
        return {
            "topology": "arena",
            "goal": goal,
            "ok": winner is not None,
            "answer": answer,
            "winner": winner.id if winner else None,
            "ranking": [c.id for c in ranking],
            "scores": {k: round(v, 2) for k, v in scores.items()},
            "record": {"wins": wins or {}, "losses": losses or {},
                       "draws": draws or {}},
            "battles": [b.to_dict() for b in battles],
            "cycles": cycles or [],
            "candidates": [{"id": c.id, "source": c.source, "text": c.short(80)}
                           for c in candidates],
            "tokens_used": budget.tokens,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }


def _length_and_hedge_judge(a: Candidate, b: Candidate) -> int:
    """Deterministic stand-in judge.

    Scoring rule (intentionally naive but *explainable*): a longer answer that
    hedges less wins.  Real judges call a model; this one exists so the arena's
    *mechanics* (pairing, swap-order, intransitivity) can be exercised offline.
    """
    def score(c: Candidate) -> float:
        hedge = c.text.count("可能") + c.text.count("也许") + c.text.count("大概")
        return len(c.text) - hedge * 20

    sa, sb = score(a), score(b)
    if sa > sb:
        return 1
    if sa < sb:
        return -1
    return 0
