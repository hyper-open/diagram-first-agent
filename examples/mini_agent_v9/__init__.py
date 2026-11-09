"""Mini Agent v9: choose the topology AFTER intent routing.

The chain: user text (maybe multi-intent) -> intent routing -> topology
selection -> execution.  This package focuses on the *selection* step: given the
detected intents and their relationships, which topology (single / parallel /
chaining / supervisor / plan_execute / escalate / clarify) should run?
"""
from .select_topology import (
    IntentItem,
    TopologyChoice,
    ExecOutcome,
    select_topology,
    execute,
)
from .pipeline import handle, text_to_intents

__all__ = [
    "IntentItem",
    "TopologyChoice",
    "ExecOutcome",
    "select_topology",
    "execute",
    "handle",
    "text_to_intents",
]
