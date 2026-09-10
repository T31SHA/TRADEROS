"""Controlled deterministic fusion-policy implementations."""

from math import isfinite

from pydantic import BaseModel, ConfigDict, Field, field_validator

from traderos.signals.errors import FusionConfigurationError
from traderos.signals.models import (
    FusionDirection,
    FusionPolicyMetadata,
    FusionStatus,
    NormalizedSignal,
    PolicyDecision,
    RegimeCompatibilityRule,
    configuration_identity,
)


class MajorityVoteParameters(BaseModel):
    """Strict, explicit parameters for the unweighted majority policy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    minimum_directional_votes: int = Field(default=1, gt=0)
    minimum_margin: int = Field(default=1, gt=0)

    @field_validator("minimum_directional_votes", "minimum_margin")
    @classmethod
    def finite_integer(cls, value: int) -> int:
        if isinstance(value, bool) or not isfinite(value):
            raise ValueError("majority policy parameters must be finite integers")
        return value


class MajorityVoteFusionPolicy:
    """Unweighted count-based majority with exact, deterministic tie behavior.

    Scores and confidence are deliberately not used.  Every eligible strategy
    instance contributes one vote at most; the engine enforces that invariant.
    """

    policy_id = "majority_vote"
    policy_version = "1"

    def __init__(
        self,
        parameters: MajorityVoteParameters | None = None,
        *,
        compatibility_rules: tuple[RegimeCompatibilityRule, ...] = (),
    ) -> None:
        canonical_rules = tuple(
            sorted(
                compatibility_rules,
                key=lambda rule: str(rule.canonical()),
            )
        )
        if len({str(rule.canonical()) for rule in canonical_rules}) != len(canonical_rules):
            raise FusionConfigurationError("duplicate regime compatibility rules are not allowed")
        self.config = parameters or MajorityVoteParameters()
        self._compatibility_rules = canonical_rules

    @property
    def parameters(self) -> dict[str, int | float | str]:
        return dict(self.config.model_dump())

    @property
    def compatibility_rules(self) -> tuple[RegimeCompatibilityRule, ...]:
        return self._compatibility_rules

    @property
    def metadata(self) -> FusionPolicyMetadata:
        return FusionPolicyMetadata(
            policy_id=self.policy_id,
            policy_version=self.policy_version,
            parameter_schema=tuple(self.parameters),
            description="Unweighted majority vote with explicit minimum count and margin.",
        )

    @property
    def configuration_id(self) -> str:
        return configuration_identity(
            self.policy_id,
            self.policy_version,
            self.parameters,
            self.compatibility_rules,
        )

    def decide(self, signals: tuple[NormalizedSignal, ...]) -> PolicyDecision:
        """Resolve eligible signals without using any historical performance input."""

        long_count = sum(signal.direction is FusionDirection.LONG for signal in signals)
        short_count = sum(signal.direction is FusionDirection.SHORT for signal in signals)
        flat_count = sum(signal.direction is FusionDirection.FLAT for signal in signals)
        if long_count == 0 and short_count == 0:
            if flat_count > 0:
                return PolicyDecision(FusionDirection.FLAT, FusionStatus.FLAT)
            return PolicyDecision(FusionDirection.HOLD, FusionStatus.HOLD)
        if long_count == short_count:
            return PolicyDecision(FusionDirection.HOLD, FusionStatus.NO_CONSENSUS)
        winner = max(long_count, short_count)
        loser = min(long_count, short_count)
        if (
            winner < self.config.minimum_directional_votes
            or winner - loser < self.config.minimum_margin
        ):
            return PolicyDecision(FusionDirection.HOLD, FusionStatus.NO_CONSENSUS)
        direction = FusionDirection.LONG if long_count > short_count else FusionDirection.SHORT
        return PolicyDecision(direction, FusionStatus.DIRECTIONAL)


__all__ = ["MajorityVoteFusionPolicy", "MajorityVoteParameters"]
