"""Mini Agent v6: Arena and Swarm/Handoff — two topologies the book was missing.

The first five rounds of code covered:
  v4 — supervisor, peer, hierarchical, debate (multi-agent topologies)
  v5 — chaining, routing, parallel, orchestrator, evaluator (orchestration patterns)

Browsing a common "8 multi-agent patterns" list turned up two more that do not
map onto any of the above:

  Arena          — run N candidate answers, then rank them by **pairwise**
                   battles.  Different from Debate: debate is 1 proposer vs 1
                   critic with a single verdict; arena has many candidates and
                   produces an ordering, not one answer.
  Swarm/Handoff  — decentralised control transfer.  No supervisor; any agent
                   can hand the conversation to another, and there is an
                   explicit terminal "handoff to a human" path.  Different from
                   Peer: peer is a conversation between fixed participants; a
                   swarm is a *relay* where the active agent changes.

Both reuse ``mini_agent_v4.contract`` (SubResult / BudgetTree) so all six rounds
of code share one notion of a result and a budget.
"""
from .arena import Arena, Candidate, Battle
from .swarm import Swarm, HandoffAgent, Handoff

__all__ = [
    "Arena",
    "Candidate",
    "Battle",
    "Swarm",
    "HandoffAgent",
    "Handoff",
    "TOPOLOGIES",
]

TOPOLOGIES = {
    "arena": Arena,
    "swarm": Swarm,
}
