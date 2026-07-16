"""The terminal is a viewer of pipeline emits only (M2 finding 9): SDK
loggers — with no handlers, python logging's lastResort prints them straight
to the operator's stderr ("Fatal error in message reader: …") — are routed to
the run workspace's logs/sdk.log instead."""

from __future__ import annotations

import logging

import pytest

from deeper.orchestrator.cli import _configure_logging

from .helpers import make_workspace


def _sdk_handlers() -> list[logging.Handler]:
    return [
        h
        for h in logging.getLogger("claude_agent_sdk").handlers
        if getattr(h, "_deeper_sdk_log", False)
    ]


@pytest.fixture(autouse=True)
def _clean_sdk_logger():
    yield
    logger = logging.getLogger("claude_agent_sdk")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = True
    logger.setLevel(logging.NOTSET)


def test_sdk_records_land_in_the_workspace_not_the_terminal(tmp_path, capsys):
    ws = make_workspace(tmp_path)
    _configure_logging(ws)
    logger = logging.getLogger("claude_agent_sdk")
    assert logger.propagate is False
    assert len(_sdk_handlers()) == 1

    _configure_logging(ws)  # idempotent: same run, no duplicate handler
    assert len(_sdk_handlers()) == 1

    # The exact record class the M2 run saw spewing to the terminal.
    logging.getLogger("claude_agent_sdk._internal.query").error(
        "Fatal error in message reader: Failed to decode JSON"
    )
    text = ws.path("logs/sdk.log").read_text(encoding="utf-8")
    assert "Fatal error in message reader" in text
    assert "claude_agent_sdk._internal.query" in text  # the logger name survives
    assert capsys.readouterr().err == ""  # nothing reached the terminal


def test_configuring_a_second_workspace_repoints_the_handler(tmp_path):
    first = make_workspace(tmp_path / "one")
    second = make_workspace(tmp_path / "two")
    _configure_logging(first)
    _configure_logging(second)
    assert len(_sdk_handlers()) == 1  # re-pointed, not stacked
    logging.getLogger("claude_agent_sdk").warning("hello second run")
    assert "hello second run" in second.path("logs/sdk.log").read_text(encoding="utf-8")
    assert not first.path("logs/sdk.log").exists()  # delay=True: never touched


def test_sdk_log_file_is_not_created_until_something_logs(tmp_path):
    ws = make_workspace(tmp_path)
    _configure_logging(ws)
    assert not ws.path("logs/sdk.log").exists()
