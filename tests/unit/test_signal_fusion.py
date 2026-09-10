"""Adversarial contracts and causality tests for Phase 6 signal fusion."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from traderos.data.bars import MarketBar
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.timeframes import Timeframe
from traderos.features import FeatureContext, FeatureEngine, FeatureRequest
from traderos.regimes import (
    DataSessionState,
    LiquidityRegime,
    RegimeState,
    TrendRegime,
    VolatilityRegime,
)
from traderos.signals import (
    DuplicateStrategySignalError,
    EligibilityState,
    FusionCausalityError,
    FusionConfigurationError,
    FusionContext,
    FusionDirection,
    FusionPolicyMetadata,
    FusionPolicyRegistry,
    FusionPolicyRegistryError,
    FusionStatus,
    FutureRegimeError,
    FutureSignalError,
    IncompatibleSignalError,
    MajorityVoteFusionPolicy,
    MajorityVoteParameters,
    NormalizedSignal,
    RegimeCompatibilityRule,
    SignalExclusion,
    SignalFusionEngine,
    UnifiedTradeIntent,
    build_fusion_policy_registry,
)
from traderos.strategies import (
    BreakoutParameters,
    FeatureRequirement,
    ForexBreakoutStrategy,
    ForexTrendFollowingStrategy,
    SignalDirection,
    StrategyContext,
    StrategySignal,
    TrendParameters,
)

BASE = datetime(2024, 1, 2, 12, tzinfo=UTC)
_DEFAULT_STATE = object()


def instrument(symbol: str = "EUR/USD", asset_class: AssetClass = AssetClass.FOREX) -> Instrument:
    return Instrument(
        canonical_symbol=symbol,
        asset_class=asset_class,
        base_currency="EUR" if asset_class is AssetClass.FOREX else None,
        quote_currency="USD",
        trading_currency="USD",
    )


def strategy_signal(
    strategy_id: str,
    direction: SignalDirection,
    *,
    item: Instrument | None = None,
    timeframe: Timeframe = Timeframe.H1,
    timestamp: datetime = BASE,
    score: float | None = None,
) -> StrategySignal:
    item = item or instrument()
    return StrategySignal(
        signal_id=f"{strategy_id}-{direction.value}-{timestamp.isoformat()}",
        strategy_id=strategy_id,
        strategy_version="1",
        instrument=item,
        timestamp=timestamp,
        timeframe=timeframe,
        direction=direction,
        reason=f"{strategy_id}_{direction.value}",
        feature_provenance=(FeatureRequirement.from_parameters("ema", 1, {"window": 10}),),
        score=score,
    )


def regime(
    *,
    item: Instrument | None = None,
    timeframe: Timeframe = Timeframe.H1,
    timestamp: datetime = BASE,
    trend: TrendRegime = TrendRegime.TRENDING_UP,
    volatility: VolatilityRegime = VolatilityRegime.NORMAL,
    liquidity: LiquidityRegime = LiquidityRegime.NORMAL,
    data_session: DataSessionState = DataSessionState.ACTIVE,
) -> RegimeState:
    item = item or instrument()
    return RegimeState(
        state_id=f"regime-{timestamp.isoformat()}-{trend.value}-{liquidity.value}",
        instrument=item,
        asset_class=item.asset_class,
        timeframe=timeframe,
        decision_timestamp=timestamp,
        regime_detector_id="fixture_detector",
        regime_detector_version="1",
        configuration_id="fixture-config",
        trend=trend,
        volatility=volatility,
        liquidity=liquidity,
        data_session=data_session,
        feature_provenance=(),
    )


def fuse(
    signals: tuple[StrategySignal | NormalizedSignal, ...],
    *,
    state: RegimeState | None | object = _DEFAULT_STATE,
    policy: MajorityVoteFusionPolicy | None = None,
):
    item = signals[0].instrument
    timeframe = signals[0].timeframe
    timestamp = (
        signals[0].timestamp
        if isinstance(signals[0], StrategySignal)
        else signals[0].decision_timestamp
    )
    resolved_state = (
        regime(item=item, timeframe=timeframe, timestamp=timestamp)
        if state is _DEFAULT_STATE
        else state
    )
    assert resolved_state is None or isinstance(resolved_state, RegimeState)
    return SignalFusionEngine(policy or MajorityVoteFusionPolicy()).fuse(
        FusionContext(
            instrument=item,
            timeframe=timeframe,
            decision_timestamp=timestamp,
            signals=signals,
            regime_state=resolved_state,
        )
    )


@pytest.mark.parametrize(
    ("directions", "expected"),
    [
        ((SignalDirection.LONG, SignalDirection.LONG, SignalDirection.LONG), FusionDirection.LONG),
        ((SignalDirection.LONG, SignalDirection.LONG, SignalDirection.SHORT), FusionDirection.LONG),
        (
            (SignalDirection.SHORT, SignalDirection.SHORT, SignalDirection.LONG),
            FusionDirection.SHORT,
        ),
    ],
)
def test_majority_policy_resolves_unanimous_and_majority_directions(directions, expected) -> None:
    result = fuse(
        tuple(
            strategy_signal(f"strategy_{index}", direction)
            for index, direction in enumerate(directions)
        )
    )
    assert result.direction is expected
    assert result.status is FusionStatus.DIRECTIONAL
    assert result.long_count + result.short_count == 3


def test_tie_is_no_trade_hold_and_is_never_arbitrarily_broken() -> None:
    result = fuse(
        (
            strategy_signal("trend", SignalDirection.LONG),
            strategy_signal("breakout", SignalDirection.SHORT),
        )
    )
    assert result.direction is FusionDirection.HOLD
    assert result.status is FusionStatus.NO_CONSENSUS


def test_flat_and_hold_have_distinct_explicit_semantics() -> None:
    flat = fuse((strategy_signal("trend", SignalDirection.FLAT),))
    hold = fuse((strategy_signal("trend", SignalDirection.HOLD),))
    assert flat.direction is FusionDirection.FLAT
    assert flat.status is FusionStatus.FLAT
    assert hold.direction is FusionDirection.HOLD
    assert hold.status is FusionStatus.HOLD
    assert flat.flat_count == 1 and hold.hold_count == 1


def test_normalization_preserves_score_and_does_not_invent_confidence() -> None:
    source = strategy_signal("trend", SignalDirection.LONG, score=0.25)
    normalized = NormalizedSignal.from_strategy_signal(source)
    assert normalized.direction is FusionDirection.LONG
    assert normalized.deterministic_score == 0.25
    assert normalized.source_signal_id == source.signal_id
    assert normalized.feature_provenance == source.feature_provenance


def test_actual_phase4_strategy_signals_fuse_without_an_execution_adapter() -> None:
    item = instrument()
    bars = tuple(
        MarketBar(
            instrument=item,
            timeframe=Timeframe.H1,
            timestamp=BASE + timedelta(hours=index),
            open=Decimal(str(100 + index)),
            high=Decimal(str(108 if index == 5 else 101 + index)),
            low=Decimal(str(99 + index)),
            close=Decimal(str(107 if index == 5 else 100 + index)),
            source="fixture",
            currency="USD",
            ingestion_timestamp=BASE,
        )
        for index in range(6)
    )
    strategies = (
        ForexTrendFollowingStrategy(TrendParameters(fast_period=2, slow_period=4)),
        ForexBreakoutStrategy(BreakoutParameters(lookback=2)),
    )
    signals = []
    for strategy in strategies:
        requests = tuple(
            FeatureRequest(
                name=requirement.name,
                version=requirement.version,
                parameters=dict(requirement.parameters),
            )
            for requirement in strategy.metadata.required_features
        )
        observations = (
            FeatureEngine()
            .compute_feature_set(
                bars,
                requests,
                FeatureContext(
                    instrument=item,
                    timeframe=Timeframe.H1,
                    source_dataset_version="fixture-v1",
                    computation_timestamp=BASE,
                ),
            )
            .observations
        )
        current = bars[-1]
        decision = current.timestamp + current.timeframe.duration
        signals.append(
            strategy.on_bar(
                StrategyContext(
                    event_timestamp=decision,
                    bar=current,
                    feature_observations=tuple(
                        observation
                        for observation in observations
                        if observation.observation_timestamp <= current.timestamp
                        and observation.availability_timestamp <= decision
                    ),
                    position_quantity=Decimal("0"),
                    cash=Decimal("1000"),
                    equity=Decimal("1000"),
                    parameters=strategy.parameters,
                    strategy_id=strategy.strategy_id,
                    strategy_version=strategy.strategy_version,
                )
            ).signal
        )
    result = fuse(tuple(signals), state=regime(item=item, timestamp=signals[0].timestamp))
    assert result.direction is FusionDirection.LONG
    assert result.long_count == 2
    assert "quantity" not in result.__dataclass_fields__


def test_unavailable_signals_are_not_neutral_or_flat() -> None:
    item = instrument()
    unavailable = NormalizedSignal(
        source_signal_id="unavailable",
        strategy_id="trend",
        strategy_version="1",
        instrument=item,
        asset_class=item.asset_class,
        timeframe=Timeframe.H1,
        decision_timestamp=BASE,
        direction=FusionDirection.UNAVAILABLE,
        reason="upstream_unavailable",
        feature_provenance=(),
        unavailable_reason="feature_warmup",
    )
    result = fuse((unavailable,))
    assert result.direction is FusionDirection.UNAVAILABLE
    assert result.status is FusionStatus.ALL_SIGNALS_UNAVAILABLE
    assert result.excluded_signals[0].eligibility is EligibilityState.UNAVAILABLE


def test_duplicate_source_signal_is_excluded_without_vote_amplification() -> None:
    long = strategy_signal("trend", SignalDirection.LONG)
    short = strategy_signal("breakout", SignalDirection.SHORT)
    result = fuse((long, long, short))
    assert result.direction is FusionDirection.HOLD
    assert result.long_count == 1 and result.short_count == 1
    assert result.excluded_signals[0].eligibility is EligibilityState.DUPLICATE


def test_conflicting_multiple_signals_from_one_strategy_fail_closed() -> None:
    with pytest.raises(DuplicateStrategySignalError, match="one strategy instance"):
        fuse(
            (
                strategy_signal("trend", SignalDirection.LONG),
                strategy_signal("trend", SignalDirection.SHORT),
            )
        )


def test_scope_and_temporal_mismatches_are_rejected() -> None:
    first = strategy_signal("trend", SignalDirection.LONG)
    other_item = strategy_signal("breakout", SignalDirection.LONG, item=instrument("GBP/USD"))
    with pytest.raises(IncompatibleSignalError, match="instrument"):
        FusionContext(first.instrument, Timeframe.H1, BASE, (first, other_item), regime())
    other_timeframe = strategy_signal("breakout", SignalDirection.LONG, timeframe=Timeframe.H4)
    with pytest.raises(IncompatibleSignalError, match="timeframe"):
        FusionContext(first.instrument, Timeframe.H1, BASE, (first, other_timeframe), regime())
    future = strategy_signal("breakout", SignalDirection.LONG, timestamp=BASE + timedelta(hours=1))
    with pytest.raises(FutureSignalError, match="future"):
        FusionContext(first.instrument, Timeframe.H1, BASE, (first, future), regime())
    stale = strategy_signal("breakout", SignalDirection.LONG, timestamp=BASE - timedelta(hours=1))
    with pytest.raises(FusionCausalityError, match="match"):
        FusionContext(first.instrument, Timeframe.H1, BASE, (first, stale), regime())


def test_regime_scope_and_temporal_mismatches_are_rejected() -> None:
    source = strategy_signal("trend", SignalDirection.LONG)
    with pytest.raises(FutureRegimeError, match="future"):
        FusionContext(
            source.instrument,
            Timeframe.H1,
            BASE,
            (source,),
            regime(timestamp=BASE + timedelta(hours=1)),
        )
    with pytest.raises(FusionCausalityError, match="match"):
        FusionContext(
            source.instrument,
            Timeframe.H1,
            BASE,
            (source,),
            regime(timestamp=BASE - timedelta(hours=1)),
        )
    with pytest.raises(IncompatibleSignalError, match="regime instrument"):
        FusionContext(
            source.instrument,
            Timeframe.H1,
            BASE,
            (source,),
            regime(item=instrument("GBP/USD")),
        )


@pytest.mark.parametrize(
    "state",
    [
        None,
        regime(data_session=DataSessionState.DATA_UNAVAILABLE),
        regime(trend=TrendRegime.UNAVAILABLE),
        regime(volatility=VolatilityRegime.UNAVAILABLE),
        regime(liquidity=LiquidityRegime.UNKNOWN),
    ],
)
def test_unavailable_regime_fails_closed_without_reclassifying_it(state) -> None:
    result = fuse((strategy_signal("trend", SignalDirection.LONG),), state=state)
    assert result.direction is FusionDirection.UNAVAILABLE
    assert result.status is FusionStatus.REGIME_UNAVAILABLE
    assert result.excluded_signals[0].eligibility is EligibilityState.REGIME_UNAVAILABLE


def test_declarative_regime_compatibility_excludes_only_disallowed_signal() -> None:
    rules = (
        RegimeCompatibilityRule(
            strategy_id="trend",
            strategy_version="1",
            allowed_directions=frozenset({FusionDirection.LONG}),
            allowed_trends=frozenset({TrendRegime.TRENDING_UP}),
        ),
        RegimeCompatibilityRule(
            strategy_id="trend",
            strategy_version="1",
            allowed_directions=frozenset({FusionDirection.SHORT}),
            allowed_trends=frozenset({TrendRegime.TRENDING_DOWN}),
        ),
    )
    policy = MajorityVoteFusionPolicy(compatibility_rules=rules)
    result = fuse(
        (
            strategy_signal("trend", SignalDirection.SHORT),
            strategy_signal("breakout", SignalDirection.LONG),
        ),
        policy=policy,
    )
    assert result.direction is FusionDirection.LONG
    assert result.short_count == 0
    assert result.excluded_signals[0].eligibility is EligibilityState.REGIME_INCOMPATIBLE

    long_only = MajorityVoteFusionPolicy(
        compatibility_rules=(
            RegimeCompatibilityRule(
                strategy_id="trend",
                strategy_version="1",
                allowed_directions=frozenset({FusionDirection.LONG}),
            ),
        )
    )
    direction_mismatch = fuse((strategy_signal("trend", SignalDirection.SHORT),), policy=long_only)
    assert direction_mismatch.direction is FusionDirection.UNAVAILABLE
    assert (
        direction_mismatch.excluded_signals[0].eligibility is EligibilityState.REGIME_INCOMPATIBLE
    )


def test_policy_thresholds_and_ties_are_explicit_and_strict() -> None:
    policy = MajorityVoteFusionPolicy(
        MajorityVoteParameters(minimum_directional_votes=2, minimum_margin=2)
    )
    result = fuse(
        (
            strategy_signal("trend", SignalDirection.LONG),
            strategy_signal("breakout", SignalDirection.LONG),
            strategy_signal("momentum", SignalDirection.SHORT),
        ),
        policy=policy,
    )
    assert result.direction is FusionDirection.HOLD
    assert result.status is FusionStatus.NO_CONSENSUS
    with pytest.raises(ValueError):
        MajorityVoteParameters(minimum_margin=0)
    with pytest.raises(ValueError):
        MajorityVoteParameters(minimum_margin=True)


def test_permutation_duplicate_future_append_and_mutation_invariance() -> None:
    signals = (
        strategy_signal("trend", SignalDirection.LONG),
        strategy_signal("breakout", SignalDirection.LONG),
        strategy_signal("momentum", SignalDirection.SHORT),
    )
    original = fuse(signals)
    permuted = fuse((signals[2], signals[0], signals[1]))
    assert permuted == original

    future = strategy_signal("future", SignalDirection.SHORT, timestamp=BASE + timedelta(hours=1))
    assert fuse(signals) == original
    assert future.timestamp > original.decision_timestamp
    mutated_future = replace(future, direction=SignalDirection.LONG)
    assert mutated_future.timestamp > original.decision_timestamp
    assert fuse(signals) == original


def test_configuration_identity_changes_with_parameters_or_compatibility_rules() -> None:
    base = MajorityVoteFusionPolicy()
    threshold = MajorityVoteFusionPolicy(MajorityVoteParameters(minimum_directional_votes=2))
    constrained = MajorityVoteFusionPolicy(
        compatibility_rules=(
            RegimeCompatibilityRule("trend", "1", allowed_trends=frozenset({TrendRegime.NEUTRAL})),
        )
    )
    assert (
        len({base.configuration_id, threshold.configuration_id, constrained.configuration_id}) == 3
    )
    assert base.configuration_id == MajorityVoteFusionPolicy().configuration_id


def test_policy_registry_requires_explicit_version_and_valid_identity() -> None:
    policy = MajorityVoteFusionPolicy()
    registry = FusionPolicyRegistry()
    registry.register(policy)
    assert registry.get(policy.policy_id) is policy
    with pytest.raises(FusionPolicyRegistryError):
        registry.register(policy)
    with pytest.raises(FusionPolicyRegistryError):
        registry.get("missing", "1")
    second = MajorityVoteFusionPolicy()
    second.policy_version = "2"
    registry.register(second)
    with pytest.raises(FusionPolicyRegistryError, match="explicit version"):
        registry.get(policy.policy_id)


def test_configuration_and_model_validation_are_fail_closed() -> None:
    duplicate_rule = RegimeCompatibilityRule("trend", "1")
    with pytest.raises(FusionConfigurationError, match="duplicate"):
        MajorityVoteFusionPolicy(compatibility_rules=(duplicate_rule, duplicate_rule))
    with pytest.raises(ValueError):
        RegimeCompatibilityRule("", "1")
    with pytest.raises(ValueError):
        RegimeCompatibilityRule("trend", "1", allowed_trends=frozenset())
    with pytest.raises(ValueError):
        NormalizedSignal(
            "id",
            "strategy",
            "1",
            instrument(),
            AssetClass.FOREX,
            Timeframe.H1,
            BASE,
            FusionDirection.UNAVAILABLE,
            "reason",
            (),
        )
    result = fuse((strategy_signal("trend", SignalDirection.LONG),))
    with pytest.raises(ValueError):
        replace(result, long_count=-1)
    assert "quantity" not in result.__dataclass_fields__


def test_remaining_contract_guards_and_registry_helpers_are_explicit() -> None:
    source = strategy_signal("trend", SignalDirection.LONG)
    normalized = NormalizedSignal.from_strategy_signal(source)
    with pytest.raises(ValueError):
        replace(normalized, asset_class=AssetClass.EQUITY)
    with pytest.raises(ValueError):
        replace(normalized, source_signal_id="")
    with pytest.raises(ValueError):
        replace(normalized, deterministic_score=float("inf"))
    with pytest.raises(ValueError):
        replace(normalized, unavailable_reason="unexpected")
    with pytest.raises(ValueError):
        FusionPolicyMetadata("", "1", (), "description")
    with pytest.raises(ValueError):
        FusionPolicyMetadata("policy", "1", ("x", "x"), "description")
    with pytest.raises(ValueError):
        SignalExclusion(normalized, EligibilityState.ELIGIBLE, "not_allowed")
    with pytest.raises(ValueError):
        SignalExclusion(normalized, EligibilityState.DUPLICATE, " ")
    with pytest.raises(ValueError):
        FusionContext(source.instrument, Timeframe.H1, BASE, (), regime())
    with pytest.raises(IncompatibleSignalError, match="regime timeframe"):
        FusionContext(
            source.instrument, Timeframe.H1, BASE, (source,), regime(timeframe=Timeframe.H4)
        )

    rule = RegimeCompatibilityRule(
        "trend",
        "1",
        allowed_directions=frozenset({FusionDirection.LONG}),
        allowed_volatility=frozenset({VolatilityRegime.LOW}),
    )
    assert rule.applies_to(normalized)
    assert not rule.allows(regime())
    assert rule.canonical()["allowed_volatility"] == ["low"]

    result = fuse((source,))
    with pytest.raises(ValueError):
        replace(result, asset_class=AssetClass.EQUITY)
    with pytest.raises(ValueError):
        replace(result, configuration_id="")
    with pytest.raises(ValueError):
        replace(result, direction=FusionDirection.UNAVAILABLE, status=FusionStatus.HOLD)
    assert isinstance(result, UnifiedTradeIntent)

    policy = MajorityVoteFusionPolicy()
    built = build_fusion_policy_registry((policy,))
    assert built.all() == (policy,)

    class MismatchedPolicy:
        policy_id = "mismatched"
        policy_version = "1"
        parameters = {}
        compatibility_rules = ()
        configuration_id = "test"

        @property
        def metadata(self):
            return FusionPolicyMetadata("different", "1", (), "mismatch")

        def decide(self, signals):
            del signals
            raise AssertionError("not called")

    with pytest.raises(FusionPolicyRegistryError, match="attributes"):
        FusionPolicyRegistry().register(MismatchedPolicy())
