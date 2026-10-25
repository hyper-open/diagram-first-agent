"""Process entry point: wire settings -> v3 components -> HTTP server.

    python3 -m examples.mini_agent_v3.app            # reads environment
    MINI_AGENT_PORT=8080 python3 -m examples.mini_agent_v3.app

The wiring is the interesting part: **configuration determines capabilities**.
A read-only deployment does not get write tools at all (not "write tools that
check a flag"), which is the same principle as ACL filtering before ranking.
"""
from __future__ import annotations

import signal
import sys
import threading
from pathlib import Path
from typing import Any, Dict

from ..mini_agent.guard import Guard
from ..mini_agent_v2.corpus import InMemoryCorpus
from ..mini_agent_v2.fixtures import seed_documents
from ..mini_agent_v2.memory_hygiene import MemoryStore
from ..mini_agent_v2.retrieval import HybridRetriever
from ..mini_agent_v2.router import IntentRouter, default_intents
from .audit import AuditLog
from .billing import Ledger, Quota
from .capability import CapabilityIssuer
from .config import ConfigError, Settings
from .gated import GatedPipeline, GatedRequest
from .server import App, make_server


class Service:
    """Everything the HTTP layer needs, assembled from ``Settings``."""

    def __init__(self, settings: Settings):
        self.settings = settings
        data_dir = Path(settings.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)

        self.audit = AuditLog(str(data_dir / "audit.jsonl"))
        self.ledger = Ledger(str(data_dir / "ledger.json"))
        self.issuer = CapabilityIssuer()
        # A disabled guard is an explicit deployment decision, and it is
        # recorded as such below — never a silent default.
        self.guard = Guard() if settings.guard_enabled else None

        retriever = HybridRetriever(InMemoryCorpus(seed_documents()))
        retriever.build()

        self.pipeline = GatedPipeline(
            router=IntentRouter(default_intents()),
            retriever=retriever,
            memory=MemoryStore(),
            issuer=self.issuer,
            audit=self.audit,
            ledger=self.ledger,
            quota=Quota(
                self.ledger,
                monthly_limits=settings.tenant_limits,
                default_limit=settings.default_limit_usd,
            ),
            model=settings.model,
        )
        if not settings.guard_enabled:
            self.audit.record(
                "startup",
                actor="service:app",
                decision="allow",
                reason="guard_disabled_by_config",
            )

    # -- used by App ------------------------------------------------------

    def answer(self, text: str, tenant: str, user_id: str) -> Dict[str, Any]:
        result = self.pipeline.handle(
            GatedRequest(
                text=text,
                tenant_id=tenant,
                user_id=user_id,
                principals={"public"},
            )
        )
        # The HTTP envelope is flat on purpose: dashboards parse one shape.
        return {
            "request_id": result.get("request_id"),
            "trace_id": result.get("trace_id"),
            "route": result.get("route"),
            "status": result.get("status"),
            "output": result.get("output"),
            "citations": result.get("citations", []),
            "cost_usd": result.get("cost_usd", 0.0),
            "latency_ms": result.get("latency_ms", 0),
        }

    def usage(self) -> Dict[str, float]:
        return self.ledger.total_by_tenant()


def build_app(settings: Settings) -> App:
    service = Service(settings)
    return App(settings, answer=service.answer, usage=service.usage)


def main(argv=None) -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        # Exit non-zero with the variable name: container orchestrators surface
        # this in their own logs, which is where operators look first.
        print("配置错误：%s" % exc, file=sys.stderr)
        return 2

    app = build_app(settings)
    server = make_server(settings, app)
    url = "http://%s:%d" % (settings.host, settings.port)
    print("mini-agent-v3 已启动：%s  env=%s  readonly=%s  live_model=%s"
          % (url, settings.env, settings.readonly, bool(settings.api_key)))

    def _shutdown(signum, frame):  # noqa: ARG001
        # Graceful stop: finish the in-flight request, then close the socket.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        print("已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
