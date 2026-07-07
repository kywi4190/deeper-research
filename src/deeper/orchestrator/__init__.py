"""Deterministic orchestrator — the state machine that owns pipeline process.

Code decides process (stage sequencing, gates, budgets, stop rules); LLM
subagents decide content (design P8). `engine` is the machine, `gates` the
file-edit pause states, `rerun` surgical invalidation, `cli` the `deeper`
entry point. See docs/deeper-research-design.md §5, §8.
"""

from .engine import GATE_NODES, Engine, EngineError, Node, node_of
from .gates import GATE_AFTER_STAGE, GATE_SPECS, GateOutcome, GateSpec, read_decision
from .rerun import STAGE_ARTIFACTS, RerunError, invalidate

__all__ = [
    "GATE_AFTER_STAGE",
    "GATE_NODES",
    "GATE_SPECS",
    "STAGE_ARTIFACTS",
    "Engine",
    "EngineError",
    "GateOutcome",
    "GateSpec",
    "Node",
    "RerunError",
    "invalidate",
    "node_of",
    "read_decision",
]
