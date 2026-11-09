"""Mini Agent v8: a Plan-Execute harness running *inside* a graph executor.

Two layers, cleanly separated:

  * ``graph.py``   — a tiny LangGraph-shaped executor: State, Node, Edge,
                     conditional edge, checkpointer, interrupt/resume.
  * ``compose.py`` — wraps each graph node as a harness run; the demo workflow
                     is plan -> execute -> approve -> summarize.
"""
from .graph import Checkpointer, Edge, Graph, GraphInterrupt, GraphResult, Node
from .compose import StageSpec, build_demo_graph, run_stage_as_node

__all__ = [
    "Graph",
    "Node",
    "Edge",
    "Checkpointer",
    "GraphResult",
    "GraphInterrupt",
    "StageSpec",
    "build_demo_graph",
    "run_stage_as_node",
]
