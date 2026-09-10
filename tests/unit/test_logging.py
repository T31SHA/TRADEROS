"""Tests for structured logging primitives."""

import json
import logging

from traderos.core.logging import JsonFormatter, configure_logging


def test_json_formatter_contains_core_fields() -> None:
    record = logging.LogRecord(
        name="traderos.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )

    payload = json.loads(JsonFormatter().format(record))

    assert payload["logger"] == "traderos.test"
    assert payload["level"] == "INFO"
    assert payload["message"] == "hello world"
    assert payload["timestamp"].endswith("+00:00")


def test_configure_logging_sets_requested_level() -> None:
    configure_logging("WARNING")

    assert logging.getLogger().level == logging.WARNING
    assert len(logging.getLogger().handlers) == 1
