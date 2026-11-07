"""Tests for Mini Agent v4 — the four multi-agent topologies.

The suite is written around the *invariants* the handbook cares about, not around
happy paths:

* a failed worker must degrade to data, not crash the orchestrator;
* merging must not launder confidence upward (min, not average);
* fan-out must never spend more than the parent's cap;
* every topology must terminate, even the peer one that has no leader;
* debate must report disagreement instead of fabricating consensus.
"""
from __future__ import annotations

import unittest

from examples.mini_agent_v4 import (
    TOPOLOGIES,
    AgentRole,
    BudgetTree,
    DebateTopology,
    HierarchicalTopology,
    PeerTopology,
    SubResult,
    SupervisorTopology,
)
from examples.mini_agent_v4.contract import (
    BudgetExhausted,
    merge_confidence,
    new_task,
    build_worker,
)
from examples.mini_agent_v4.hierarchical import Branch


class ContractTests(unittest.TestCase):
    def test_budget_reserve_never_exceeds_parent(self):
        parent = BudgetTree(max_tokens=3000, max_steps=20)
        children = parent.reserve(10)
        self.assertLessEqual(len(children) * children[0].max_tokens, 3000)
        # after reserving, the parent has already deducted capacity
        self.assertLessEqual(parent.remaining_tokens(), 3000)

    def test_reserve_returns_fewer_when_budget_tiny(self):
        parent = BudgetTree(max_tokens=1000, max_steps=10)  # only 1 call affordable
        children = parent.reserve(5)
        self.assertEqual(len(children), 1)

    def test_settle_refunds_unspent(self):
        parent = BudgetTree(max_tokens=3000, max_steps=20)
        child = parent.reserve(1)[0]
        before = parent.remaining_tokens()
        child.spend(tokens=100, steps=1)
        parent.settle(child)
        after = parent.remaining_tokens()
        # The child's *unspent* share comes back; the 100 spent does not.
        self.assertGreater(after, before)
        self.assertLess(after, before + child.max_tokens + 1)

    def test_spend_over_limit_raises(self):
        budget = BudgetTree(max_tokens=100, max_steps=2)
        with self.assertRaises(BudgetExhausted):
            budget.spend(tokens=200)

    def test_merge_confidence_is_min_not_average(self):
        a = SubResult("t1", "a", True, summary="x", confidence=0.9)
        b = SubResult("t2", "b", True, summary="y", confidence=0.2)
        self.assertEqual(merge_confidence([a, b]), 0.2)

    def test_empty_summary_is_not_usable(self):
        empty = SubResult("t", "r", True, summary="   ", confidence=0.99)
        self.assertFalse(empty.usable)

    def test_failed_result_is_not_usable(self):
        failed = SubResult("t", "r", False, reason="boom", confidence=0.9)
        self.assertFalse(failed.usable)

    def test_worker_respects_its_budget(self):
        role = AgentRole("r", "做 {goal}")
        task = new_task("r", "做 任务")
        tiny = BudgetTree(max_tokens=100, max_steps=3)
        result = build_worker(role, tokens_per_call=800)(task, tiny)
        self.assertFalse(result.ok)
        self.assertIn("budget_exhausted", result.reason)


class SupervisorTests(unittest.TestCase):
    def test_happy_path_merges_all_workers(self):
        result = SupervisorTopology().run("整理退款政策")
        self.assertTrue(result.ok)
        self.assertEqual(result.subtasks, 3)
        self.assertEqual(result.failures, [])
        self.assertEqual(len(result.answer.splitlines()), 4)  # header + 3 bullets

    def test_one_failed_worker_still_merges(self):
        roles = [
            AgentRole("ok1", "部分A {goal}", confidence=0.8),
            AgentRole("bad", "失败", fail_above=0, fail_reason="tool_timeout"),
            AgentRole("ok2", "部分B {goal}", confidence=0.6),
        ]
        result = SupervisorTopology(workers=roles).run("任务")
        self.assertTrue(result.ok)
        self.assertEqual(len(result.failures), 1)
        self.assertEqual(result.confidence, 0.6)  # min of usable, not average

    def test_all_failed_reports_cannot_complete(self):
        roles = [AgentRole("a", "x", fail_above=0, fail_reason="down")]
        result = SupervisorTopology(workers=roles).run("任务")
        self.assertFalse(result.ok)
        self.assertIn("无法完成", result.answer)
        self.assertEqual(result.confidence, 0.0)

    def test_fanout_respects_budget_cap(self):
        roles = [AgentRole("w%d" % i, "任务 {goal}") for i in range(10)]
        result = SupervisorTopology(workers=roles).run(
            "大任务", budget=BudgetTree(max_tokens=3000, max_steps=20)
        )
        self.assertLessEqual(result.tokens_used, 3000)
        self.assertLess(result.trace[0]["spawned"], 10)


