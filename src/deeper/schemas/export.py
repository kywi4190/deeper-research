"""JSON Schema exports for every file-level artifact.

The exported files in schemas/ get inlined into agent prompts (design §8), so
they must always match the Pydantic models: `python -m deeper.schemas.export`
regenerates them, `--check` (and the test suite) fails when they are stale.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .allocation import AllocationTable
from .angles import AngleMap, CartographerReport, CoverageReport
from .base import ArtifactModel
from .common import ContradictionLedger, SourceRecord
from .dossier import DeepDiveRoundLog, Dossier, VerificationReport
from .gates import GateADecision, GateBDecision, GateCDecision
from .intake import Brief, DestinationModel, Preferences
from .options import CardCritique, OptionCardSet
from .report import DecisionReport
from .rubric import Rubric
from .runstate import RunState
from .screening import ScreeningResult, Shortlist
from .tournament import FrameCheck, Prosecution, ScoreUpdateLog, Steelman

# Workspace-file artifact name -> model. Nested models appear in each schema's $defs.
ARTIFACT_REGISTRY: dict[str, type[ArtifactModel]] = {
    "brief": Brief,
    "destination": DestinationModel,
    "preferences": Preferences,
    "cartographer-report": CartographerReport,
    "angle-map": AngleMap,
    "coverage-report": CoverageReport,
    "allocation": AllocationTable,
    "option-card-set": OptionCardSet,
    "card-critique": CardCritique,
    "rubric": Rubric,
    "screening-result": ScreeningResult,
    "shortlist": Shortlist,
    "dossier": Dossier,
    "verification-report": VerificationReport,
    "deep-dive-log": DeepDiveRoundLog,
    "prosecution": Prosecution,
    "steelman": Steelman,
    "frame-check": FrameCheck,
    "score-update-log": ScoreUpdateLog,
    "decision-report": DecisionReport,
    "gate-a-decision": GateADecision,
    "gate-b-decision": GateBDecision,
    "gate-c-decision": GateCDecision,
    "source-record": SourceRecord,
    "contradiction-ledger": ContradictionLedger,
    "run-state": RunState,
}

# src/deeper/schemas/export.py -> repo root / schemas
DEFAULT_SCHEMAS_DIR = Path(__file__).resolve().parents[3] / "schemas"


def render_schema(model: type[ArtifactModel]) -> str:
    return json.dumps(model.model_json_schema(), indent=2) + "\n"


def expected_files() -> dict[str, str]:
    return {
        f"{name}.schema.json": render_schema(model) for name, model in ARTIFACT_REGISTRY.items()
    }


def export_all(schemas_dir: Path = DEFAULT_SCHEMAS_DIR) -> list[Path]:
    """Write every schema; remove orphaned exports for renamed/deleted artifacts."""
    schemas_dir.mkdir(parents=True, exist_ok=True)
    expected = expected_files()
    written: list[Path] = []
    for fname, content in expected.items():
        path = schemas_dir / fname
        path.write_text(content, encoding="utf-8", newline="\n")
        written.append(path)
    for path in schemas_dir.glob("*.schema.json"):
        if path.name not in expected:
            path.unlink()
    return written


def check(schemas_dir: Path = DEFAULT_SCHEMAS_DIR) -> list[str]:
    """Return problems (missing/stale/orphaned exports); empty list means fresh."""
    expected = expected_files()
    problems: list[str] = []
    for fname, content in expected.items():
        path = schemas_dir / fname
        if not path.exists():
            problems.append(f"missing: {fname}")
        elif path.read_text(encoding="utf-8") != content:
            problems.append(f"stale: {fname}")
    if schemas_dir.exists():
        for path in schemas_dir.glob("*.schema.json"):
            if path.name not in expected:
                problems.append(f"orphaned: {path.name}")
    return sorted(problems)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export artifact JSON Schemas.")
    parser.add_argument("--check", action="store_true", help="verify exports are fresh")
    parser.add_argument("--dir", type=Path, default=DEFAULT_SCHEMAS_DIR)
    args = parser.parse_args(argv)
    if args.check:
        problems = check(args.dir)
        if problems:
            print("schemas/ exports are out of date — run `make schemas`:")
            for p in problems:
                print(f"  {p}")
            return 1
        print(f"schemas/ exports are fresh ({len(ARTIFACT_REGISTRY)} artifacts).")
        return 0
    written = export_all(args.dir)
    print(f"wrote {len(written)} schema files to {args.dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
