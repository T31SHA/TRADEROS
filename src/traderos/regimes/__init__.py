"""Causal, deterministic, observation-only market regime detection."""

from traderos.regimes.detectors import BaselineRegimeParameters, EmaPercentileRegimeDetector
from traderos.regimes.engine import RegimeEngine
from traderos.regimes.errors import (
    RegimeAnalysisError,
    RegimeCausalityError,
    RegimeConfigurationError,
    RegimeError,
    RegimeRegistryError,
)
from traderos.regimes.models import (
    DataSessionState,
    FeatureProvenance,
    FeatureRequirement,
    LiquidityRegime,
    RegimeContext,
    RegimeDetector,
    RegimeMetadata,
    RegimeState,
    TrendRegime,
    VolatilityRegime,
)
from traderos.regimes.registry import RegimeRegistry, build_regime_registry
from traderos.regimes.stability import (
    DimensionStability,
    RegimeDimension,
    RegimeRun,
    RegimeShare,
    RegimeStabilityReport,
    analyze_regime_stability,
)

__all__ = [
    "BaselineRegimeParameters",
    "DataSessionState",
    "DimensionStability",
    "EmaPercentileRegimeDetector",
    "FeatureProvenance",
    "FeatureRequirement",
    "LiquidityRegime",
    "RegimeAnalysisError",
    "RegimeCausalityError",
    "RegimeConfigurationError",
    "RegimeContext",
    "RegimeDetector",
    "RegimeDimension",
    "RegimeEngine",
    "RegimeError",
    "RegimeMetadata",
    "RegimeRegistry",
    "RegimeRegistryError",
    "RegimeRun",
    "RegimeShare",
    "RegimeStabilityReport",
    "RegimeState",
    "TrendRegime",
    "VolatilityRegime",
    "analyze_regime_stability",
    "build_regime_registry",
]
