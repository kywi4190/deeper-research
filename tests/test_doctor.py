"""`deeper doctor` — live-run preflight over this repo's real prompts/schemas."""

from __future__ import annotations

from typer.testing import CliRunner

from deeper.orchestrator.cli import app

runner = CliRunner()

CHECKS = ("API key", "Agent SDK", "Config profiles", "Agent prompts", "Schema exports")


def test_doctor_runs_every_check_and_passes_on_this_repo(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    for check in CHECKS:
        assert check in result.output
    assert "prompts parse" in result.output
    assert "exports fresh" in result.output
    assert "claude-agent-sdk" in result.output  # version printed


def test_doctor_warns_without_api_key_but_still_passes(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "warn" in result.output
    assert "ANTHROPIC_API_KEY not set" in result.output


def test_doctor_fails_on_stale_schema_exports(monkeypatch):
    import deeper.schemas.export as export

    monkeypatch.setattr(export, "check", lambda *a, **k: ["stale: rubric.schema.json"])
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "make schemas" in result.output


def test_doctor_fails_when_a_prompt_is_broken(monkeypatch, tmp_path):
    import deeper.agents_runtime.contracts as contracts

    broken = tmp_path / "agents"
    broken.mkdir()
    (broken / "scout.md").write_text("no frontmatter at all", encoding="utf-8")
    monkeypatch.setattr(contracts, "AGENTS_DIR", broken)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "frontmatter" in result.output
