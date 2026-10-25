"""A standard-library HTTP surface for the v3 platform.

This is deliberately a **teaching server**, not a production one.  The
``http.server`` implementation is single-process and has no TLS, no HTTP/2, no
connection pooling and no protection against slowloris beyond a socket timeout.
The value is that every *application-level* decision is visible in one file:

  * tenant resolution and allowlisting happen before any work;
  * the read-only switch is enforced at the gateway, not in prompts;
  * every response carries the same trace/cost/status envelope;
  * ``/health`` never leaks the API key;
  * oversized bodies are rejected before they are read into memory.

Replace ``ThreadingHTTPServer`` with gunicorn/uvicorn + a real ASGI app when you
need production properties; the handlers below map 1:1 onto those routes.

Endpoints::

    GET  /health          liveness + safe config summary
    GET  /v1/tenants      configured tenants and their limits
    GET  /v1/usage        per-tenant spend (from the ledger)
    POST /v1/answer       {"text": "...", "tenant": "...", "user_id": "user:1"}
    GET  /metrics         Prometheus-style counters
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple

from .config import ConfigError, Settings

MAX_BODY = 64 * 1024


def _json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class Metrics:
    """Counters and a tiny histogram.  Enough to answer 'is it healthy?'."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests = 0
        self.by_status: Dict[str, int] = {}
        self.by_route: Dict[str, int] = {}
        self.denials = 0
        self.cost_usd = 0.0
        self.latency_ms_sum = 0

    def observe(self, status: str, route: str, latency_ms: int, cost_usd: float = 0.0) -> None:
        with self._lock:
            self.requests += 1
            self.by_status[status] = self.by_status.get(status, 0) + 1
            self.by_route[route] = self.by_route.get(route, 0) + 1
            self.latency_ms_sum += latency_ms
            self.cost_usd += cost_usd
            if status in ("permission_denied", "quota_exceeded", "blocked"):
                self.denials += 1

    def render(self) -> str:
        lines = [
            "# HELP mini_agent_requests_total Total handled requests",
            "# TYPE mini_agent_requests_total counter",
            "mini_agent_requests_total %d" % self.requests,
            "# TYPE mini_agent_denials_total counter",
            "mini_agent_denials_total %d" % self.denials,
            "# TYPE mini_agent_cost_usd_total counter",
            "mini_agent_cost_usd_total %.8f" % self.cost_usd,
            "# TYPE mini_agent_latency_ms_sum counter",
            "mini_agent_latency_ms_sum %d" % self.latency_ms_sum,
        ]
        for status, count in sorted(self.by_status.items()):
            lines.append('mini_agent_status_total{status="%s"} %d' % (status, count))
        for route, count in sorted(self.by_route.items()):
            lines.append('mini_agent_route_total{route="%s"} %d' % (route, count))
        return "\n".join(lines) + "\n"


