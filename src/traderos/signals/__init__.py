"""Versioned deterministic signal fusion with no execution or sizing behavior."""

from traderos.signals.errors import (
    DuplicateStrategySignalError,
    FusionCausalityError,
    FusionConfigurationError,
    FusionPolicyRegistryError,
    FutureRegimeError,
    FutureSignalError,
    IncompatibleSignalError,
    SignalFusionError,
)
from traderos.signals.fusion import SignalFusionEngine
from traderos.signals.models import (
    EligibilityState,
    FusionContext,
    FusionDirection,
    FusionPolicy,
    FusionPolicyMetadata,
    FusionStatus,
    NormalizedSignal,
    PolicyDecision,
    RegimeCompatibilityRule,
    SignalExclusion,
    UnifiedTradeIntent,
)
from traderos.signals.policies import MajorityVoteFusionPolicy, MajorityVoteParameters
from traderos.signals.registry import FusionPolicyRegistry, build_fusion_policy_registry

__all__ = [
    "DuplicateStrategySignalError",
    "EligibilityState",
    "FusionCausalityError",
    "FusionConfigurationError",
    "FusionContext",
    "FusionDirection",
    "FusionPolicy",
    "FusionPolicyMetadata",
    "FusionPolicyRegistry",
    "FusionPolicyRegistryError",
    "FusionStatus",
    "FutureRegimeError",
    "FutureSignalError",
    "IncompatibleSignalError",
    "MajorityVoteFusionPolicy",
    "MajorityVoteParameters",
    "NormalizedSignal",
    "PolicyDecision",
    "RegimeCompatibilityRule",
    "SignalExclusion",
    "SignalFusionEngine",
    "SignalFusionError",
    "UnifiedTradeIntent",
    "build_fusion_policy_registry",
]
