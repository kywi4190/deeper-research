"""Design-§11 security claims as executable policy (the prompt-injection
mitigation bullet): these tests pin the tool fences, the preference-quarantine
allowlist, and the no-write-scope rule so that a future contract or constant
change that loosens any of them fails the suite instead of shipping silently.

If one of these fails, that is the point — loosening the policy is a deliberate
design change, made here first."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeper.agents_runtime.contracts import AGENTS_DIR, AgentContract, load_role
from deeper.agents_runtime.dispatch import (
    DISALLOWED_TOOLS,
    NON_RESEARCH_TOOLS,
    RESEARCH_TOOLS,
)
from deeper.agents_runtime.hooks import (
    PREFERENCE_READERS,
    PROTECTED_FILES,
    quarantine_gate,
    write_scope_gate,
)
from deeper.config import SizeClass, profile_config
from deeper.schemas import Stage
from deeper.workspace import Workspace

SRC_ROOT = Path(__file__).parents[1] / "src" / "deeper"

ALL_ROLES = sorted(p.stem for p in AGENTS_DIR.glob("*.md"))

# §6/§11 role sets, pinned. A role changing sides is a security-relevant design
# decision: it must be made HERE (with the design doc updated), never as a
# side effect of editing frontmatter.
EXPECTED_RESEARCH_ROLES = {
    "interviewer",
    "cartographer-first-principles",
    "cartographer-analogist",
    "cartographer-contrarian",
    "cartographer-practitioner",
    "cartographer-taxonomist",
    "cartographer-horizon",
    "scout",
    "card-critic",
    "screener",
    "analyst",
    "verifier",
    "prosecutor",
    "steelman",
    "frame-checker",
}
EXPECTED_NON_RESEARCH_ROLES = {
    "merger",
    "rubric-builder",
    "judge",
    "synthesist",
    "eval-judge",
}


# -- §11: "none has Bash" — and no subagent spawning either -----------------------


def test_no_tool_allowlist_carries_bash_or_subagents() -> None:
    for forbidden in ("Bash", "Task", "Agent"):
        assert forbidden in DISALLOWED_TOOLS, f"{forbidden} must stay disallowed"
        assert forbidden not in RESEARCH_TOOLS
        assert forbidden not in NON_RESEARCH_TOOLS


def test_research_role_set_is_pinned() -> None:
    actual_research = set()
    actual_non_research = set()
    for role in ALL_ROLES:
        meta, _ = load_role(role)
        (actual_research if meta.get("research") else actual_non_research).add(role)
    assert actual_research == EXPECTED_RESEARCH_ROLES
    assert actual_non_research == EXPECTED_NON_RESEARCH_ROLES


def test_live_options_fence_every_role(tmp_path: Path) -> None:
    """The actual option set each live subagent gets: allowed tools per the
    research flag, Bash/Task/Agent disallowed, dontAsk permission mode, the
    hook events registered. Skipped when the SDK is not installed — the
    constants above are then the (still-tested) source of truth."""
    pytest.importorskip("claude_agent_sdk")
    from deeper.agents_runtime.dispatch import LiveDispatcher

    ws = Workspace.create(tmp_path / "run", profile_config("quick"))
    dispatcher = LiveDispatcher(ws, ws.load_config())
    for role in ALL_ROLES:
        meta, _ = load_role(role)
        contract = AgentContract(
            role=role,
            stage=Stage.S1,
            output_schemas=tuple(meta.get("output_schemas") or ()),
            size_class=SizeClass.S,
            budget_line="policy probe",
        )
        options = dispatcher._live_options(contract)
        expected = RESEARCH_TOOLS if meta.get("research") else NON_RESEARCH_TOOLS
        assert options.allowed_tools == expected, role
        assert set(options.disallowed_tools) >= {"Bash", "Task", "Agent"}, role
        assert options.permission_mode == "dontAsk", role
        assert set(options.hooks) >= {"PreToolUse", "PostToolUse"}, role


# -- §11: "no research agent has Write access outside its own artifact directory" --
# In this implementation the bar is stricter: agents emit artifacts inside
# their replies and the ORCHESTRATOR writes files, so no contract grants any
# write path at all. The hook denies every agent write until a contract says
# otherwise — and no code in src/ may say otherwise without editing this test.


def test_no_contract_in_src_grants_write_paths() -> None:
    offenders = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if path.name in {"contracts.py", "hooks.py"}:  # field decl + the gate itself
            continue
        if "allowed_write_paths" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(SRC_ROOT)))
    assert not offenders, (
        "these modules touch allowed_write_paths — granting an agent a write "
        f"scope is a §11 policy change; update this test deliberately: {offenders}"
    )


def test_default_contract_write_scope_is_empty_and_denied(tmp_path: Path) -> None:
    ws = Workspace.create(tmp_path / "run", profile_config("quick"))
    contract = AgentContract(
        role="scout",
        stage=Stage.S3,
        output_schemas=("option-card-set",),
        size_class=SizeClass.M,
        budget_line="b",
    )
    assert contract.allowed_write_paths == ()
    denial = write_scope_gate(
        "Write", {"file_path": "options/x/cards.yaml"}, ws.root, contract.allowed_write_paths
    )
    assert denial is not None and "(none)" in denial


# -- §6: the preference-quarantine allowlist is exact -----------------------------


def test_preference_reader_allowlist_is_exactly_screener_and_synthesist() -> None:
    assert PREFERENCE_READERS == frozenset({"screener", "synthesist"})


def test_every_other_role_is_denied_preferences(tmp_path: Path) -> None:
    ws = Workspace.create(tmp_path / "run", profile_config("quick"))
    for role in ALL_ROLES:
        denial = quarantine_gate(role, "Read", {"file_path": "preferences.yaml"}, ws.root)
        if role in PREFERENCE_READERS:
            assert denial is None, role
        else:
            assert denial is not None, f"{role} must not read preferences.yaml"


def test_run_control_files_are_protected_from_every_agent() -> None:
    assert PROTECTED_FILES >= {"state.json", "config.yaml", "preferences.yaml"}


# -- §6: prompt-level defense — every research prompt carries the untrusted rule --


def test_every_research_prompt_carries_the_untrusted_web_rule() -> None:
    for role in sorted(EXPECTED_RESEARCH_ROLES):
        _, body = load_role(role)
        assert "untrusted" in body.lower(), (
            f"agents/{role}.md is research-capable but its body never says "
            "fetched content is untrusted (design §6 source hygiene)"
        )


def test_only_screener_and_synthesist_prompts_declare_preferences_input() -> None:
    for role in ALL_ROLES:
        meta, _ = load_role(role)
        inputs = meta.get("inputs") or []
        declares = any("preferences" in str(i) for i in inputs)
        if role in PREFERENCE_READERS:
            assert declares, f"{role} should declare the preferences input"
        else:
            assert not declares, f"{role} must not declare preferences as an input"