class PeerTests(unittest.TestCase):
    def test_peer_terminates_within_round_cap(self):
        result = PeerTopology(max_rounds=3).run("讨论退款")
        self.assertLessEqual(len(result.trace), 3 * 2 + 3)  # turns + stop events
        self.assertTrue(result.ok)

    def test_peer_stops_on_no_new_evidence(self):
        # A single-round peer has no "previous round", so it runs one round then
        # the second round would add no new evidence and must stop.
        result = PeerTopology(
            peers=[AgentRole("only", "看 {goal}", confidence=0.7)], max_rounds=5
        ).run("问题")
        stops = [t for t in result.trace if t.get("step") == "stop"]
        self.assertTrue(stops, "peer must have an explicit stop reason")

    def test_peer_has_explicit_termination_reason(self):
        result = PeerTopology(max_rounds=2).run("g")
        reasons = {t.get("reason") for t in result.trace if t.get("step") == "stop"}
        self.assertTrue(reasons, "终止必须有理由，不能只是跑完循环")

    def test_context_is_not_nested_into_goal(self):
        # The bug we fixed: appending context to goal made each turn nest the
        # previous answer.  Assert the shortest summary stays short.
        result = PeerTopology(max_rounds=2).run("简短问题")
        shortest = min(len(r.summary) for r in result.results)
        self.assertLess(shortest, 200)


class HierarchicalTests(unittest.TestCase):
    def test_two_levels_both_run(self):
        result = HierarchicalTopology().run("综合研究任务")
        self.assertTrue(result.ok)
        workers = [t for t in result.trace if t.get("step") == "worker"]
        self.assertGreaterEqual(len(workers), 3)

    def test_depth_guard_prevents_further_delegation(self):
        result = HierarchicalTopology(max_depth=1).run("超大任务")
        guards = [t for t in result.trace if t.get("step") == "depth_guard"]
        self.assertGreaterEqual(len(guards), 1)
        self.assertTrue(result.ok)

    def test_evidence_is_preserved_up_the_tree(self):
        result = HierarchicalTopology().run("任务")
        # Every worker produced one evidence entry; the top result should carry
        # them forward rather than rewrite them away.
        self.assertTrue(any("::" in e for r in result.results for e in r.evidence))

    def test_branch_all_workers_failed(self):
        branches = [Branch(lead=AgentRole("lead", "统筹 {goal}"),
                           workers=[AgentRole("w", "做", fail_above=0, fail_reason="down")])]
        result = HierarchicalTopology(branches=branches).run("任务")
        self.assertFalse(result.ok)


class DebateTests(unittest.TestCase):
    def test_debate_produces_a_verdict(self):
        result = DebateTopology().run("是否退款")
        self.assertTrue(result.ok)
        self.assertIn("裁决", result.answer)
        # propose + critique + judge
        steps = [t["step"] for t in result.trace]
        self.assertEqual(steps, ["propose", "critique", "judge"])

    def test_strong_objection_blocks_consensus(self):
        debate = DebateTopology(
            proposer=AgentRole("proposer", "主张 {goal}", confidence=0.4),
            critic=AgentRole("critic", "反驳 {goal}", confidence=0.9),
            judge=AgentRole("judge", "裁决 {goal}", confidence=0.5),
            confidence_floor=0.5,
        )
        result = debate.run("是否退款")
        self.assertFalse(result.ok)
        self.assertIn("异议", result.answer)

    def test_judge_failure_is_reported_not_faked(self):
        debate = DebateTopology(
            judge=AgentRole("judge", "裁决 {goal}", fail_above=0, fail_reason="judge_down")
        )
        result = debate.run("g")
        self.assertFalse(result.ok)
        self.assertIn("裁决失败", result.answer)


class CrossTopologyTests(unittest.TestCase):
    def test_all_topologies_return_same_shape(self):
        goal = "评估退款"
        keys = None
        for name, cls in TOPOLOGIES.items():
            with self.subTest(topology=name):
                result = cls().run(goal)
                payload = result.to_dict()
                if keys is None:
                    keys = set(payload)
                self.assertEqual(set(payload), keys, "所有拓扑必须返回同一结构")
                self.assertEqual(payload["topology"], name)
                self.assertIsInstance(payload["ok"], bool)
                self.assertGreaterEqual(payload["confidence"], 0.0)

    def test_all_topologies_ok_on_healthy_workers(self):
        for name, cls in TOPOLOGIES.items():
            with self.subTest(topology=name):
                self.assertTrue(cls().run("普通任务").ok)

    def test_every_topology_reports_tokens(self):
        for name, cls in TOPOLOGIES.items():
            with self.subTest(topology=name):
                self.assertGreater(cls().run("任务").tokens_used, 0)


if __name__ == "__main__":
    unittest.main()
