"""Instance-scoped registry for explicit regime detector versions."""

from collections.abc import Iterable

from traderos.features.registry import FeatureRegistry
from traderos.regimes.errors import RegimeRegistryError
from traderos.regimes.models import RegimeDetector


class RegimeRegistry:
    """Register detectors by immutable ``(id, version)`` identity."""

    def __init__(self, feature_registry: FeatureRegistry | None = None) -> None:
        self._detectors: dict[tuple[str, str], RegimeDetector] = {}
        self._feature_registry = feature_registry

    def register(self, detector: RegimeDetector) -> None:
        metadata = detector.metadata
        key = (metadata.regime_detector_id, metadata.regime_detector_version)
        if key in self._detectors:
            raise RegimeRegistryError(f"regime detector {key!r} is already registered")
        if (
            detector.regime_detector_id != metadata.regime_detector_id
            or detector.regime_detector_version != metadata.regime_detector_version
        ):
            raise RegimeRegistryError("detector attributes do not match metadata")
        if self._feature_registry is not None:
            for requirement in metadata.required_features:
                for asset_class in metadata.supported_asset_classes:
                    for timeframe in metadata.supported_timeframes:
                        self._feature_registry.resolve(
                            requirement.name, requirement.version, asset_class, timeframe
                        )
        self._detectors[key] = detector

    def get(
        self, regime_detector_id: str, regime_detector_version: str | None = None
    ) -> RegimeDetector:
        """Get a detector; ambiguous unversioned lookups fail explicitly."""

        if regime_detector_version is not None:
            try:
                return self._detectors[(regime_detector_id, regime_detector_version)]
            except KeyError as exc:
                raise RegimeRegistryError(
                    "regime detector "
                    f"{regime_detector_id}.v{regime_detector_version} is not registered"
                ) from exc
        matches = [
            detector
            for (identifier, _), detector in self._detectors.items()
            if identifier == regime_detector_id
        ]
        if len(matches) != 1:
            raise RegimeRegistryError(
                f"regime detector {regime_detector_id!r} requires an explicit version"
            )
        return matches[0]

    def all(self) -> tuple[RegimeDetector, ...]:
        """Return a deterministic identity-sorted detector list."""

        return tuple(self._detectors[key] for key in sorted(self._detectors))


def build_regime_registry(
    detectors: Iterable[RegimeDetector], feature_registry: FeatureRegistry | None = None
) -> RegimeRegistry:
    """Build a registry without process-global mutable detector state."""

    registry = RegimeRegistry(feature_registry)
    for detector in detectors:
        registry.register(detector)
    return registry


__all__ = ["RegimeRegistry", "build_regime_registry"]
