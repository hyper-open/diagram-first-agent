"""Tests for v9: choose topology after intent routing.

The contract under test is the DECISION, not the executor:
low-confidence -> clarify; high-risk -> escalate; single simple -> single/rag;
single complex -> plan_execute; multi independent -> parallel; multi dependent
-> chaining/supervisor; multi conflicting -> supervisor.
"""
from __future__ import annotations

import unittest

from examples.mini_agent_v9 import IntentItem, handle, select_topology


def item(text, route, intent="", conf=0.9, complexity="simple", depends=None):
    return IntentItem(text=text, route=route, intent=intent, confidence=conf,
                      complexity=complexity, depends_on=depends or [])


class SelectionRulesTests(unittest.TestCase):
    def test_low_confidence_clarifies_first(self):
        # Even though nearest intent is 'complaint', low confidence => clarify.
        items = [item("你好", "clarify", "risk.complaint", conf=0.30)]
        self.assertEqual(select_topology(items).topology, "clarify")

    def test_clarify_route_is_clarify(self):
        items = [item("嗯嗯", "clarify", "risk.complaint", conf=0.38)]
        self.assertEqual(select_topology(items).topology, "clarify")

    def test_high_risk_escalates(self):
        items = [item("我要投诉", "human", "risk.complaint", conf=0.9)]
        self.assertEqual(select_topology(items).topology, "escalate")

    def test_high_risk_wins_over_business_intent(self):
        items = [item("退款", "workflow", "refund.request"),
                 item("投诉", "human", "risk.complaint")]
        self.assertEqual(select_topology(items).topology, "escalate")

    def test_single_simple_is_single(self):
        items = [item("我要退款", "workflow", "refund.request")]
        self.assertEqual(select_topology(items).topology, "single")

    def test_single_rag_is_rag(self):
        items = [item("解释条款第三条", "rag", "doc.explain")]
        self.assertEqual(select_topology(items).topology, "rag")

    def test_single_complex_is_plan_execute(self):
        items = [item("整理成表格", "agent", "task.multi_step", complexity="complex")]
        self.assertEqual(select_topology(items).topology, "plan_execute")

    def test_multi_independent_is_parallel(self):
        items = [item("退款", "workflow", "refund.request"),
                 item("查订单", "workflow", "query.order_status")]
        self.assertEqual(select_topology(items).topology, "parallel")

    def test_multi_two_dependent_is_chaining(self):
        items = [item("查订单", "workflow", "query.order_status"),
                 item("退款", "workflow", "refund.request", depends=["query.order_status"])]
        self.assertEqual(select_topology(items).topology, "chaining")

    def test_multi_three_dependent_is_supervisor(self):
        items = [item("a", "workflow", "i1"),
                 item("b", "rag", "i2", depends=["i1"]),
                 item("c", "workflow", "i3", depends=["i2"])]
        self.assertEqual(select_topology(items).topology, "supervisor")

    def test_conflicting_is_supervisor(self):
        # refund + policy explanation can disagree -> adjudicate, not concat
        items = [item("退款", "workflow", "refund.request"),
                 item("解释政策", "rag", "doc.explain")]
        self.assertEqual(select_topology(items).topology, "supervisor")

    def test_two_writes_is_supervisor(self):
        items = [item("退款", "workflow", "refund.request"),
                 item("改地址", "workflow", "change.address")]
        self.assertEqual(select_topology(items).topology, "supervisor")

    def test_empty_is_clarify(self):
        self.assertEqual(select_topology([]).topology, "clarify")


class PipelineTests(unittest.TestCase):
    def test_refund_plus_lookup_goes_parallel(self):
        r = handle("我要退款，另外订单 A1024 也帮我查一下到哪了")
        self.assertTrue(r["is_multi"])
        self.assertEqual(r["topology"]["topology"], "parallel")

    def test_complaint_plus_tech_escalates(self):
        r = handle("这个功能报错了，而且我要投诉你们服务太差")
        self.assertEqual(r["topology"]["topology"], "escalate")

    def test_greeting_clarifies(self):
        r = handle("你好")
        self.assertEqual(r["topology"]["topology"], "clarify")

    def test_complex_single_plans(self):
        r = handle("帮我把这个月的退款整理成表格并汇总金额")
        self.assertEqual(r["topology"]["topology"], "plan_execute")

    def test_every_result_has_topology_and_execution(self):
        r = handle("解释一下退款政策")
        self.assertIn("topology", r)
        self.assertIn("result", r)
        self.assertEqual(r["result"]["topology"], r["topology"]["topology"])

    def test_decision_includes_reason(self):
        r = handle("我要退款，订单 A1024 到哪了")
        self.assertTrue(r["topology"]["reason"])

    def test_json_serialisable(self):
        import json
        json.dumps(handle("我要退款，另外查订单 A1024"), ensure_ascii=False, default=str)


if __name__ == "__main__":
    unittest.main()
