"""Tests for Mini Agent v5 — the five orchestration patterns.

What is actually worth testing here is not "does it return text", but the
properties the handbook claims each pattern has (and the ways they go wrong):

* chaining stops at a failed checkpoint instead of producing a bad answer;
* routing never sends an uncertain request into a specific business flow;
* parallel reports partial coverage instead of silently dropping a shard;
* orchestrator cannot loop forever re-planning;
* evaluator feeds back the *reason*, and running out of rounds is PARTIAL.
"""
from __future__ import annotations

import unittest

from examples.mini_agent_v5 import PATTERNS
from examples.mini_agent_v5.chaining import PromptChaining
from examples.mini_agent_v5.contract import Stage, StopReason, new_budget
from examples.mini_agent_v5.evaluator_optimizer import EvaluatorOptimizer
from examples.mini_agent_v5.orchestrator_workers import OrchestratorWorkers
from examples.mini_agent_v5.parallel import Parallelization
from examples.mini_agent_v5.routing import Branch, Routing


class Boom(Stage):
    def __call__(self, text, ctx):
        raise RuntimeError("stage exploded")


class TerminalStateTests(unittest.TestCase):
    def test_stop_reason_enum_has_five_states(self):
        # The orchestration chapter defines exactly five terminal states.
        self.assertEqual(
            {s.value for s in StopReason},
            {"success", "partial", "blocked", "failed", "needs_human"},
        )

    def test_only_success_is_ok(self):
        for reason in StopReason:
            self.assertEqual(reason.ok, reason is StopReason.SUCCESS)


class ChainingTests(unittest.TestCase):
    def test_happy_path_runs_all_stages(self):
        result = PromptChaining().run("写退款说明")
        self.assertEqual(result.stop_reason, StopReason.SUCCESS)
        self.assertEqual(result.stages, 4)

    def test_check_stage_does_not_corrupt_content(self):
        # The bug we fixed: a check stage's verdict was concatenated into the
        # next stage's input.
        result = PromptChaining().run("写退款说明")
        self.assertNotIn("检查：", result.answer)

    def test_failed_checkpoint_returns_partial(self):
        stages = [
            Stage("gen", "初稿：{input}"),
            Stage("chk", "检查", role="check", passes_when=lambda s: False),
            Stage("rev", "终稿：{input}"),
        ]
        result = PromptChaining(stages=stages).run("任务")
        self.assertEqual(result.stop_reason, StopReason.PARTIAL)
        self.assertEqual(result.stages, 2)  # stopped at the failed check
        self.assertIn("初稿", result.answer)

    def test_late_stage_error_is_partial_not_crash(self):
        stages = [Stage("gen", "x：{input}"), Boom("boom", "y")]
        result = PromptChaining(stages=stages).run("任务")
        # prompt chaining treats a broken stage as a failed checkpoint/partial
        self.assertIn(result.stop_reason, (StopReason.PARTIAL, StopReason.FAILED))


class RoutingTests(unittest.TestCase):
    def test_routes_refund(self):
        result = Routing().run("我要申请退款")
        self.assertIn("退款流程", result.answer)
        self.assertEqual(result.stages, 1)  # only one branch runs

    def test_routes_tech(self):
        result = Routing().run("网站报错 404 打不开")
        self.assertIn("技术排查", result.answer)

    def test_low_confidence_goes_to_neutral_fallback(self):
        # Must not be routed into a specific business flow like complaints.
        result = Routing().run("今天天气怎么样")
        self.assertIn("转人工", result.answer)
        self.assertNotIn("投诉", result.answer)

    def test_missing_input_is_blocked_with_question(self):
        routing = Routing(
            branches=[Branch("order", Stage("order", "查单：{input}"),
                             lambda t: 0.9, requires=["订单号"])],
            fallback="order",
        )
        result = routing.run("我要查订单")
        self.assertEqual(result.stop_reason, StopReason.BLOCKED)
        self.assertTrue(result.questions)

    def test_no_fallback_low_confidence_needs_human(self):
        routing = Routing(
            branches=[Branch("refund", Stage("refund", "退款：{input}"), lambda t: 0.1)],
            fallback=None,
            confidence_floor=0.5,
        )
        result = routing.run("含糊")
        self.assertEqual(result.stop_reason, StopReason.NEEDS_HUMAN)