class App:
    """Framework-free application object; the HTTP layer only adapts to it."""

    def __init__(
        self,
        settings: Settings,
        *,
        answer: Optional[Callable[[str, str, str], Dict[str, Any]]] = None,
        usage: Optional[Callable[[], Dict[str, float]]] = None,
    ):
        self.settings = settings
        self.metrics = Metrics()
        self._answer = answer
        self._usage = usage

    # -- routes ----------------------------------------------------------

    def handle(self, method: str, path: str, body: bytes, headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        started = time.monotonic()
        status_code, payload = self._dispatch(method, path, body, headers)
        elapsed = int((time.monotonic() - started) * 1000)
        payload.setdefault("latency_ms", elapsed)
        route = payload.get("route", path)
        state = payload.get("status", "ok" if status_code < 400 else "error")
        self.metrics.observe(state, route, elapsed, float(payload.get("cost_usd", 0.0) or 0.0))
        return status_code, payload

    def _dispatch(self, method: str, path: str, body: bytes, headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
        if method == "GET" and path == "/health":
            return 200, {"ok": True, "service": "mini-agent-v3", "config": self.settings.public_dict()}
        if method == "GET" and path == "/metrics":
            return 200, {"ok": True, "prometheus": self.metrics.render()}
        if method == "GET" and path == "/v1/tenants":
            return 200, {
                "ok": True,
                "tenants": self.settings.tenant_limits,
                "allowlist": self.settings.tenant_allowlist or "all",
            }
        if method == "GET" and path == "/v1/usage":
            if self._usage is None:
                return 200, {"ok": True, "usage": {}}
            return 200, {"ok": True, "usage": self._usage()}
        if method == "POST" and path == "/v1/answer":
            return self._answer_endpoint(body)
        return 404, {"ok": False, "status": "not_found", "error": "未知路径：%s" % path}

    def _answer_endpoint(self, body: bytes) -> Tuple[int, Dict[str, Any]]:
        if len(body) > self.settings.max_body_bytes:
            return 413, {"ok": False, "status": "too_large",
                         "error": "请求体超过 %d 字节" % self.settings.max_body_bytes}
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return 400, {"ok": False, "status": "bad_request", "error": "JSON 解析失败：%s" % exc}
        if not isinstance(payload, dict):
            return 400, {"ok": False, "status": "bad_request", "error": "请求体必须是对象"}

        text = str(payload.get("text", "")).strip()
        tenant = str(payload.get("tenant", self.settings.default_tenant))
        user_id = str(payload.get("user_id", "user:anonymous"))
        if not text:
            return 400, {"ok": False, "status": "bad_request", "error": "text 不能为空"}

        # Tenant allowlist is checked *before* any work: an unknown tenant must
        # not be able to spend money or learn whether it exists.
        if not self.settings.tenant_allowed(tenant):
            return 403, {"ok": False, "status": "tenant_not_allowed", "error": "该租户未开通"}
        if tenant not in self.settings.tenant_limits:
            return 403, {"ok": False, "status": "unknown_tenant", "error": "该租户未配置额度"}

        if self._answer is None:
            return 503, {"ok": False, "status": "unavailable", "error": "治理层未就绪"}

        # Read-only is a *deployment* switch: refuse write-capable asks outright.
        if self.settings.readonly and _looks_write_action(text):
            return 403, {"ok": False, "status": "readonly", "error": "当前实例为只读模式，拒绝写操作"}

        try:
            result = self._answer(text, tenant, user_id)
        except Exception as exc:  # noqa: BLE001 - never leak stack traces to clients
            return 500, {"ok": False, "status": "error", "error": "内部错误：%s" % type(exc).__name__}

        code = 200
        status = result.get("status", "success")
        if status == "permission_denied":
            code = 403
        elif status == "quota_exceeded":
            code = 429
        payload_out = {"ok": code == 200, **result}
        return code, payload_out


def _looks_write_action(text: str) -> bool:
    keywords = ("退款", "删除", "转账", "发送邮件", "修改地址")
    return any(word in text for word in keywords)


def make_server(settings: Settings, app: App) -> ThreadingHTTPServer:
    """Build a server; ``app`` is wrapped in one handler class."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "MiniAgentV3/0.1"
        sys_version = ""

        def log_message(self, fmt, *args):  # keep test output quiet
            if settings.env != "dev":
                return

        def _read_body(self) -> bytes:
            length_header = self.headers.get("Content-Length", "0")
            try:
                length = int(length_header)
            except ValueError:
                length = 0
            if length > settings.max_body_bytes:
                return b"\x00" * (settings.max_body_bytes + 1)  # triggers 413
            return self.rfile.read(length) if length > 0 else b""

        def _respond(self, method: str) -> None:
            path = self.path.split("?", 1)[0]
            body = self._read_body() if method == "POST" else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            try:
                code, payload = app.handle(method, path, body, headers)
            except Exception as exc:  # noqa: BLE001
                code, payload = 500, {"ok": False, "status": "error",
                                      "error": "内部错误：%s" % type(exc).__name__}
            data = _json_bytes(payload)
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            self._respond("GET")

        def do_POST(self):  # noqa: N802
            self._respond("POST")

    server = ThreadingHTTPServer((settings.host, settings.port), Handler)
    server.daemon_threads = True
    return server


__all__ = ["App", "Metrics", "make_server", "ConfigError"]
