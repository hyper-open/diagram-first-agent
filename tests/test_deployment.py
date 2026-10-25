"""Tests for configuration, the HTTP surface, and the deployment switches.

These tests do not need Docker.  They exercise the same code paths the container
runs, on an ephemeral port, so a broken config or a leaking ``/health`` fails in
CI rather than in a container log nobody reads.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from examples.mini_agent_v3.app import build_app
from examples.mini_agent_v3.config import ConfigError, Settings
from examples.mini_agent_v3.server import Metrics, make_server


class ConfigTests(unittest.TestCase):
    def test_defaults_are_valid(self):
        settings = Settings.from_env({})
        self.assertEqual(settings.port, 8000)
        self.assertEqual(settings.env, "dev")
        self.assertFalse(settings.readonly)

    def test_bad_int_names_the_variable(self):
        with self.assertRaises(ConfigError) as ctx:
            Settings.from_env({"MINI_AGENT_PORT": "abc"})
        self.assertIn("MINI_AGENT_PORT", str(ctx.exception))

    def test_port_range_is_checked(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({"MINI_AGENT_PORT": "70000"})
        with self.assertRaises(ConfigError):
            Settings.from_env({"MINI_AGENT_PORT": "-1"})

    def test_port_zero_means_os_assigned(self):
        # test-only spelling for "free port"; the server reports the real one.
        self.assertEqual(Settings.from_env({"MINI_AGENT_PORT": "0"}).port, 0)

    def test_bool_switch_is_strict(self):
        self.assertTrue(Settings.from_env({"MINI_AGENT_READONLY": "yes"}).readonly)
        with self.assertRaises(ConfigError):
            Settings.from_env({"MINI_AGENT_READONLY": "maybe"})

    def test_tenant_limits_are_parsed(self):
        settings = Settings.from_env({"TENANT_LIMITS": "acme:5,globex:2.5"})
        self.assertEqual(settings.tenant_limits["acme"], 5.0)
        self.assertEqual(settings.tenant_limits["globex"], 2.5)

    def test_malformed_tenant_limit_is_rejected(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({"TENANT_LIMITS": "acme"})
        with self.assertRaises(ConfigError):
            Settings.from_env({"TENANT_LIMITS": "acme:lots"})

    def test_allowlisted_tenant_must_have_a_limit(self):
        # An allowlisted tenant with no limit is a billing hole.
        with self.assertRaises(ConfigError) as ctx:
            Settings.from_env({"TENANT_ALLOWLIST": "acme,ghost", "TENANT_LIMITS": "acme:5"})
        self.assertIn("ghost", str(ctx.exception))

    def test_prod_without_api_key_fails_fast(self):
        with self.assertRaises(ConfigError) as ctx:
            Settings.from_env({"MINI_AGENT_ENV": "prod"})
        self.assertIn("OPENAI_API_KEY", str(ctx.exception))

    def test_prod_with_api_key_is_accepted(self):
        settings = Settings.from_env({"MINI_AGENT_ENV": "prod", "OPENAI_API_KEY": "sk-test"})
        self.assertEqual(settings.env, "prod")

    def test_unknown_env_is_rejected(self):
        with self.assertRaises(ConfigError):
            Settings.from_env({"MINI_AGENT_ENV": "production"})

    def test_public_dict_never_leaks_the_key(self):
        settings = Settings.from_env({"OPENAI_API_KEY": "sk-super-secret-value"})
        payload = json.dumps(settings.public_dict(), ensure_ascii=False)
        self.assertNotIn("sk-super-secret-value", payload)
        self.assertTrue(settings.public_dict()["live_model"])

    def test_tenant_allowed_and_limits(self):
        settings = Settings.from_env(
            {"TENANT_LIMITS": "acme:5", "TENANT_ALLOWLIST": "acme", "DEFAULT_TENANT_LIMIT_USD": "7"}
        )
        self.assertTrue(settings.tenant_allowed("acme"))
        self.assertFalse(settings.tenant_allowed("evil"))
        self.assertEqual(settings.limit_for("unknown"), 7.0)


class ServerTests(unittest.TestCase):
    """Spin up the real HTTP server on an ephemeral port."""

    def _start(self, env=None, data_dir=None):
        env = dict(env or {})
        env.setdefault("MINI_AGENT_PORT", "0")  # ask the OS for a free port
        env.setdefault("MINI_AGENT_DATA_DIR", data_dir or tempfile.mkdtemp(prefix="v3_srv_"))
        env.setdefault("TENANT_LIMITS", "acme:5,globex:2")
        settings = Settings.from_env(env)
        app = build_app(settings)
        server = make_server(settings, app)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:%d" % port, settings

    def _call(self, base, method, path, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_health_returns_safe_config(self):
        base, _ = self._start({"OPENAI_API_KEY": "sk-should-not-appear"})
        code, body = self._call(base, "GET", "/health")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertNotIn("sk-should-not-appear", json.dumps(body))

    def test_answer_endpoint_returns_flat_envelope(self):
        base, _ = self._start()
        code, body = self._call(
            base, "POST", "/v1/answer",
            {"text": "订单 A1024 到哪了", "tenant": "acme", "user_id": "user:88"},
        )
        self.assertEqual(code, 200)
        for key in ("request_id", "trace_id", "route", "status", "output", "cost_usd"):
            self.assertIn(key, body)

    def test_unlisted_tenant_is_rejected_before_work(self):
        base, _ = self._start({"TENANT_ALLOWLIST": "acme"})
        code, body = self._call(
            base, "POST", "/v1/answer", {"text": "hi", "tenant": "evil", "user_id": "user:1"}
        )
        self.assertEqual(code, 403)
        self.assertEqual(body["status"], "tenant_not_allowed")

    def test_empty_text_is_a_client_error(self):
        base, _ = self._start()
        code, _ = self._call(base, "POST", "/v1/answer", {"text": "   ", "tenant": "acme"})
        self.assertEqual(code, 400)

    def test_malformed_json_is_a_client_error(self):
        base, _ = self._start()
        request = urllib.request.Request(
            base + "/v1/answer", data=b"{not json", method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_oversized_body_is_rejected(self):
        base, settings = self._start({"MINI_AGENT_MAX_BODY_BYTES": "1024"})
        big = "x" * (settings.max_body_bytes + 100)
        code, body = self._call(base, "POST", "/v1/answer", {"text": big, "tenant": "acme"})
        self.assertEqual(code, 413)
        self.assertEqual(body["status"], "too_large")

    def test_unknown_path_is_404(self):
        base, _ = self._start()
        code, _ = self._call(base, "GET", "/nope")
        self.assertEqual(code, 404)

    def test_readonly_switch_refuses_write_intent(self):
        base, _ = self._start({"MINI_AGENT_READONLY": "true"})
        code, body = self._call(
            base, "POST", "/v1/answer",
            {"text": "帮我退款 A1024", "tenant": "acme", "user_id": "user:88"},
        )
        self.assertEqual(code, 403)
        self.assertEqual(body["status"], "readonly")

    def test_agent_subject_is_denied_over_http(self):
        base, _ = self._start()
        code, body = self._call(
            base, "POST", "/v1/answer",
            {"text": "订单 B2048 到哪了", "tenant": "acme", "user_id": "agent:support"},
        )
        self.assertEqual(code, 403)
        self.assertEqual(body["status"], "permission_denied")

    def test_usage_and_metrics_reflect_traffic(self):
        base, _ = self._start()
        self._call(base, "POST", "/v1/answer",
                   {"text": "解释一下退款时效条款", "tenant": "acme", "user_id": "user:88"})
        _, usage = self._call(base, "GET", "/v1/usage")
        self.assertIn("acme", usage["usage"])
        _, metrics = self._call(base, "GET", "/metrics")
        self.assertIn("mini_agent_requests_total", metrics["prometheus"])

    def test_guard_disabled_is_audited(self):
        data_dir = tempfile.mkdtemp(prefix="v3_noguard_")
        self._start({"MINI_AGENT_GUARD": "false"}, data_dir=data_dir)
        log = (
            __import__("pathlib").Path(data_dir) / "audit.jsonl"
        ).read_text(encoding="utf-8")
        self.assertIn("guard_disabled_by_config", log)


class MetricsTests(unittest.TestCase):
    def test_denials_are_counted(self):
        metrics = Metrics()
        metrics.observe("permission_denied", "workflow", 3)
        metrics.observe("success", "faq", 5, cost_usd=0.001)
        rendered = metrics.render()
        self.assertIn("mini_agent_denials_total 1", rendered)
        self.assertIn("mini_agent_requests_total 2", rendered)
        self.assertIn('mini_agent_status_total{status="permission_denied"} 1', rendered)


class DeploymentFileTests(unittest.TestCase):
    """The Docker files are part of the deliverable, so lint the cheap parts."""

    def test_dockerfile_runs_non_root_and_has_healthcheck(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("USER appuser", dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertIn("python:3.11-slim", dockerfile)
        self.assertIn("examples.mini_agent_v3.app", dockerfile)

    def test_compose_declares_a_readonly_instance(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("MINI_AGENT_READONLY", compose)
        self.assertIn("8000:8000", compose)

    def test_dockerignore_excludes_docs_and_data(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        ignored = (root / ".dockerignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("docs/", ignored)
        self.assertIn(".data/", ignored)


if __name__ == "__main__":
    unittest.main()
