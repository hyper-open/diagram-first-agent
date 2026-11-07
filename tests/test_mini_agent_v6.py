"""Tests for Mini Agent v6 — Arena and Swarm/Handoff.

Arena's invariants:
* output is a ranking, not a single verdict;
* swap-order battles neutralise position bias;
* intransitivity is *reported*, never hidden;
* O(N²) cost is bounded by max_candidates.

Swarm's invariants:
* exactly one agent is active at a time;
* a handoff loop (A→B→A) is blocked, not infinite;
* escalation to a human is a success, not a failure;
* hop count is bounded.
"""
from __future__ import annotations

import unittest

from examples.mini_agent_v6 import Arena, Candidate, Swarm
from examples.mini_agent_v6.arena import Arena as ArenaCls
from examples.mini_agent_v6.swarm import HandoffAgent
from examples.mini_agent_v4.contract import BudgetTree


def cands(n=4):
    return [Candidate("c%d" % i, "答案" * (i + 1), "agent-%d" % i) for i in range(1, n + 1)]


class ArenaTests(unittest.TestCase):
    def test_returns_a_ranking_not_a_single_answer(self):
        result = Arena(candidates=cands(3)).run("问题")
        self.assertEqual(len(result["ranking"]), 3)
        self.assertIsNotNone(result["winner"])
        self.assertIn("排名", result["answer"])

    def test_every_pair_is_compared_in_round_robin(self):
        result = Arena(candidates=cands(4), schedule="round_robin").run("问题")
        # C(4,2) = 6 pairs
        self.assertEqual(len(result["battles"]), 6)

    def test_swap_orders_flags_inconsistency_as_draw(self):
        # A judge that always prefers the *first* argument → position bias.
        class FirstWins:
            def __call__(self, a, b):
                return 1

        result = Arena(candidates=cands(2), judge=FirstWins(), swap_orders=True).run("q")
        # Each pair sees both orders; a biased judge wins in one, loses in
        # reverse → recorded as draw, not a false victory.
        battle = result["battles"][0]
        self.assertEqual(battle["winner"], "draw")
        self.assertFalse(battle["consistent"])

    def test_consistent_judge_gives_decisive_winner(self):
        class LongerWins:
            def __call__(self, a, b):
                return 1 if len(a.text) > len(b.text) else (-1 if len(a.text) < len(b.text) else 0)

        result = Arena(candidates=cands(3), judge=LongerWins()).run("q")
        # The longest candidate should win every battle it is in.
        longest = max(cands(3), key=lambda c: len(c.text))
        self.assertEqual(result["winner"], longest.id)

    def test_intransitivity_is_reported(self):
        cyclic = {"a": "b", "b": "c", "c": "a"}

        def judge(x, y):
            if cyclic.get(x.id) == y.id:
                return 1
            if cyclic.get(y.id) == x.id:
                return -1
            return 0

        result = Arena(candidates=[Candidate("a", "A"), Candidate("b", "B"),
                                   Candidate("c", "C")], judge=judge).run("q")
        self.assertTrue(result["cycles"])
        self.assertIn("非传递性", result["answer"])

    def test_fewer_than_two_candidates_is_rejected(self):
        result = Arena(candidates=[Candidate("only", "x")]).run("q")
        self.assertFalse(result["ok"])
        self.assertIn("至少需要 2 个候选", result["answer"])

    def test_candidate_pool_is_capped(self):
        result = Arena(candidates=cands(10), max_candidates=3).run("q")
        self.assertEqual(len(result["candidates"]), 3)

    def test_budget_limits_battles(self):
        # A budget too small for all pairs must stop, not overspend.
        result = Arena(candidates=cands(5)).run(
            "q", budget=BudgetTree(max_tokens=1200, max_steps=40)
        )
        self.assertLessEqual(result["tokens_used"], 1200)

    def test_single_elim_schedule_is_cheaper(self):
        rr = Arena(candidates=cands(4), schedule="round_robin").run("q")
        se = Arena(candidates=cands(4), schedule="single_elim").run("q")
        self.assertLessEqual(len(se["battles"]), len(rr["battles"]))


