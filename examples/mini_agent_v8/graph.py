"""A tiny, dependency-free graph executor modelled on LangGraph's core ideas.

Why hand-roll this instead of importing LangGraph?

This project has a hard "standard library only" rule, so the demo cannot depend
on ``langgraph``.  What we *can* do is reproduce its **shape** faithfully enough
that the integration is real, not a mock:

  * ``State``        — a shared, serialisable dict threaded through nodes
  * ``Node``         — ``(state) -> partial_update`` (a pure-ish unit)
  * ``Edge``         — static next-node, plus a ``router`` for conditional edges
  * ``Checkpointer`` — snapshots state after every node (pause / resume / replay)
  * ``interrupt``    — a node can request human approval and halt the graph

The important design constraint mirrors LangGraph: **a node never mutates shared
state directly**; it returns an update that the executor merges. That is what
makes checkpointing and time-travel possible.
"""
from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# A node takes the current state and returns a partial update (merged into it).
NodeFn = Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]
# A router looks at state and returns the name of the next node.
RouterFn = Callable[[Dict[str, Any]], str]


@dataclass
class Node:
    name: str
    fn: NodeFn


@dataclass
class Edge:
    """Static edge or conditional edge.

    ``router`` present  -> conditional: use its return value as next node.
    ``router`` absent   -> static: go to ``to``.
    """

    src: str
    to: str = ""
    router: Optional[RouterFn] = None


class Checkpointer:
    """In-memory snapshots (LangGraph calls this a checkpointer). Kept simple;
    a real one writes to SQLite/Postgres and supports time-travel."""

    def __init__(self) -> None:
        self._snapshots: List[Dict[str, Any]] = []

    def save(self, step: int, node: str, state: Dict[str, Any]) -> None:
        self._snapshots.append({
            "step": step, "node": node,
            "state": copy.deepcopy(state), "at": time.time(),
        })

    def latest(self) -> Optional[Dict[str, Any]]:
        return copy.deepcopy(self._snapshots[-1]) if self._snapshots else None

    def history(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._snapshots)

    def __len__(self) -> int:
        return len(self._snapshots)


class GraphInterrupt(Exception):
    """Raised by a node that needs human approval before continuing.

    Mirrors LangGraph's ``interrupt``: the graph stops at this node, state is
    checkpointed, and a resume can continue from the same place.
    """

    def __init__(self, node: str, prompt: str):
        super().__init__("interrupt at %s: %s" % (node, prompt))
        self.node = node
        self.prompt = prompt


@dataclass
class GraphResult:
    status: str  # completed | interrupted | failed | max_steps
    state: Dict[str, Any]
    path: List[str]
    steps: int
    interrupted_at: str = ""
    prompt: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "path": list(self.path),
            "steps": self.steps,
            "interrupted_at": self.interrupted_at,
            "prompt": self.prompt,
            "error": self.error,
            "state": self.state,
        }


class Graph:
    """A minimal StateGraph: add nodes, wire edges, run from an entry node."""

    def __init__(self, checkpointer: Optional[Checkpointer] = None):
        self.nodes: Dict[str, Node] = {}
        self.edges: Dict[str, Edge] = {}
        self.entry: str = ""
        # NOTE: do not write ``checkpointer or Checkpointer()`` — Checkpointer
        # defines __len__, so an empty one is falsy and would be silently
        # replaced, dropping the caller's checkpoints. (A real bug we hit.)
        self.checkpointer = checkpointer if checkpointer is not None else Checkpointer()

    # -- building ---------------------------------------------------------

    def add_node(self, name: str, fn: NodeFn) -> "Graph":
        self.nodes[name] = Node(name, fn)
        return self

    def add_edge(self, src: str, to: str) -> "Graph":
        self.edges[src] = Edge(src, to=to)
        return self

    def add_conditional_edge(self, src: str, router: RouterFn) -> "Graph":
        self.edges[src] = Edge(src, router=router)
        return self

    def set_entry(self, name: str) -> "Graph":
        self.entry = name
        return self

    # -- running ----------------------------------------------------------

    def run(
        self,
        initial_state: Dict[str, Any],
        *,
        max_steps: int = 30,
        start: Optional[str] = None,
    ) -> GraphResult:
        """Execute from ``start`` (or entry) with ``initial_state``.

        Passing ``start`` plus a previously checkpointed state is exactly how
        "resume after interrupt" works.
        """
        state = copy.deepcopy(initial_state)
        current = start or self.entry
        path: List[str] = []
        steps = 0

        if not current:
            return GraphResult("failed", state, path, steps, error="no entry node")

        while current:
            if steps >= max_steps:
                return GraphResult("max_steps", state, path, steps)

            node = self.nodes.get(current)
            if node is None:
                return GraphResult("failed", state, path, steps,
                                   error="unknown node: %s" % current)

            try:
                update = node.fn(state)
            except GraphInterrupt as exc:
                # Checkpoint BEFORE halting so a resume can pick up here.
                self.checkpointer.save(steps, current, state)
                path.append(current)
                return GraphResult("interrupted", state, path, steps,
                                   interrupted_at=exc.node, prompt=exc.prompt)

            # Merge node update into shared state (nodes never mutate directly).
            if update:
                state.update(update)
            self.checkpointer.save(steps, current, state)
            path.append(current)
            steps += 1

            # Route to next node.
            edge = self.edges.get(current)
            if edge is None:
                break  # terminal node
            if edge.router is not None:
                nxt = edge.router(state)
                if nxt in ("", "END", None):
                    break
                current = nxt
            else:
                current = edge.to

        return GraphResult("completed", state, path, steps)

    def resume(self, run_id_state: Dict[str, Any], resume_at: str) -> GraphResult:
        """Convenience: continue from a checkpoint at ``resume_at``."""
        return self.run(run_id_state, start=resume_at)


# ---------------------------------------------------------------------------
# Helpers used by the demo
# ---------------------------------------------------------------------------


def pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
