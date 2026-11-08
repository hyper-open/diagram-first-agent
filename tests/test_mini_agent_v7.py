"""Tests for the Plan-Execute harness (v7).

These lock the harness *contract*, not the planner's content:
budget can't be exceeded, retry only happens when allowed, exhaustion is
PARTIAL not success, verification failure triggers a bounded re-plan, and every
terminal state is recorded truthfully in the trace.
"""
from __future__ import annotations

import unittest

from examples.mini_agent.types import Budget
from examples.mini_agent_v7 import (
    ExecResult,
    Plan,
    PlanExecuteHarness,
    Step,
    contains_verifier,
    dict_executor,
    list_planner,
)


def harness(planner, executor, verifier=None, **kw):
    """Test helper.  Old tests may pass max_replans/max_attempts_per_step/budget;
    translate them into a HarnessConfig so they exercise the config path too."""
    from examples.mini_agent_v7 import HarnessConfig

    config = kw.pop("config", None)
    budget = kw.pop("budget", None)
    if config is None:
        config = HarnessConfig(
            max_steps=16, max_tool_calls=16, max_tokens=10000,
            max_replans=kw.pop("max_replans", 2),
            max_attempts_per_step=kw.pop("max_attempts_per_step", 2),
        )
    if budget is not None:
        # Caller wants an explicit budget (e.g. to test exhaustion); reflect its
        # limits into config so harness reads consistent values.
        config = HarnessConfig(
            max_steps=budget.max_steps, max_tool_calls=budget.max_tool_calls,
            max_tokens=budget.max_tokens,
            max_replans=config.max_replans,
            max_attempts_per_step=config.max_attempts_per_step,
        )
    if kw:
        raise TypeError("unexpected kwargs: %s" % list(kw))
    return PlanExecuteHarness(
        planner=planner, executor=executor, verifier=verifier,
        config=config, budget=budget,
    )


class HappyPathTests(unittest.TestCase):
    def test_runs_plan_to_success(self):
        plan = [Step("policy"), Step("order"), Step("draft")]
        execs = {
            "policy": ExecResult(True, "政策：7天内", tokens=50),
            "order": ExecResult(True, "订单已发货", tokens=50),
            "draft": ExecResult(True, "答复完成", tokens=50),
        }
        result = harness(list_planner(plan), dict_executor(execs),
                         contains_verifier(["7天内", "已发货", "答复"])).run("退款")
        self.assertEqual(result.status, "success")
        self.assertEqual(result.steps_done, ["policy", "order", "draft"])
        self.assertTrue(result.ok)

    def test_early_stop_when_verifier_passes(self):
        plan = [Step("a"), Step("b"), Step("c")]
        execs = {k: ExecResult(True, k, tokens=10) for k in "abc"}
        result = harness(list_planner(plan), dict_executor(execs),
                         contains_verifier(["a"])).run("g")
        self.assertEqual(result.status, "success")
        self.assertEqual(result.steps_done, ["a"])  # stopped after first step

    def test_empty_plan_verifies_existing(self):
        result = harness(lambda g, d, r: Plan([], "nothing"), dict_executor({}),
                         contains_verifier([])).run("g")
        self.assertEqual(result.status, "success")


class RetryTests(unittest.TestCase):
    def test_retryable_failure_is_retried(self):
        calls = {"n": 0}

        def flaky(step):
            calls["n"] += 1
            if calls["n"] < 2:
                return ExecResult(False, "timeout", retryable=True, tokens=10)
            return ExecResult(True, "ok", tokens=10)

        result = harness(list_planner([Step("s")]), flaky, contains_verifier(["ok"])).run("g")
        self.assertEqual(result.status, "success")
        self.assertEqual(calls["n"], 2)

    def test_non_retryable_failure_stops_immediately(self):
        calls = {"n": 0}

        def bad(step):
            calls["n"] += 1
            return ExecResult(False, "bad_arg", retryable=False)

        result = harness(list_planner([Step("s")]), bad).run("g")
        self.assertEqual(result.status, "failed")
        self.assertEqual(calls["n"], 1)  # no retry

    def test_retry_exhausted_is_partial(self):
        result = harness(
            list_planner([Step("s")]),
            lambda s: ExecResult(False, "timeout", retryable=True),
            max_attempts_per_step=2,
        ).run("g")
        self.assertEqual(result.status, "partial")
        self.assertIn("step_failed", result.error)