class SwarmTests(unittest.TestCase):
    def test_happy_path_reaches_answer(self):
        result = Swarm().run("我要退款，订单 A1024")
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], ["triage", "refund"])
        self.assertFalse(result["escalated"])

    def test_handoff_loop_is_blocked_not_infinite(self):
        # refund → triage would loop; the revisit guard stops it.
        result = Swarm().run("我要退款")  # no order id → refund tries to bounce back
        self.assertFalse(result["ok"])
        self.assertIn("移交环", result["answer"])
        self.assertLessEqual(len(result["path"]), 2)

    def test_escalation_to_human_is_success(self):
        result = Swarm().run("我要投诉你们")
        self.assertTrue(result["ok"])  # escalation is correct behaviour
        self.assertTrue(result["escalated"])
        self.assertIn("移交人工", result["answer"])

    def test_only_one_agent_active_per_step(self):
        result = Swarm().run("网站报错打不开")
        # every step is produced by exactly one agent
        for step in result["steps"]:
            self.assertIn("agent", step)
            self.assertTrue(step["agent"])

    def test_hop_limit_is_enforced(self):
        # A chain that never repeats and never answers, longer than max_hops.
        def make_chain(n):
            agents = {}
            for i in range(n):
                nxt = "a%d" % (i + 1)
                agents["a%d" % i] = HandoffAgent(
                    "a%d" % i, (lambda nxt: (lambda g, h: ("handoff", nxt, "next")))(nxt)
                )
            agents["a%d" % n] = HandoffAgent("a%d" % n, lambda g, h: ("answer", "done"))
            return agents

        swarm = Swarm(agents=make_chain(10), entry="a0", max_hops=3)
        result = swarm.run("q")
        self.assertFalse(result["ok"])
        self.assertIn("最大跳数", result["answer"])
        self.assertLessEqual(len(result["path"]), 3)

    def test_unknown_agent_is_blocked(self):
        swarm = Swarm(agents={"triage": HandoffAgent("triage",
                                                    lambda g, h: ("handoff", "ghost", "x"))},
                      entry="triage")
        result = swarm.run("q")
        self.assertFalse(result["ok"])
        self.assertIn("未知 Agent", result["answer"])

    def test_unknown_decision_is_blocked(self):
        swarm = Swarm(agents={"triage": HandoffAgent("triage", lambda g, h: ("banana",))},
                      entry="triage")
        result = swarm.run("q")
        self.assertFalse(result["ok"])

    def test_budget_is_charged_per_hop(self):
        result = Swarm().run("我要退款，订单 A1024")
        self.assertGreater(result["tokens_used"], 0)


class DistinctionTests(unittest.TestCase):
    """The whole point of v6 is that these are *not* v4's debate/peer."""

    def test_arena_is_not_debate(self):
        # Arena returns a ranking of N candidates; debate returns one verdict.
        arena = Arena(candidates=cands(3)).run("q")
        self.assertIn("ranking", arena)
        self.assertEqual(len(arena["ranking"]), 3)

    def test_swarm_path_is_single_threaded(self):
        # Unlike peer (all talk each round), a swarm path is a single chain of
        # handoffs: len(path) == number of steps, one agent each.
        result = Swarm().run("我要退款，订单 A1024")
        self.assertEqual(len(result["path"]), len(result["steps"]))

    def test_escalation_differs_from_failure(self):
        human = Swarm().run("我要投诉你们")
        blocked = Swarm().run("我要退款")  # loops → blocked
        self.assertTrue(human["ok"] and human["escalated"])
        self.assertFalse(blocked["ok"] and blocked["escalated"])


if __name__ == "__main__":
    unittest.main()
