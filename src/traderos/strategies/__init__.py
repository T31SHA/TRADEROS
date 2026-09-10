"""Versioned signal-only strategies and deterministic baseline rules."""

from traderos.strategies.baselines import (
    BreakoutParameters,
    EquityBreakoutStrategy,
    EquityMomentumStrategy,
    ForexBreakoutStrategy,
    ForexTrendFollowingStrategy,
    MomentumParameters,
    TrendParameters,
)
from traderos.strategies.errors import (
    MissingFeatureError,
    StrategyCausalityError,
    StrategyConfigurationError,
    StrategyError,
    StrategyRegistryError,
)
from traderos.strategies.integration import BacktestStrategyAdapter
from traderos.strategies.models import (
    FeatureRequirement,
    OrderIntent,
    SignalDirection,
    StrategyContext,
    StrategyMetadata,
    StrategyResult,
    StrategySignal,
)
from traderos.strategies.registry import StrategyRegistry, build_default_strategy_registry

__all__ = [
    "BacktestStrategyAdapter",
    "BreakoutParameters",
    "EquityBreakoutStrategy",
    "EquityMomentumStrategy",
    "FeatureRequirement",
    "ForexBreakoutStrategy",
    "ForexTrendFollowingStrategy",
    "MissingFeatureError",
    "MomentumParameters",
    "OrderIntent",
    "SignalDirection",
    "StrategyCausalityError",
    "StrategyConfigurationError",
    "StrategyContext",
    "StrategyError",
    "StrategyMetadata",
    "StrategyRegistry",
    "StrategyRegistryError",
    "StrategyResult",
    "StrategySignal",
    "TrendParameters",
    "build_default_strategy_registry",
]
