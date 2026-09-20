"""Canonical Phase 2→5→4→6→7→8-sizing→3 replay coverage."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from traderos.backtesting import BacktestConfig
from traderos.backtesting.engine import BacktestEngine
from traderos.data.bars import MarketBar
from traderos.data.calendars import ForexCalendar
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.lineage import AdjustmentPolicy
from traderos.data.timeframes import Timeframe
from traderos.features import FeatureContext, FeatureRequest
from traderos.paper import PaperExecutionConfig
from traderos.regimes import EmaPercentileRegimeDetector
from traderos.research.replay import CanonicalHistoricalReplay, ReplayConfigurationError
from traderos.risk import RiskFirewall, RiskFirewallParameters
from traderos.signals import MajorityVoteFusionPolicy
from traderos.strategies.base import RuleStrategy
from traderos.strategies.models import (
    SignalDirection,
    StrategyContext,
    StrategyMetadata,
    StrategyResult,
)

BASE = datetime(2024, 1, 2, tzinfo=UTC)


class _OnceLongStrategy(RuleStrategy):
    strategy_version = "1"

    def __init__(
        self,
        strategy_id: str,
        decision_at: datetime,
        direction: SignalDirection = SignalDirection.LONG,
        timeframe: Timeframe = Timeframe.H1,
    ) -> None:
        self.strategy_id = strategy_id
        self._decision_at = decision_at
        self._direction = direction
        self._timeframe = timeframe

    @property
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            self.strategy_id,
            self.strategy_version,
            frozenset({AssetClass.FOREX}),
            frozenset({self._timeframe}),
            (),
            0,
            (),
            "deterministic Phase 4 replay fixture",
        )

    @property
    def parameters(self) -> dict[str, int]:
        return {}

    def on_bar(self, context: StrategyContext) -> StrategyResult:
        self._validate_context(context)
        direction = (
            self._direction
            if context.event_timestamp == self._decision_at
            else SignalDirection.HOLD
        )
        return self._result(context, direction, "fixture", ())


def _instrument(symbol: str = "EUR/USD") -> Instrument:
    base, quote = symbol.split("/")
    return Instrument(
        canonical_symbol=symbol,
        asset_class=AssetClass.FOREX,
        base_currency=base,
        quote_currency=quote,
        trading_currency=quote,
        provider_symbols={"fixture": symbol.replace("/", "")},
    )


def _bars(
    count: int = 70,
    *,
    instrument: Instrument | None = None,
    timeframe: Timeframe = Timeframe.H1,
) -> tuple[MarketBar, ...]:
    instrument = instrument or _instrument()
    return tuple(
        MarketBar(
            instrument=instrument,
            timeframe=timeframe,
            adjustment_policy=AdjustmentPolicy.RAW,
            timestamp=BASE + timeframe.duration * index,
            open=Decimal("1.10") + Decimal(index) / Decimal("10000"),
            high=Decimal("1.101") + Decimal(index) / Decimal("10000"),
            low=Decimal("1.099") + Decimal(index) / Decimal("10000"),
            close=(
                Decimal("1.1005")
                + Decimal(index) / Decimal("10000")
                + (Decimal("0.00002") if index % 2 else Decimal("-0.00002"))
            ),
            bid=Decimal("1.1004") + Decimal(index) / Decimal("10000"),
            ask=Decimal("1.1006") + Decimal(index) / Decimal("10000"),
            quote_timestamp=BASE + timeframe.duration * index,
            source="fixture",
            ingestion_timestamp=BASE,
        )
        for index in range(count)
    )


def _replay(
    *,
    reject: bool,
    directions: tuple[SignalDirection, SignalDirection] = (
        SignalDirection.LONG,
        SignalDirection.LONG,
    ),
    maximum_quantity: Decimal = Decimal("1000"),
    bars: tuple[MarketBar, ...] | None = None,
    dataset_version: str = "canonical-replay-fixture",
) -> tuple[CanonicalHistoricalReplay, object, object]:
    bars = bars or _bars()
    timeframe = bars[0].timeframe
    decision_at = bars[min(60, len(bars) - 1)].timestamp + timeframe.duration
    replay = CanonicalHistoricalReplay(
        strategies=(
            _OnceLongStrategy("phase4_a", decision_at, directions[0], timeframe),
            _OnceLongStrategy("phase4_b", decision_at, directions[1], timeframe),
        ),
        regime_detector=EmaPercentileRegimeDetector(),
        fusion_policy=MajorityVoteFusionPolicy(),
        risk_firewall=RiskFirewall(
            RiskFirewallParameters(kill_switch_active=reject, max_trade_notional=Decimal("100"))
        ),
        calendar=ForexCalendar(),
        sizing_config=PaperExecutionConfig(
            quantity_increment=Decimal("1"),
            minimum_quantity=Decimal("1"),
            maximum_quantity=maximum_quantity,
        ),
        account_id="research-fixture",
        account_currency="USD",
        starting_cash=Decimal("1000"),
    )
    features = replay.compute_features(
        bars=bars,
        requests=(
            FeatureRequest(name="ema", version=1, parameters={"window": 10}),
            FeatureRequest(name="ema", version=1, parameters={"window": 30}),
            FeatureRequest(name="volatility_percentile", version=1, parameters={"window": 20}),
        ),
        context=FeatureContext(
            instrument=bars[0].instrument,
            timeframe=timeframe,
            source_dataset_version=dataset_version,
            computation_timestamp=BASE,
        ),
    )
    result = BacktestEngine(
        BacktestConfig(
            dataset_version=dataset_version,
            instrument_symbols=(bars[0].symbol,),
            timeframe=timeframe,
            start=bars[0].timestamp,
            end=bars[-1].timestamp + timeframe.duration,
            starting_cash=Decimal("1000"),
            account_currency="USD",
            strategy_id=replay.strategy_id,
            strategy_version=replay.strategy_version,
            feature_versions=(("ema", 1), ("volatility_percentile", 1)),
        )
    ).run(bars, replay, features=features.observations)
    return replay, result, features


def _trace(
    replay: CanonicalHistoricalReplay, result: object, until: datetime
) -> tuple[object, ...]:
    """Capture causal replay outputs without comparing future-only state."""

    return (
        tuple(event for event in replay.audit_events if event.timestamp <= until),
        tuple(order for order in result.orders if order.submitted_timestamp <= until),
        tuple(fill for fill in result.fills if fill.timestamp <= until),
        tuple(point for point in result.equity_curve if point.timestamp <= until),
    )


def test_canonical_replay_executes_real_phase_boundaries_and_keeps_next_bar_timing() -> None:
    replay, result, features = _replay(reject=False)
    created = next(
        event for event in replay.audit_events if event.terminal_state == "ORDER_CREATED"
    )

    assert created.regime is not None and created.regime.data_session.value == "active"
    assert features.context.source_dataset_version == result.config.dataset_version
    assert len(created.strategy_results) == 2
    strategy_identities = {
        (item.signal.strategy_id, item.signal.strategy_version)
        for item in created.strategy_results
    }
    assert strategy_identities == {
        ("phase4_a", "1"),
        ("phase4_b", "1"),
    }
    assert created.regime.regime_detector_id == "ema_percentile_regime"
    assert created.intent is not None and created.intent.direction.value == "long"
    assert created.intent.policy_id == "majority_vote"
    assert created.risk_decision is not None and created.risk_decision.status.value == "approve"
    assert created.risk_decision.policy_id == "risk_firewall"
    assert len(result.orders) == 1
    assert len(result.fills) == 1
    assert result.orders[0].submitted_timestamp == created.timestamp
    # Phase 3 represents the next bar's open and prior completed-bar decision
    # at their shared boundary timestamp; the fill must use the next-bar quote.
    assert result.fills[0].timestamp == created.timestamp
    assert result.fills[0].price == _bars()[61].ask
    assert result.orders[0].metadata["sizing_configuration_id"]


def test_canonical_replay_risk_veto_creates_no_phase3_order() -> None:
    replay, result, _ = _replay(reject=True)

    rejected = next(
        event for event in replay.audit_events if event.terminal_state == "RISK_REJECTED"
    )
    assert rejected.risk_decision is not None and rejected.risk_decision.status.value == "reject"
    assert result.orders == ()
    assert result.fills == ()


def test_canonical_replay_fusion_no_consensus_creates_no_order_or_fill() -> None:
    replay, result, _ = _replay(
        reject=False, directions=(SignalDirection.LONG, SignalDirection.SHORT)
    )

    rejected = next(
        event
        for event in replay.audit_events
        if event.terminal_state == "FUSION_REJECTED" and event.detail == "no_consensus"
    )
    assert rejected.intent is not None and rejected.risk_decision is None
    assert result.orders == ()
    assert result.fills == ()


def test_canonical_replay_rejects_invalid_configuration_and_oversized_sizing() -> None:
    with pytest.raises(ReplayConfigurationError, match="requires at least"):
        CanonicalHistoricalReplay(
            strategies=(),
            regime_detector=EmaPercentileRegimeDetector(),
            fusion_policy=MajorityVoteFusionPolicy(),
            risk_firewall=RiskFirewall(),
            calendar=ForexCalendar(),
            sizing_config=PaperExecutionConfig(),
            account_id="a",
            account_currency="USD",
            starting_cash=Decimal("1"),
        )
    with pytest.raises(ReplayConfigurationError, match="account configuration"):
        CanonicalHistoricalReplay(
            strategies=(_OnceLongStrategy("a", BASE),),
            regime_detector=EmaPercentileRegimeDetector(),
            fusion_policy=MajorityVoteFusionPolicy(),
            risk_firewall=RiskFirewall(),
            calendar=ForexCalendar(),
            sizing_config=PaperExecutionConfig(),
            account_id="",
            account_currency="USD",
            starting_cash=Decimal("1"),
        )
    with pytest.raises(ReplayConfigurationError, match="unique"):
        CanonicalHistoricalReplay(
            strategies=(_OnceLongStrategy("a", BASE), _OnceLongStrategy("a", BASE)),
            regime_detector=EmaPercentileRegimeDetector(),
            fusion_policy=MajorityVoteFusionPolicy(),
            risk_firewall=RiskFirewall(),
            calendar=ForexCalendar(),
            sizing_config=PaperExecutionConfig(),
            account_id="a",
            account_currency="USD",
            starting_cash=Decimal("1"),
        )
    replay, result, _ = _replay(reject=False, maximum_quantity=Decimal("1"))
    rejected = next(
        event for event in replay.audit_events if event.terminal_state == "SIZING_REJECTED"
    )
    assert rejected.risk_decision is not None
    assert result.orders == ()


def test_canonical_replay_stale_regime_warmup_fails_closed_with_audit_provenance() -> None:
    """The actual Phase 5 detector emits WARMUP, which Phase 6 must reject."""

    replay, result, _ = _replay(reject=False, bars=_bars(count=12))

    rejected = next(
        event for event in replay.audit_events if event.terminal_state == "FUSION_REJECTED"
    )
    assert rejected.regime is not None
    assert rejected.regime.data_session.value == "warmup"
    assert rejected.intent is not None and rejected.intent.status.value == "regime_unavailable"
    assert rejected.risk_decision is None
    assert len(rejected.strategy_results) == 2
    assert result.orders == ()
    assert result.fills == ()


def test_canonical_replay_isolates_cross_instrument_feature_to_execution_state() -> None:
    """A EUR/USD replay cannot mutate an independent GBP/USD replay."""

    eur_bars = _bars()
    gbp_bars = _bars(instrument=_instrument("GBP/USD"))
    gbp_alone, gbp_result_alone, gbp_features_alone = _replay(reject=False, bars=gbp_bars)
    eur_alone, eur_result_alone, eur_features_alone = _replay(reject=False, bars=eur_bars)
    gbp_after, gbp_result_after, gbp_features_after = _replay(reject=False, bars=gbp_bars)
    eur_after, eur_result_after, eur_features_after = _replay(reject=False, bars=eur_bars)

    assert eur_features_alone.observations == eur_features_after.observations
    assert eur_alone.audit_events == eur_after.audit_events
    assert eur_result_alone.orders == eur_result_after.orders
    assert eur_result_alone.fills == eur_result_after.fills
    assert eur_result_alone.equity_curve == eur_result_after.equity_curve
    assert gbp_features_alone.observations == gbp_features_after.observations
    assert gbp_alone.audit_events == gbp_after.audit_events
    assert gbp_result_alone.orders == gbp_result_after.orders
    assert gbp_result_alone.fills == gbp_result_after.fills
    assert gbp_result_alone.equity_curve == gbp_result_after.equity_curve
    assert gbp_result_after.orders[0].instrument.canonical_symbol == "GBP/USD"
    assert gbp_after.audit_events[0].regime is not None
    assert gbp_after.audit_events[0].regime.instrument.canonical_symbol == "GBP/USD"


def test_canonical_replay_isolates_supported_timeframes_end_to_end() -> None:
    """Feature, regime, strategy, and Phase 3 state remain timeframe-specific."""

    hourly, hourly_result, hourly_features = _replay(reject=False, bars=_bars())
    four_hour_bars = _bars(timeframe=Timeframe.H4)
    four_hour, four_hour_result, four_hour_features = _replay(reject=False, bars=four_hour_bars)
    hourly_after, hourly_result_after, hourly_features_after = _replay(reject=False, bars=_bars())

    hourly_created = next(event for event in hourly.audit_events if event.order_id)
    four_hour_created = next(event for event in four_hour.audit_events if event.order_id)
    assert hourly_features.context.timeframe is Timeframe.H1
    assert four_hour_features.context.timeframe is Timeframe.H4
    assert hourly_created.timestamp == BASE + timedelta(hours=61)
    assert four_hour_created.timestamp == BASE + timedelta(hours=61 * 4)
    assert hourly_result.fills[0].timestamp == hourly_created.timestamp
    assert four_hour_result.fills[0].timestamp == four_hour_created.timestamp
    assert hourly_created.regime is not None and hourly_created.regime.timeframe is Timeframe.H1
    assert (
        four_hour_created.regime is not None
        and four_hour_created.regime.timeframe is Timeframe.H4
    )
    assert hourly_features.observations == hourly_features_after.observations
    assert hourly.audit_events == hourly_after.audit_events
    assert hourly_result.orders == hourly_result_after.orders
    assert hourly_result.fills == hourly_result_after.fills


def test_canonical_replay_is_causally_invariant_to_future_mutation() -> None:
    bars = _bars()
    boundary = bars[62].timestamp + Timeframe.H1.duration
    mutated = tuple(
        bar.model_copy(
            update={
                "open": bar.open + Decimal("0.20"),
                "high": bar.high + Decimal("0.20"),
                "low": bar.low + Decimal("0.20"),
                "close": bar.close + Decimal("0.20"),
                "bid": bar.bid + Decimal("0.20") if bar.bid else None,
                "ask": bar.ask + Decimal("0.20") if bar.ask else None,
            }
        )
        if index > 62
        else bar
        for index, bar in enumerate(bars)
    )
    original_replay, original_result, original_features = _replay(reject=False, bars=bars)
    mutated_replay, mutated_result, mutated_features = _replay(reject=False, bars=mutated)

    assert tuple(
        observation
        for observation in original_features.observations
        if observation.decision_timestamp <= boundary
    ) == tuple(
        observation
        for observation in mutated_features.observations
        if observation.decision_timestamp <= boundary
    )
    assert _trace(original_replay, original_result, boundary) == _trace(
        mutated_replay, mutated_result, boundary
    )


def test_canonical_replay_is_causally_invariant_to_future_append() -> None:
    bars = _bars()
    horizon = bars[-1].timestamp + Timeframe.H1.duration
    appended = bars + _bars(count=5)[0:5]
    appended = tuple(
        bar.model_copy(update={"timestamp": horizon + Timeframe.H1.duration * index})
        for index, bar in enumerate(appended[len(bars) :], start=1)
    )
    extended = bars + appended
    original_replay, original_result, _ = _replay(reject=False, bars=bars)
    extended_replay, extended_result, _ = _replay(reject=False, bars=extended)

    assert _trace(original_replay, original_result, horizon) == _trace(
        extended_replay, extended_result, horizon
    )
