"""The measurement layer (design §10, P10): five property metrics over a
completed run's workspace, an LLM-judge for the one semantic metric (breadth),
and the report/compare machinery behind `deeper eval`.

Module split mirrors the pipeline's own discipline: `metrics` is pure
deterministic math over loaded artifacts; `judge` assembles the eval-judge
contract and dispatches it through the SAME agent runtime as every pipeline
role (mockable, cost-tracked under stage EVAL); `runner` loads a workspace
tolerantly and orchestrates one eval; `report` renders the markdown views.
"""

from .benchmarks import BENCHMARKS_DIR, BaselineNotPasted, EvalError, load_benchmark
from .runner import EVAL_MD_PATH, EVAL_YAML_PATH, compare_path, evaluate_run, render_compare

__all__ = [
    "BENCHMARKS_DIR",
    "BaselineNotPasted",
    "EVAL_MD_PATH",
    "EVAL_YAML_PATH",
    "EvalError",
    "compare_path",
    "evaluate_run",
    "load_benchmark",
    "render_compare",
]
