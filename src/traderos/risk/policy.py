"""Versioned, validated policy configuration for the risk firewall."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator


def _positive(value: Decimal) -> Decimal:
    if not value.is_finite() or value <= 0:
        raise ValueError("risk limits must be finite and positive")
    return value


def _nonnegative(value: Decimal) -> Decimal:
    if not value.is_finite() or value < 0:
        raise ValueError("risk limits must be finite and non-negative")
    return value


class RiskFirewallParameters(BaseModel):
    """All decision-relevant, fail-closed limits for ``risk_firewall`` v1.

    Monetary and exposure limits are account-currency Decimal amounts. The
    limits are deliberately configured, never estimated from backtest returns.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_market_data_age: timedelta = timedelta(minutes=5)
    max_risk_snapshot_age: timedelta = timedelta(minutes=5)
    max_system_health_age: timedelta = timedelta(minutes=5)
    max_spread_fraction: Decimal = Decimal("0.002")
    max_trade_notional: Decimal = Decimal("10000")
    max_loss_at_stop: Decimal = Decimal("100")
    max_gross_exposure: Decimal = Decimal("50000")
    max_net_exposure: Decimal = Decimal("30000")
    max_long_exposure: Decimal = Decimal("40000")
    max_short_exposure: Decimal = Decimal("40000")
    max_instrument_exposure: Decimal = Decimal("15000")
    max_asset_class_exposure: Decimal = Decimal("30000")
    max_open_positions: int = 10
    max_pending_intents: int = 5
    max_leverage: Decimal = Decimal("2")
    minimum_available_margin: Decimal = Decimal("0")
    minimum_cash_buffer: Decimal = Decimal("0")
    daily_loss_limit: Decimal = Decimal("500")
    max_drawdown: Decimal = Decimal("1000")
    reject_stressed_liquidity: bool = True
    kill_switch_active: bool = False

    @field_validator(
        "max_spread_fraction",
        "max_trade_notional",
        "max_loss_at_stop",
        "max_gross_exposure",
        "max_net_exposure",
        "max_long_exposure",
        "max_short_exposure",
        "max_instrument_exposure",
        "max_asset_class_exposure",
        "max_leverage",
        "daily_loss_limit",
        "max_drawdown",
    )
    @classmethod
    def positive_decimals(cls, value: Decimal) -> Decimal:
        return _positive(value)

    @field_validator("minimum_available_margin", "minimum_cash_buffer")
    @classmethod
    def nonnegative_decimals(cls, value: Decimal) -> Decimal:
        return _nonnegative(value)

    @field_validator("max_market_data_age", "max_risk_snapshot_age", "max_system_health_age")
    @classmethod
    def positive_durations(cls, value: timedelta) -> timedelta:
        if value <= timedelta(0):
            raise ValueError("freshness bounds must be positive")
        return value

    @field_validator("max_open_positions", "max_pending_intents")
    @classmethod
    def positive_counts(cls, value: int) -> int:
        if value < 1:
            raise ValueError("risk count limits must be positive")
        return value


def risk_configuration_identity(parameters: RiskFirewallParameters) -> str:
    """Hash all and only policy parameters that can affect a risk decision."""

    payload = {
        name: (
            value.total_seconds()
            if isinstance(value, timedelta)
            else str(value)
            if isinstance(value, Decimal)
            else value
        )
        for name, value in sorted(parameters.model_dump().items())
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["RiskFirewallParameters", "risk_configuration_identity"]
