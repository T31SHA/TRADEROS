"""Leakage-safe quantitative feature engine for validated market bars."""

from traderos.features.engine import FeatureEngine, compute_feature, compute_feature_set
from traderos.features.errors import (
    CausalityViolationError,
    FeatureComputationError,
    FeatureConfigurationError,
    FeatureError,
    FeatureValidationError,
)
from traderos.features.models import (
    CausalSafety,
    FeatureContext,
    FeatureDefinition,
    FeatureLineage,
    FeatureMetadata,
    FeatureObservation,
    FeatureRequest,
    FeatureSet,
    FeatureStatus,
    MissingDataPolicy,
)
from traderos.features.registry import FeatureRegistry, build_default_registry
from traderos.features.storage import FeatureStore, InMemoryFeatureStore
from traderos.features.validation import validate_bar_series, validate_feature_set

__all__ = [
    "CausalSafety",
    "CausalityViolationError",
    "FeatureComputationError",
    "FeatureConfigurationError",
    "FeatureContext",
    "FeatureDefinition",
    "FeatureEngine",
    "FeatureError",
    "FeatureLineage",
    "FeatureMetadata",
    "FeatureObservation",
    "FeatureRegistry",
    "FeatureRequest",
    "FeatureSet",
    "FeatureStatus",
    "FeatureStore",
    "FeatureValidationError",
    "InMemoryFeatureStore",
    "MissingDataPolicy",
    "build_default_registry",
    "compute_feature",
    "compute_feature_set",
    "validate_bar_series",
    "validate_feature_set",
]
