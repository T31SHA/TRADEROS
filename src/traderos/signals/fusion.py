"""Information combination only: no sizing, risk, execution, or broker behavior."""

from traderos.regimes.models import DataSessionState, LiquidityRegime, TrendRegime, VolatilityRegime
from traderos.signals.models import (
    EligibilityState,
    FusionContext,
    FusionDirection,
    FusionPolicy,
    FusionStatus,
    NormalizedSignal,
    PolicyDecision,
    SignalExclusion,
    UnifiedTradeIntent,
    deterministic_intent_id,
)


def _signal_key(signal: NormalizedSignal) -> tuple[str, str, str]:
    return signal.strategy_id, signal.strategy_version, signal.source_signal_id


class SignalFusionEngine:
    """Fuse one strictly aligned strategy/regime context with no mutable state."""

    def __init__(self, policy: FusionPolicy) -> None:
        self.policy = policy

    @staticmethod
    def _usable_regime(context: FusionContext) -> bool:
        regime = context.regime_state
        return (
            regime is not None
            and regime.data_session is DataSessionState.ACTIVE
            and regime.trend is not TrendRegime.UNAVAILABLE
            and regime.volatility is not VolatilityRegime.UNAVAILABLE
            and regime.liquidity is not LiquidityRegime.UNKNOWN
        )

    def _compatible(self, signal: NormalizedSignal, context: FusionContext) -> bool:
        """Apply only rules explicitly configured for the strategy/direction."""

        assert context.regime_state is not None
        strategy_rules = tuple(
            rule
            for rule in self.policy.compatibility_rules
            if (rule.strategy_id, rule.strategy_version) == signal.strategy_key
        )
        if not strategy_rules:
            return True
        applicable_rules = tuple(rule for rule in strategy_rules if rule.applies_to(signal))
        return bool(applicable_rules) and any(
            rule.allows(context.regime_state) for rule in applicable_rules
        )

    @staticmethod
    def _counts(signals: tuple[NormalizedSignal, ...]) -> tuple[int, int, int, int]:
        return (
            sum(signal.direction is FusionDirection.LONG for signal in signals),
            sum(signal.direction is FusionDirection.SHORT for signal in signals),
            sum(signal.direction is FusionDirection.FLAT for signal in signals),
            sum(signal.direction is FusionDirection.HOLD for signal in signals),
        )

    def _intent(
        self,
        context: FusionContext,
        decision: PolicyDecision,
        eligible: tuple[NormalizedSignal, ...],
        excluded: tuple[SignalExclusion, ...],
    ) -> UnifiedTradeIntent:
        long_count, short_count, flat_count, hold_count = self._counts(eligible)
        return UnifiedTradeIntent(
            intent_id=deterministic_intent_id(
                context=context,
                policy=self.policy,
                decision=decision,
                eligible=eligible,
                excluded=excluded,
            ),
            instrument=context.instrument,
            asset_class=context.asset_class,
            timeframe=context.timeframe,
            decision_timestamp=context.decision_timestamp,
            direction=decision.direction,
            status=decision.status,
            policy_id=self.policy.policy_id,
            policy_version=self.policy.policy_version,
            configuration_id=self.policy.configuration_id,
            regime_state_id=context.regime_state.state_id if context.regime_state else None,
            regime_detector_id=(
                context.regime_state.regime_detector_id if context.regime_state else None
            ),
            regime_detector_version=(
                context.regime_state.regime_detector_version if context.regime_state else None
            ),
            eligible_signals=eligible,
            excluded_signals=excluded,
            long_count=long_count,
            short_count=short_count,
            flat_count=flat_count,
            hold_count=hold_count,
        )

    def fuse(self, context: FusionContext) -> UnifiedTradeIntent:
        """Return one reproducible unified intent from immutable current inputs."""

        normalized = tuple(
            signal
            if isinstance(signal, NormalizedSignal)
            else NormalizedSignal.from_strategy_signal(signal)
            for signal in context.signals
        )
        ordered = tuple(sorted(normalized, key=_signal_key))
        if not self._usable_regime(context):
            regime_excluded = tuple(
                SignalExclusion(
                    signal=signal,
                    eligibility=EligibilityState.REGIME_UNAVAILABLE,
                    reason="regime_state_unavailable_or_not_active",
                )
                for signal in ordered
            )
            return self._intent(
                context,
                PolicyDecision(FusionDirection.UNAVAILABLE, FusionStatus.REGIME_UNAVAILABLE),
                (),
                regime_excluded,
            )

        eligible: list[NormalizedSignal] = []
        exclusions: list[SignalExclusion] = []
        seen_source_ids: set[str] = set()
        for signal in ordered:
            if signal.source_signal_id in seen_source_ids:
                exclusions.append(
                    SignalExclusion(
                        signal=signal,
                        eligibility=EligibilityState.DUPLICATE,
                        reason="duplicate_source_signal",
                    )
                )
                continue
            seen_source_ids.add(signal.source_signal_id)
            if signal.direction is FusionDirection.UNAVAILABLE:
                exclusions.append(
                    SignalExclusion(
                        signal=signal,
                        eligibility=EligibilityState.UNAVAILABLE,
                        reason=signal.unavailable_reason or "unavailable_signal",
                    )
                )
            elif not self._compatible(signal, context):
                exclusions.append(
                    SignalExclusion(
                        signal=signal,
                        eligibility=EligibilityState.REGIME_INCOMPATIBLE,
                        reason="strategy_regime_compatibility_rule",
                    )
                )
            else:
                eligible.append(signal)
        eligible_tuple = tuple(eligible)
        excluded_tuple = tuple(exclusions)
        if not eligible_tuple:
            return self._intent(
                context,
                PolicyDecision(FusionDirection.UNAVAILABLE, FusionStatus.ALL_SIGNALS_UNAVAILABLE),
                eligible_tuple,
                excluded_tuple,
            )
        return self._intent(
            context,
            self.policy.decide(eligible_tuple),
            eligible_tuple,
            excluded_tuple,
        )


__all__ = ["SignalFusionEngine"]
