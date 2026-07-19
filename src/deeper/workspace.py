"""Run workspace: the plain-file state of a run (design §7).

Each run is a directory tree that is also a git repository: every stage
completion and gate decision is one commit, so "what changed after my Gate C
feedback?" is a `git diff`. All artifact I/O goes through the schema layer —
a file that does not validate never reaches disk, and a corrupted file never
reaches a stage.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from .config import RunConfig
from .schemas import ArtifactModel, RunState, RunStatus, Stage

# Design §7 tree. brief.md, allocation.yaml, rubric.yaml etc. are files that
# stages write into this skeleton; angles/raw/ holds per-cartographer output.
RUN_DIRS = (
    "angles",
    "angles/raw",
    "gates",
    "options",
    "screening",
    "dossiers",
    "tournament",
    "report",
    "sources",
    "ledger",
    "logs",
)

CONFIG_FILE = "config.yaml"
STATE_FILE = "state.json"

A = TypeVar("A", bound=ArtifactModel)


class WorkspaceError(Exception):
    """A workspace that is missing, malformed, or being misused."""


def _atomic_write(target: Path, text: str) -> None:
    """Write via a same-directory temp file + rename, so a crash mid-write can
    never leave a truncated artifact where a valid one is expected."""
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# Per-run log rotation (design §11 ops): append-only logs are bounded so a
# long or pathological run cannot grow one file without limit. 5MB × 3 backups
# is ~4 orders of magnitude above a normal run's line volume.
LOG_ROTATE_MAX_BYTES = 5 * 1024 * 1024
LOG_ROTATE_BACKUPS = 3


def append_log_line(
    target: Path,
    line: str,
    *,
    max_bytes: int = LOG_ROTATE_MAX_BYTES,
    backups: int = LOG_ROTATE_BACKUPS,
) -> None:
    """Append one line to a per-run log, rotating target -> target.1 -> …
    -> target.<backups> (oldest dropped) when the file would exceed max_bytes."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if target.stat().st_size >= max_bytes:
            for i in range(backups - 1, 0, -1):
                older = target.with_name(f"{target.name}.{i}")
                if older.exists():
                    older.replace(target.with_name(f"{target.name}.{i + 1}"))
            target.replace(target.with_name(f"{target.name}.1"))
    except OSError:  # missing file (fresh log) or a racing rotation: append anyway
        pass
    with target.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


class Workspace:
    """One run directory: schema-checked artifact I/O + git audit trail."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    @property
    def run_id(self) -> str:
        return self.root.name

    # -- lifecycle --------------------------------------------------------------

    @classmethod
    def create(cls, root: str | Path, config: RunConfig) -> Workspace:
        """Create the §7 tree, git-init it, write config + initial state, and
        make the run's first commit."""
        root = Path(root)
        if root.exists() and any(root.iterdir()):
            raise WorkspaceError(f"run directory already exists and is not empty: {root}")
        root.mkdir(parents=True, exist_ok=True)
        ws = cls(root)
        for d in RUN_DIRS:
            (ws.root / d).mkdir(parents=True, exist_ok=True)
        ws._git("init", "-q")
        # Runs must commit regardless of the machine's git identity/signing setup.
        ws._git("config", "user.name", "deeper")
        ws._git("config", "user.email", "deeper@localhost")
        ws._git("config", "commit.gpgsign", "false")
        ws._git("config", "core.autocrlf", "false")
        ws.write_artifact(CONFIG_FILE, config)
        ws.save_state(
            RunState(
                run_id=ws.run_id,
                profile=config.profile,
                stage=Stage.S0,
                status=RunStatus.RUNNING,
                updated_at=datetime.now(UTC),
            )
        )
        ws.commit(f"run created (profile={config.profile})")
        return ws

    @classmethod
    def open(cls, root: str | Path) -> Workspace:
        """Open an existing run; resumability check = state.json must validate."""
        ws = cls(root)
        if not ws.root.is_dir():
            raise WorkspaceError(f"run directory does not exist: {root}")
        missing = [p for p in (".git", CONFIG_FILE, STATE_FILE) if not (ws.root / p).exists()]
        if missing:
            raise WorkspaceError(f"not a run workspace ({root} is missing {missing})")
        ws.load_state()
        return ws

    # -- paths ------------------------------------------------------------------

    def path(self, relpath: str | Path) -> Path:
        p = (self.root / relpath).resolve()
        if not p.is_relative_to(self.root):
            raise WorkspaceError(f"path escapes the run workspace: {relpath}")
        return p

    # -- artifacts (all I/O goes through the schema layer) -----------------------

    def write_artifact(
        self, relpath: str | Path, artifact: ArtifactModel, commit_message: str | None = None
    ) -> Path:
        """Validate → dump (.json → JSON, anything else → YAML) → atomic write,
        optionally followed by one commit. Re-validation catches models mutated
        after construction — nothing invalid ever reaches disk."""
        target = self.path(relpath)
        validated = type(artifact).model_validate(artifact.model_dump(mode="json"))
        text = validated.dump_json_str() if target.suffix == ".json" else validated.dump_yaml()
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, text)
        if commit_message is not None:
            self.commit(commit_message)
        return target

    def read_artifact(self, relpath: str | Path, model: type[A]) -> A:
        """Load and validate; a corrupted or schema-violating file raises
        (pydantic.ValidationError / yaml.YAMLError) instead of entering a stage."""
        target = self.path(relpath)
        if not target.is_file():
            raise WorkspaceError(f"artifact not found: {relpath}")
        if target.suffix == ".json":
            return model.from_json_file(target)
        return model.from_yaml_file(target)

    # -- state.json ---------------------------------------------------------------

    def load_state(self) -> RunState:
        return self.read_artifact(STATE_FILE, RunState)

    def save_state(self, state: RunState, commit_message: str | None = None) -> None:
        self.write_artifact(STATE_FILE, state, commit_message)

    def load_config(self) -> RunConfig:
        return self.read_artifact(CONFIG_FILE, RunConfig)

    # -- git ---------------------------------------------------------------------

    def commit(self, message: str) -> str | None:
        """Stage everything and commit; returns the short hash, or None when
        there is nothing to commit (so callers can commit unconditionally)."""
        self._git("add", "-A")
        if not self._git("status", "--porcelain").strip():
            return None
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "--short", "HEAD").strip()

    def history(self) -> list[str]:
        """Commit subjects, newest first — the run's audit trail."""
        return self._git("log", "--format=%s").splitlines()

    def _git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if proc.returncode != 0:
            raise WorkspaceError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout
