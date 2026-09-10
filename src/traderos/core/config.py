"""Validated application configuration.

This module intentionally contains configuration only. It does not establish
database, Redis, provider, or broker connections.
"""

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from traderos.core.errors import ConfigurationError


class TradingMode(StrEnum):
    """Supported execution contexts."""

    RESEARCH = "research"
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    """Environment-backed settings with fail-closed live-mode validation."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = Field(default="TRADEROS", min_length=1)
    environment: str = Field(default="development", min_length=1)
    log_level: str = Field(default="INFO", min_length=1)
    timezone: str = Field(default="UTC", min_length=1)
    trading_mode: TradingMode = TradingMode.RESEARCH
    live_trading_enabled: bool = False
    market_data_provider: str = Field(default="local", min_length=1)
    data_max_retries: int = Field(default=3, ge=0, le=10)
    data_retry_backoff_seconds: float = Field(default=0.0, ge=0.0, le=300.0)
    database_url: str = Field(
        default="postgresql+psycopg://traderos:change-me@localhost:5432/traderos",
        min_length=1,
    )
    redis_url: str = Field(default="redis://localhost:6379/0", min_length=1)

    @model_validator(mode="after")
    def validate_live_safety(self) -> "Settings":
        """Reject an explicitly requested live mode unless the second switch is on."""

        if self.trading_mode is TradingMode.LIVE and not self.live_trading_enabled:
            raise ConfigurationError(
                "Live mode requires both TRADING_MODE=live and LIVE_TRADING_ENABLED=true."
            )
        return self

    @property
    def live_execution_permitted(self) -> bool:
        """Return the configuration-level live gate; human gates still apply."""

        return self.trading_mode is TradingMode.LIVE and self.live_trading_enabled


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process settings singleton.

    Callers that need isolated settings in tests should instantiate ``Settings``
    directly rather than mutating process-global configuration.
    """

    return Settings()


__all__ = ["Settings", "TradingMode", "get_settings"]
