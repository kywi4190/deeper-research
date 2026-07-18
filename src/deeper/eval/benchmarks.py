"""Benchmark spec loading and the baseline-answer slot (design §10).

Specs live in the repo's benchmarks/ directory, one YAML per question, and
validate against BenchmarkSpec. Each spec may name a baseline file — a place
to paste a plain Deep Research answer to the same question — which stays a
commented placeholder until the user pastes one; `eval --compare-baseline`
refuses a placeholder loudly instead of judging boilerplate.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from deeper.agents_runtime.contracts import REPO_ROOT
from deeper.schemas import BenchmarkSpec, format_validation_error

BENCHMARKS_DIR = REPO_ROOT / "benchmarks"


class EvalError(Exception):
    """An eval request that cannot be honored (missing spec, missing report)."""


class BaselineNotPasted(EvalError):
    """The spec's baseline slot is still the placeholder — nothing to judge."""


def find_spec(name_or_path: str | Path, benchmarks_dir: Path = BENCHMARKS_DIR) -> Path:
    """Resolve a benchmark by literal path first, then as benchmarks/<id>.yaml."""
    candidates = [Path(name_or_path), benchmarks_dir / f"{name_or_path}.yaml"]
    path = next((c for c in candidates if c.is_file()), None)
    if path is None:
        known = sorted(p.stem for p in benchmarks_dir.glob("*.yaml"))
        raise EvalError(
            f"no benchmark spec found for {str(name_or_path)!r} "
            f"(tried {', '.join(str(c) for c in candidates)}); "
            f"available: {', '.join(known) or 'none'}"
        )
    return path


def load_benchmark(
    name_or_path: str | Path, benchmarks_dir: Path = BENCHMARKS_DIR
) -> tuple[BenchmarkSpec, Path]:
    """(validated spec, the spec file's path — baseline files resolve against it)."""
    path = find_spec(name_or_path, benchmarks_dir)
    try:
        return BenchmarkSpec.from_yaml_file(path), path
    except yaml.YAMLError as err:
        raise EvalError(f"{path} is not valid YAML: {err}") from err
    except ValidationError as err:
        raise EvalError(f"{path}:\n{format_validation_error(err, BenchmarkSpec)}") from err


def read_baseline(spec: BenchmarkSpec, spec_path: Path) -> str:
    """The pasted Deep Research answer, or a loud refusal while the file is
    still the placeholder (nothing but comments/blank lines)."""
    if not spec.baseline_file:
        raise EvalError(f"benchmark {spec.id!r} declares no baseline_file slot")
    path = (spec_path.parent / spec.baseline_file).resolve()
    if not path.is_file():
        raise EvalError(f"baseline file missing: {path}")
    text = path.read_text(encoding="utf-8")
    # Placeholder = nothing but comments: strip <!-- --> blocks (the shipped
    # placeholder is one) and markdown comment-ish '#'-only lines, then look
    # for any surviving content.
    without_html_comments = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    content_lines = [
        line
        for line in without_html_comments.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not content_lines:
        raise BaselineNotPasted(
            f"{path} is still the placeholder — paste a plain Deep Research answer "
            "to the benchmark question below its header, then rerun with "
            "--compare-baseline"
        )
    return text
