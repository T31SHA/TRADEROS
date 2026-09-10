"""Tests for configuration and the live safety gate."""

import pytest
from pydantic import ValidationError

from traderos.core.config import Settings, TradingMode, get_settings
from traderos.core.errors import ConfigurationError


def test_defaults_are_research_and_not_live() -> None:
    settings = Settings(_env_file=None)

    assert settings.trading_mode is TradingMode.RESEARCH
    assert settings.live_trading_enabled is False
    assert settings.live_execution_permitted is False


def test_live_mode_requires_independent_enable_switch() -> None:
    with pytest.raises(ConfigurationError, match="requires both"):
        Settings(_env_file=None, trading_mode=TradingMode.LIVE)


def test_live_mode_is_permitted_only_when_both_switches_are_set() -> None:
    settings = Settings(
        _env_file=None,
        trading_mode=TradingMode.LIVE,
        live_trading_enabled=True,
    )

    assert settings.live_execution_permitted is True


def test_invalid_trading_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, trading_mode="not-a-mode")


def test_settings_cache_returns_same_instance() -> None:
    first = get_settings()
    second = get_settings()

    assert first is second