class ParallelTests(unittest.TestCase):
    def test_sharding_concatenates(self):
        result = Parallelization(mode="sharding").run("三份文档")
        self.assertEqual(result.stop_reason, StopReason.SUCCESS)
        self.assertEqual(result.stages, 3)
        self.assertEqual(result.answer.count("- ["), 3)

    def test_perspective_majority_vote(self):
        result = Parallelization(mode="perspectives").run("是否退款")
        self.assertEqual(result.stop_reason, StopReason.SUCCESS)
        self.assertIn("一致", result.answer)  # 2/3 agree

    def test_perspective_no_majority_is_honest(self):
        shards = [Stage("a", "方案X：{input}"), Stage("b", "方案Y：{input}"),
                  Stage("c", "方案Z：{input}")]
        result = Parallelization(mode="perspectives", shards=shards).run("g")
        self.assertIn("无多数", result.answer)

    def test_failed_shard_yields_partial_and_names_it(self):
        shards = [Stage("ok1", "A：{input}"), Boom("bad", "B：{input}"), Stage("ok2", "C：{input}")]
        result = Parallelization(shards=shards).run("g")
        self.assertEqual(result.stop_reason, StopReason.PARTIAL)
        self.assertIn("bad", result.answer)

    def test_all_shards_failed_is_failed(self):
        result = Parallelization(shards=[Boom("a", "x"), Boom("b", "y")]).run("g")
        self.assertEqual(result.stop_reason, StopReason.FAILED)

    def test_parallel_respects_budget(self):
        result = Parallelization().run("任务", budget=new_budget(max_tokens=1000, max_steps=10))
        self.assertLessEqual(result.tokens_used, 1000)


class OrchestratorTests(unittest.TestCase):
    def test_replans_then_finishes(self):
        result = OrchestratorWorkers().run("修 bug")
        self.assertEqual(result.stop_reason, StopReason.SUCCESS)
        plans = [t for t in result.trace if t.get("step") == "plan"]
        self.assertEqual(len(plans), 2)  # two rounds

    def test_bounded_by_max_rounds(self):
        # A planner that always asks for more work must still terminate.
        result = OrchestratorWorkers(
            max_rounds=3,
            planner=lambda g, i, done: ["retrieve"],
        ).run("任务")
        self.assertLessEqual(result.stages, 3)

    def test_empty_round_is_partial_not_success(self):
        result = OrchestratorWorkers(workers={"e": Stage("e", "")},
                                     planner=lambda g, i, d: ["e"]).run("g")
        self.assertEqual(result.stop_reason, StopReason.PARTIAL)

    def test_no_plan_at_all_is_failed(self):
        result = OrchestratorWorkers(planner=lambda g, i, d: []).run("g")
        self.assertEqual(result.stop_reason, StopReason.FAILED)


class EvaluatorTests(unittest.TestCase):
    def test_loops_until_pass(self):
        result = EvaluatorOptimizer().run("写退款说明")
        self.assertEqual(result.stop_reason, StopReason.SUCCESS)
        evals = [t for t in result.trace if t.get("step") == "evaluate"]
        self.assertGreaterEqual(len(evals), 2)
        self.assertTrue(evals[-1]["passed"])

    def test_reason_is_fed_back(self):
        # The 2nd draft must mention it was rejected, i.e. the reason travelled.
        result = EvaluatorOptimizer().run("写退款说明")
        self.assertIn("未通过", result.answer)

    def test_never_passes_is_partial(self):
        evaluator = Stage("ev", "e", role="check", passes_when=lambda s: False)
        result = EvaluatorOptimizer(evaluator=evaluator, max_rounds=2).run("g")
        self.assertEqual(result.stop_reason, StopReason.PARTIAL)
        self.assertIn("最大轮次", result.answer)

    def test_generator_failure_is_failed(self):
        result = EvaluatorOptimizer(generator=Boom("gen", "x")).run("g")
        self.assertEqual(result.stop_reason, StopReason.FAILED)


class CrossPatternTests(unittest.TestCase):
    def test_all_patterns_return_same_shape(self):
        keys = None
        for name, cls in PATTERNS.items():
            with self.subTest(pattern=name):
                payload = cls().run("任务").to_dict()
                if keys is None:
                    keys = set(payload)
                self.assertEqual(set(payload), keys)
                self.assertEqual(payload["pattern"], name)
                self.assertIn(payload["stop_reason"],
                              {s.value for s in StopReason})

    def test_all_patterns_succeed_on_healthy_input(self):
        for name, cls in PATTERNS.items():
            with self.subTest(pattern=name):
                result = cls().run("处理退款申请")
                self.assertTrue(result.ok, "%s 未成功: %s" % (name, result.stop_reason))

    def test_every_pattern_reports_tokens(self):
        for name, cls in PATTERNS.items():
            with self.subTest(pattern=name):
                self.assertGreater(cls().run("任务").tokens_used, 0)


if __name__ == "__main__":
    unittest.main()
