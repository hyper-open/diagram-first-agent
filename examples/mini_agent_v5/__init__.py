"""Mini Agent v5: the five orchestration patterns from the handbook.

``docs/layers/orchestration.md`` (§ 二) defines five *basic* orchestration
patterns.  They answer "what shape is the control flow?" — a different question
from v4's "how are multiple agents arranged?".

    1  Prompt Chaining        A -> B -> C   (fixed sequence, checkpoints between)
    2  Routing                one of N branches, chosen per request
    3  Parallelization        fan out -> merge (sharding OR multi-perspective)
    4  Orchestrator-Workers   plan -> dispatch -> merge -> maybe re-plan
    5  Evaluator-Optimizer    generate -> evaluate -> (loop back) | accept

The overlaps are deliberate and worth naming:

* ``Parallelization`` vs v4's ``Supervisor``: the supervisor *decides* the
  split dynamically; parallelization applies a fixed fan-out ("split these N
  documents"), with no planner.
* ``Evaluator-Optimizer`` vs v4's ``Debate``: both have an evaluator, but
  evaluator-optimizer **loops until a criterion passes**, while debate decides
  once between two positions.

Everything here reuses ``mini_agent_v4.contract`` for budgets and results, so
the two packages share one definition of "what a sub-agent returns".
"""
from .contract import (
    StageResult,
    PatternResult,
    StopReason,
    Stage,
    RunContext,
)
from .chaining import PromptChaining
from .routing import Routing
from .parallel import Parallelization
from .orchestrator_workers import OrchestratorWorkers
from .evaluator_optimizer import EvaluatorOptimizer

__all__ = [
    "StageResult",
    "PatternResult",
    "StopReason",
    "Stage",
    "RunContext",
    "PromptChaining",
    "Routing",
    "Parallelization",
    "OrchestratorWorkers",
    "EvaluatorOptimizer",
    "PATTERNS",
]

PATTERNS = {
    "chaining": PromptChaining,
    "routing": Routing,
    "parallel": Parallelization,
    "orchestrator": OrchestratorWorkers,
    "evaluator": EvaluatorOptimizer,
}
