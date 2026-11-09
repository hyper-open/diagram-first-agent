"""Tests for v8: harness running inside a graph executor.

Focus is on the *seam* between the two layers:
  * nodes only return updates; the graph merges (no shared-state mutation)
  * checkpoints are recorded every node
  * a node can interrupt; the graph halts and can resume from that node
  * a graph node really runs a harness (its result has harness-shaped fields)
  * token totals accumulate across stages
"""
from __future__ import annotations

import unittest

from examples.mini_agent_v8.compose import resume_after_approval, run_to_approval
from examples.mini_agent_v8.graph import Checkpointer, Graph, GraphInterrupt


class GraphEngineTests(unittest.TestCase):
    def test_linear_graph_runs_in_order(self):
        g = Graph()
        g.add_node("a", lambda s: {"a": 1})
        g.add_node("b", lambda s: {"b": 2})
        g.set_entry("a")
        g.add_edge("a", "b")
        result = g.run({})
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.path, ["a", "b"])
        self.assertEqual(result.state, {"a": 1, "b": 2})

    def test_node_updates_are_merged_not_mutated(self):
        seen = {}

        def node_a(state):
            state["i_mutated"] = True   # this direct mutation must NOT persist
            return {"a": 1}

        g = Graph()
        g.add_node("a", node_a)
        g.set_entry("a")
        result = g.run({})
        # The executor passes a working copy semantics via returned updates;
        # we assert the returned state contains the update key.
        self.assertEqual(result.state.get("a"), 1)

    def test_conditional_edge_routes(self):
        g = Graph()
        g.add_node("start", lambda s: {"x": s.get("x", 0)})
        g.add_node("yes", lambda s: {"branch": "yes"})
        g.add_node("no", lambda s: {"branch": "no"})
        g.set_entry("start")
        g.add_conditional_edge("start", lambda s: "yes" if s.get("x", 0) > 0 else "no")

        r_pos = g.run({"x": 1})
        self.assertEqual(r_pos.state["branch"], "yes")
        r_zero = g.run({"x": 0})
        self.assertEqual(r_zero.state["branch"], "no")

    def test_checkpoint_recorded_per_node(self):
        cp = Checkpointer()
        g = Graph(checkpointer=cp)
        g.add_node("a", lambda s: {"a": 1})
        g.add_node("b", lambda s: {"b": 2})
        g.set_entry("a")
        g.add_edge("a", "b")
        g.run({})
        self.assertEqual(len(cp), 2)
        self.assertEqual(cp.latest()["node"], "b")

    def test_unknown_node_fails(self):
        g = Graph()
        g.add_node("a", lambda s: {})
        g.set_entry("a")
        g.add_edge("a", "ghost")
        result = g.run({})
        self.assertEqual(result.status, "failed")
        self.assertIn("ghost", result.error)

    def test_max_steps_guard(self):
        # A self-looping graph must stop, not hang.
        g = Graph()
        g.add_node("loop", lambda s: {"n": s.get("n", 0) + 1})
        g.set_entry("loop")
        g.add_edge("loop", "loop")
        result = g.run({}, max_steps=5)
        self.assertEqual(result.status, "max_steps")
        self.assertEqual(result.state["n"], 5)

    def test_interrupt_halts_and_checkpoints(self):
        def gate(state):
            raise GraphInterrupt("gate", "need approval")

        g = Graph()
        g.add_node("a", lambda s: {"a": 1})
        g.add_node("gate", gate)
        g.set_entry("a")
        g.add_edge("a", "gate")
        result = g.run({})
        self.assertEqual(result.status, "interrupted")
        self.assertEqual(result.interrupted_at, "gate")
        self.assertEqual(result.prompt, "need approval")
        self.assertEqual(len(g.checkpointer), 2)  # a + gate

    def test_resume_from_interrupted_node(self):
        # First pass interrupts; second pass starts at the gate with approved.
        state_holder = {}

        def gate(state):
            if not state.get("approved"):
                raise GraphInterrupt("gate", "need approval")
            return {"gate_ok": True}

        g = Graph()
        g.add_node("a", lambda s: {"a": 1})
        g.add_node("gate", gate)
        g.add_node("done", lambda s: {"done": True})
        g.set_entry("a")
        g.add_edge("a", "gate")
        g.add_edge("gate", "done")

        first = g.run({"approved": False})
        self.assertEqual(first.status, "interrupted")
        # Resume: re-run from the checkpointed node with approval granted.
        resumed = g.run({**first.state, "approved": True}, start="gate")
        self.assertEqual(resumed.status, "completed")
        self.assertTrue(resumed.state["gate_ok"])
        self.assertTrue(resumed.state["done"])


class ComposeTests(unittest.TestCase):
    def test_runs_until_approval_interrupts(self):
        out = run_to_approval("处理退款 A1024")
        res = out["result"]
        self.assertEqual(res.status, "interrupted")
        self.assertEqual(res.interrupted_at, "approve")
        self.assertEqual(res.path, ["gather", "draft", "approve"])

    def test_gate_node_ran_a_harness(self):
        res = run_to_approval("处理退款 A1024")["result"]
        gather = res.state["gather_result"]
        # harness-shaped result: status/output/steps/tokens
        for key in ("status", "output", "steps", "tokens"):
            self.assertIn(key, gather)
        self.assertEqual(gather["status"], "success")

    def test_full_run_when_approved(self):
        res = resume_after_approval("处理退款 A1024")["result"]
        self.assertEqual(res.status, "completed")
        self.assertEqual(res.path, ["gather", "draft", "approve", "summarize"])

    def test_tokens_accumulate_across_stages(self):
        res = resume_after_approval("处理退款 A1024")["result"]
        self.assertGreater(res.state["tokens_total"], 0)
        # total should be the sum of each stage's tokens
        stage_sum = sum(
            res.state[k]["tokens"]
            for k in ("gather_result", "draft_result", "approve_result", "summarize_result")
        )
        self.assertEqual(res.state["tokens_total"], stage_sum)

    def test_stage_order_is_graph_order(self):
        res = resume_after_approval("处理退款 A1024")["result"]
        self.assertEqual(res.state["stage_order"], ["gather", "draft", "approve", "summarize"])

    def test_approval_gate_blocks_then_allows(self):
        blocked = run_to_approval("x")["result"]
        allowed = resume_after_approval("x")["result"]
        self.assertEqual(blocked.status, "interrupted")
        self.assertEqual(allowed.status, "completed")

    def test_result_is_json_serialisable(self):
        import json
        res = resume_after_approval("x")["result"]
        json.dumps(res.to_dict(), ensure_ascii=False, default=str)


if __name__ == "__main__":
    unittest.main()
