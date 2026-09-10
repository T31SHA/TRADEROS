"""Instance-scoped deterministic strategy registry."""

from collections.abc import Iterable

from traderos.features.registry import FeatureRegistry
from traderos.strategies.base import Strategy
from traderos.strategies.errors import StrategyRegistryError


class StrategyRegistry:
    """Register strategy instances by explicit `(strategy_id, version)` identity."""

    def __init__(self, feature_registry: FeatureRegistry | None = None) -> None:
        self._strategies: dict[tuple[str, str], Strategy] = {}
        self._feature_registry = feature_registry

    def register(self, strategy: Strategy) -> None:
        metadata = strategy.metadata
        key = (metadata.strategy_id, metadata.strategy_version)
        if key in self._strategies:
            raise StrategyRegistryError(f"strategy {key!r} is already registered")
        if (
            strategy.strategy_id != metadata.strategy_id
            or strategy.strategy_version != metadata.strategy_version
        ):
            raise StrategyRegistryError("strategy attributes do not match metadata")
        if self._feature_registry is not None:
            for requirement in metadata.required_features:
                for asset_class in metadata.supported_asset_classes:
                    for timeframe in metadata.supported_timeframes:
                        self._feature_registry.resolve(
                            requirement.name, requirement.version, asset_class, timeframe
                        )
        self._strategies[key] = strategy

    def get(self, strategy_id: str, strategy_version: str | None = None) -> Strategy:
        if strategy_version is not None:
            try:
                return self._strategies[(strategy_id, strategy_version)]
            except KeyError as exc:
                raise StrategyRegistryError(
                    f"strategy {strategy_id}.v{strategy_version} is not registered"
                ) from exc
        matches = [
            strategy
            for (identifier, _), strategy in self._strategies.items()
            if identifier == strategy_id
        ]
        if len(matches) != 1:
            raise StrategyRegistryError(f"strategy {strategy_id!r} requires an explicit version")
        return matches[0]

    def all(self) -> tuple[Strategy, ...]:
        return tuple(self._strategies[key] for key in sorted(self._strategies))


def build_default_strategy_registry(
    strategies: Iterable[Strategy], feature_registry: FeatureRegistry | None = None
) -> StrategyRegistry:
    registry = StrategyRegistry(feature_registry)
    for strategy in strategies:
        registry.register(strategy)
    return registry


__all__ = ["StrategyRegistry", "build_default_strategy_registry"]