class BudgetTests(unittest.TestCase):
    def test_step_budget_bounds_real_work(self):
        many = [Step("s%d" % i) for i in range(20)]
        execs = {s.name: ExecResult(True, "ok", tokens=10) for s in many}
        result = harness(list_planner(many), dict_executor(execs),
                         contains_verifier(["never"]),
                         budget=Budget(max_steps=5, max_tool_calls=100, max_tokens=100000)).run("g")
        self.assertEqual(result.status, "partial")
        self.assertLess(len(result.steps_done), 20)

    def test_budget_exhaustion_is_not_success(self):
        # The bug we fixed: no budget to verify must not mean "verified".
        many = [Step("s%d" % i) for i in range(20)]
        execs = {s.name: ExecResult(True, "ok", tokens=1000) for s in many}
        result = harness(list_planner(many), dict_executor(execs),
                         contains_verifier(["absent"]),
                         budget=Budget(max_steps=4, max_tool_calls=100, max_tokens=100000)).run("g")
        self.assertNotEqual(result.status, "success")

    def test_token_budget_is_respected(self):
        many = [Step("s%d" % i) for i in range(50)]
        execs = {s.name: ExecResult(True, "ok", tokens=5000) for s in many}
        result = harness(list_planner(many), dict_executor(execs),
                         budget=Budget(max_steps=100, max_tool_calls=100, max_tokens=12000)).run("g")
        self.assertLessEqual(result.tokens, 20000)  # some slack for verify/plan


class ReplanTests(unittest.TestCase):
    def test_verify_failure_triggers_replan(self):
        def planner(goal, done, rnd):
            if rnd == 1:
                return Plan([Step("a")], "init")
            if rnd == 2:
                return Plan([Step("b")], "corrective")
            return Plan([], "done")

        execs = {"a": ExecResult(True, "部分", tokens=10),
                 "b": ExecResult(True, "补齐", tokens=10)}
        result = harness(planner, dict_executor(execs),
                         contains_verifier(["补齐"]), max_replans=3).run("g")
        self.assertEqual(result.status, "success")
        self.assertEqual(result.rounds, 2)
        self.assertEqual(result.steps_done, ["a", "b"])

    def test_replan_cap_produces_partial(self):
        result = harness(
            lambda g, d, r: Plan([Step("x%d" % r)], "loop"),
            lambda s: ExecResult(True, "never", tokens=10),
            contains_verifier(["impossible"]),
            budget=Budget(max_steps=30, max_tool_calls=30),
            max_replans=2,
        ).run("g")
        self.assertEqual(result.status, "partial")
        self.assertIn("verify_failed", result.error)


class TraceTests(unittest.TestCase):
    def test_trace_records_all_phases(self):
        plan = [Step("a"), Step("b")]
        execs = {"a": ExecResult(True, "x", tokens=10), "b": ExecResult(True, "y", tokens=10)}
        result = harness(list_planner(plan), dict_executor(execs),
                         contains_verifier(["y"])).run("g")
        phases = [t["phase"] for t in result.trace]
        self.assertEqual(phases[0], "plan")
        self.assertIn("execute", phases)
        self.assertTrue(any(p in phases for p in ("verify", "early_stop")))

    def test_failed_step_is_traced(self):
        result = harness(list_planner([Step("s")]),
                         lambda s: ExecResult.failure("err", retryable=False)).run("g")
        execs = [t for t in result.trace if t["phase"] == "execute"]
        self.assertEqual(len(execs), 1)
        self.assertFalse(execs[0]["ok"])
        self.assertEqual(execs[0]["error"], "err")

    def test_result_is_json_serialisable(self):
        import json

        result = harness(list_planner([Step("a")]),
                         dict_executor({"a": ExecResult(True, "x", tokens=5)}),
                         contains_verifier(["x"])).run("g")
        json.dumps(result.to_dict(), ensure_ascii=False)


class ContractTests(unittest.TestCase):
    def test_planner_executor_verifier_are_injected(self):
        # The harness must not hard-code any of them: swap in fakes and it works.
        seen = {}

        def planner(goal, done, rnd):
            seen["goal"] = goal
            return Plan([Step("only", input=goal)], "r1")

        def executor(step):
            seen["step_input"] = step.input
            return ExecResult(True, "done", tokens=1)

        result = harness(planner, executor, contains_verifier(["done"])).run("我的目标")
        self.assertEqual(seen["goal"], "我的目标")
        self.assertEqual(seen["step_input"], "我的目标")
        self.assertEqual(result.status, "success")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Configuration: the harness must read its knobs from config, not hard-code them
# ---------------------------------------------------------------------------


