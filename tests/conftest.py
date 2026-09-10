"""Shared test configuration."""

import pytest


@pytest.fixture(autouse=True)
def clear_settings_cache() -> None:
    """Prevent the process settings singleton from leaking between tests."""

    from traderos.core.config import get_settings

    get_settings.cache_clear()
