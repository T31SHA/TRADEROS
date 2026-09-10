"""Explicit registry for controlled, versioned feature definitions."""

from collections.abc import Iterable

from traderos.data.instruments import AssetClass
from traderos.data.timeframes import Timeframe
from traderos.features.errors import FeatureConfigurationError
from traderos.features.models import FeatureDefinition


class FeatureRegistry:
    """Instance-scoped registry; no mutable process-global feature state."""

    def __init__(self, definitions: Iterable[FeatureDefinition] = ()) -> None:
        self._definitions: dict[tuple[str, int], FeatureDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: FeatureDefinition) -> None:
        if definition.key in self._definitions:
            raise FeatureConfigurationError(
                f"feature {definition.name}.v{definition.version} is already registered"
            )
        missing_dependencies = [
            dependency
            for dependency in definition.dependencies
            if not any(existing.name == dependency for existing in self._definitions.values())
        ]
        if missing_dependencies:
            raise FeatureConfigurationError(
                f"feature {definition.name}.v{definition.version} has missing dependencies: "
                f"{missing_dependencies}"
            )
        self._definitions[definition.key] = definition

    def get(self, name: str, version: int) -> FeatureDefinition:
        try:
            return self._definitions[(name, version)]
        except KeyError as exc:
            raise FeatureConfigurationError(f"feature {name}.v{version} is not registered") from exc

    def resolve(
        self, name: str, version: int, asset_class: AssetClass, timeframe: Timeframe
    ) -> FeatureDefinition:
        definition = self.get(name, version)
        if asset_class not in definition.applicable_asset_classes:
            raise FeatureConfigurationError(
                f"{name}.v{version} does not support {asset_class.value}"
            )
        if timeframe not in definition.applicable_timeframes:
            raise FeatureConfigurationError(f"{name}.v{version} does not support {timeframe.value}")
        return definition

    def all(self) -> tuple[FeatureDefinition, ...]:
        return tuple(self._definitions.values())


def _definition(
    name: str,
    description: str,
    columns: tuple[str, ...],
    lookback: int,
    *,
    dependencies: tuple[str, ...] = (),
    assets: frozenset[AssetClass] | None = None,
    parameters: dict[str, int | float | str] | None = None,
    parameter_names: frozenset[str] | None = None,
) -> FeatureDefinition:
    return FeatureDefinition(
        name=name,
        version=1,
        description=description,
        applicable_asset_classes=assets or frozenset(AssetClass),
        applicable_timeframes=frozenset(Timeframe),
        required_columns=columns,
        dependencies=dependencies,
        lookback=lookback,
        computation=name,
        default_parameters=parameters or {},
        parameter_names=parameter_names or frozenset(),
    )


def build_default_registry() -> FeatureRegistry:
    """Build the Phase 2 registry without introducing global mutable state."""

    definitions = (
        _definition("simple_return", "One-bar simple close return.", ("close",), 1),
        _definition("log_return", "One-bar logarithmic close return.", ("close",), 1),
        _definition(
            "rolling_return",
            "Close return over a causal window.",
            ("close",),
            20,
            parameter_names=frozenset({"window"}),
        ),
        _definition(
            "rate_of_change",
            "Rate of change over a causal window.",
            ("close",),
            20,
            parameter_names=frozenset({"window"}),
        ),
        _definition(
            "sma",
            "Simple moving average of close.",
            ("close",),
            20,
            parameter_names=frozenset({"window"}),
        ),
        _definition(
            "ema",
            "Exponentially weighted moving average of close.",
            ("close",),
            20,
            parameter_names=frozenset({"window"}),
        ),
        _definition(
            "moving_average_distance",
            "Close distance from a causal moving average.",
            ("close",),
            20,
            parameter_names=frozenset({"window", "average"}),
        ),
        _definition(
            "rolling_volatility",
            "Population standard deviation of simple returns.",
            ("close",),
            20,
            parameter_names=frozenset({"window"}),
        ),
        _definition(
            "realized_volatility",
            "Population standard deviation of log returns.",
            ("close",),
            20,
            parameter_names=frozenset({"window"}),
        ),
        _definition(
            "atr",
            "Average true range using causal true ranges.",
            ("high", "low", "close"),
            14,
            parameter_names=frozenset({"window"}),
        ),
        _definition(
            "rolling_high",
            "Inclusive rolling high of bar highs.",
            ("high",),
            20,
            parameter_names=frozenset({"window"}),
        ),
        _definition(
            "rolling_low",
            "Inclusive rolling low of bar lows.",
            ("low",),
            20,
            parameter_names=frozenset({"window"}),
        ),
        _definition(
            "range_position",
            "Close position in the inclusive rolling high-low range.",
            ("high", "low", "close"),
            20,
            dependencies=("rolling_high", "rolling_low"),
            parameter_names=frozenset({"window"}),
        ),
        _definition(
            "distance_to_previous_high",
            "Close distance from the prior completed high window.",
            ("high", "close"),
            20,
            parameter_names=frozenset({"window"}),
        ),
        _definition(
            "distance_to_previous_low",
            "Close distance from the prior completed low window.",
            ("low", "close"),
            20,
            parameter_names=frozenset({"window"}),
        ),
        _definition(
            "breakout_state",
            "State relative to prior completed high and low windows.",
            ("high", "low", "close"),
            20,
            parameter_names=frozenset({"window"}),
        ),
        _definition(
            "rsi",
            "Wilder relative strength index.",
            ("close",),
            14,
            parameter_names=frozenset({"period"}),
        ),
        _definition(
            "volatility_percentile",
            "Causal percentile rank of rolling simple-return volatility.",
            ("close",),
            20,
            dependencies=("rolling_volatility",),
            parameter_names=frozenset({"window"}),
        ),
        _definition(
            "volatility_ratio",
            "Short-window volatility divided by long-window volatility.",
            ("close",),
            30,
            parameters={"short_window": 10, "long_window": 30},
            parameter_names=frozenset({"short_window", "long_window"}),
        ),
        _definition(
            "equity_volume",
            "Provider-reported equity/ETF volume without FX reinterpretation.",
            ("volume",),
            1,
            assets=frozenset({AssetClass.EQUITY, AssetClass.ETF}),
        ),
        _definition(
            "dollar_volume",
            "Equity/ETF close multiplied by provider-reported volume.",
            ("close", "volume"),
            1,
            assets=frozenset({AssetClass.EQUITY, AssetClass.ETF}),
        ),
    )
    return FeatureRegistry(definitions)


__all__ = ["FeatureRegistry", "build_default_registry"]
