"""Mini Agent v4: every multi-agent topology from the handbook, as code.

The handbook defines four topologies (see ``docs/evolution/multi-agent.md``):

    1a  Supervisor    — one leader splits work and merges results
    1b  Peer          — agents talk to each other as equals
    1c  Hierarchical  — lead -> sub-lead -> worker, multiple levels
    1d  Debate        — proposer + critic -> judge

Each is implemented here as a *different control flow over the same contract*.
That is the point: topologies differ in who decides next and who merges, not in
what a "sub-agent" is.  All four share ``contract.py`` for the task packet,
result envelope, budget inheritance and failure reporting.

Design rules kept from v1-v3:

* the model only proposes; the orchestrator owns control flow and budgets;
* a sub-agent returns **conclusion + evidence + confidence + open questions**,
  never its whole transcript;
* errors propagate as *data* (a failed result), not as exceptions, because a
  supervisor must be able to see one worker fail and still merge the rest;
* every topology returns the same ``TopologyResult`` so they are comparable.
"""
from .contract import (
    SubTask,
    SubResult,
    TopologyResult,
    BudgetTree,
    AgentRole,
    build_worker,
)
from .supervisor import SupervisorTopology
from .peer import PeerTopology
from .hierarchical import Branch, HierarchicalTopology
from .debate import DebateTopology

__all__ = [
    "SubTask",
    "SubResult",
    "TopologyResult",
    "BudgetTree",
    "AgentRole",
    "build_worker",
    "SupervisorTopology",
    "PeerTopology",
    "Branch",
    "HierarchicalTopology",
    "DebateTopology",
    "TOPOLOGIES",
]


# Registry so the CLI can iterate every topology without a hard-coded if/else.
TOPOLOGIES = {
    "supervisor": SupervisorTopology,
    "peer": PeerTopology,
    "hierarchical": HierarchicalTopology,
    "debate": DebateTopology,
}
