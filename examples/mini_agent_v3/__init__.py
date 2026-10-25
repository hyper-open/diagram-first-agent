"""Mini Agent v3: v2 + tool gateway, metering, audit log and persistence.

v2 answered "which path should this request take?".  v3 answers the questions a
platform has to answer:

  * **Who is allowed to do this?**  Short-lived, least-privilege capability
    tokens bound to a real user — never to the agent.
  * **What did it cost, and to whom?**  An immutable usage ledger with
    idempotent events, so retries cannot be billed twice.
  * **What actually happened?**  An append-only audit log plus a durable run
    store, so a crashed process can resume and an incident can be replayed.
  * **Can we ship it?**  A release record and a preflight gate that fails
    loudly.

Everything builds on v1/v2; nothing is forked.
"""
from .audit import AuditEvent, AuditLog
from .billing import Ledger, UsageEvent, price_for, to_usage_event
from .capability import Capability, CapabilityError, CapabilityIssuer
from .gateway import ToolGateway, GatewayResult
from .persistence import FileStore, RunState
from .gated import GatedPipeline, GatedRequest

__all__ = [
    "AuditEvent",
    "AuditLog",
    "Ledger",
    "UsageEvent",
    "price_for",
    "to_usage_event",
    "Capability",
    "CapabilityError",
    "CapabilityIssuer",
    "ToolGateway",
    "GatewayResult",
    "FileStore",
    "RunState",
    "GatedPipeline",
    "GatedRequest",
]
