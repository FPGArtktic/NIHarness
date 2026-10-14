# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Logging must survive the stream it was configured with going away."""

import io
import json
import sys

import pytest
import structlog

from niharness.cli.logging import configure


@pytest.fixture(autouse=True)
def _configured() -> None:
    configure()


def test_records_are_json_on_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    structlog.get_logger("test").info("tool.called", name="read_file")
    record = json.loads(stream.getvalue())
    assert record["event"] == "tool.called"
    assert record["name"] == "read_file"
    assert record["level"] == "info"


def test_a_replaced_stream_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A captured stream that is later closed must not break every later log line.

    This is what a test runner does around a CLI invocation, and binding the
    stream at configuration time made every subsequent record raise on a closed
    file - in the harness, not in the test.
    """
    first = io.StringIO()
    monkeypatch.setattr(sys, "stderr", first)
    log = structlog.get_logger("test")
    log.info("before")
    first.close()

    second = io.StringIO()
    monkeypatch.setattr(sys, "stderr", second)
    log.info("after")

    assert "after" in second.getvalue()


def test_debug_records_need_verbose(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    structlog.get_logger("test").debug("quiet")
    assert stream.getvalue() == ""

    configure(verbose=True)
    structlog.get_logger("test").debug("loud")
    assert "loud" in stream.getvalue()