class ConfigTests(unittest.TestCase):
    def test_defaults_are_valid(self):
        from examples.mini_agent_v7 import HarnessConfig

        cfg = HarnessConfig()
        cfg.validate()  # should not raise
        self.assertEqual(cfg.max_steps, 16)

    def test_env_parsing(self):
        from examples.mini_agent_v7 import HarnessConfig

        cfg = HarnessConfig.from_env({
            "HARNESS_MAX_STEPS": "5",
            "HARNESS_MAX_REPLANS": "0",
            "HARNESS_EARLY_STOP": "false",
            "HARNESS_PROFILE": "test",
        })
        self.assertEqual(cfg.max_steps, 5)
        self.assertEqual(cfg.max_replans, 0)
        self.assertFalse(cfg.early_stop)
        self.assertEqual(cfg.profile, "test")

    def test_bad_int_names_the_variable(self):
        from examples.mini_agent_v7 import ConfigError, HarnessConfig

        with self.assertRaises(ConfigError) as ctx:
            HarnessConfig.from_env({"HARNESS_MAX_STEPS": "abc"})
        self.assertIn("HARNESS_MAX_STEPS", str(ctx.exception))

    def test_bad_bool_is_rejected(self):
        from examples.mini_agent_v7 import ConfigError, HarnessConfig

        with self.assertRaises(ConfigError):
            HarnessConfig.from_env({"HARNESS_EARLY_STOP": "maybe"})

    def test_validate_rejects_impossible_values(self):
        from examples.mini_agent_v7 import ConfigError, HarnessConfig

        with self.assertRaises(ConfigError):
            HarnessConfig(max_steps=0).validate()
        with self.assertRaises(ConfigError):
            HarnessConfig(max_attempts_per_step=0).validate()

    def test_from_dict_rejects_unknown_keys(self):
        from examples.mini_agent_v7 import ConfigError, HarnessConfig

        with self.assertRaises(ConfigError) as ctx:
            HarnessConfig.from_dict({"max_steps": 3, "typo_key": 1})
        self.assertIn("typo_key", str(ctx.exception))

    def test_profiles_exist_and_are_copied(self):
        from examples.mini_agent_v7 import PROFILES, for_scenario

        self.assertIn("interactive", PROFILES)
        a = for_scenario("batch")
        a.max_steps = 999  # mutate the copy
        b = for_scenario("batch")
        self.assertNotEqual(b.max_steps, 999)  # shared profile not corrupted

    def test_unknown_profile_is_rejected(self):
        from examples.mini_agent_v7 import ConfigError, for_scenario

        with self.assertRaises(ConfigError):
            for_scenario("nope")

    def test_early_stop_flag_controls_behaviour(self):
        # This is the whole point: the SAME harness behaves differently because
        # config says so — not because code branches on a hard-coded constant.
        from examples.mini_agent_v7 import HarnessConfig

        plan = [Step("a"), Step("b"), Step("c"), Step("d")]
        execs = {k: ExecResult(True, k, tokens=10) for k in "abcd"}

        fast = harness(list_planner(plan), dict_executor(execs),
                       contains_verifier(["a"]),
                       config=HarnessConfig(early_stop=True)).run("g")
        full = harness(list_planner(plan), dict_executor(execs),
                       contains_verifier(["a"]),
                       config=HarnessConfig(early_stop=False)).run("g")
        self.assertEqual(fast.steps_done, ["a"])
        self.assertEqual(full.steps_done, ["a", "b", "c", "d"])

    def test_max_replans_comes_from_config(self):
        from examples.mini_agent_v7 import HarnessConfig

        cfg = HarnessConfig(max_steps=50, max_replans=1)
        result = harness(
            lambda g, d, r: Plan([Step("x%d" % r)], "loop"),
            lambda s: ExecResult(True, "never", tokens=1),
            contains_verifier(["impossible"]),
            config=cfg,
        ).run("g")
        # initial round + 1 replan = 2 rounds, then stop
        self.assertEqual(result.rounds, 2)
        self.assertEqual(result.status, "partial")

    def test_trace_can_be_disabled(self):
        from examples.mini_agent_v7 import HarnessConfig

        result = harness(list_planner([Step("a")]),
                         dict_executor({"a": ExecResult(True, "x")}),
                         contains_verifier(["x"]),
                         config=HarnessConfig(trace_enabled=False)).run("g")
        self.assertEqual(result.trace, [])
        self.assertEqual(result.status, "success")  # still works, just no trace

    def test_budget_built_from_config(self):
        from examples.mini_agent_v7 import HarnessConfig

        cfg = HarnessConfig(max_steps=3)
        h = harness(list_planner([Step("a")]),
                    dict_executor({"a": ExecResult(True, "x")}),
                    config=cfg)
        self.assertEqual(h.budget.max_steps, 3)
