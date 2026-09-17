"""Canonical Phase 2→5→4→6→7→8-sizing→3 replay coverage."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
from traderos.research.replay import CanonicalHistoricalReplay
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

    def __init__(self, strategy_id: str, decision_at: datetime) -> None:
        self.strategy_id = strategy_id
        self._decision_at = decision_at

    @property
    def metadata(self) -> StrategyMetadata:
        return StrategyMetadata(
            self.strategy_id,
            self.strategy_version,
            frozenset({AssetClass.FOREX}),
            frozenset({Timeframe.H1}),
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
            SignalDirection.LONG
            if context.event_timestamp == self._decision_at
            else SignalDirection.HOLD
        )
        return self._result(context, direction, "fixture", ())


def _bars(count: int = 70) -> tuple[MarketBar, ...]:
    instrument = Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
        trading_currency="USD",
        provider_symbols={"fixture": "EURUSD"},
    )
    return tuple(
        MarketBar(
            instrument=instrument,
            timeframe=Timeframe.H1,
            adjustment_policy=AdjustmentPolicy.RAW,
            timestamp=BASE + timedelta(hours=index),
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
            source="fixture",
            ingestion_timestamp=BASE,
        )
        for index in range(count)
    )


def _replay(*, reject: bool) -> tuple[CanonicalHistoricalReplay, object]:
    bars = _bars()
    decision_at = bars[60].timestamp + Timeframe.H1.duration
    replay = CanonicalHistoricalReplay(
        strategies=(
            _OnceLongStrategy("phase4_a", decision_at),
            _OnceLongStrategy("phase4_b", decision_at),
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
            maximum_quantity=Decimal("1000"),
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
            timeframe=Timeframe.H1,
            source_dataset_version="canonical-replay-fixture",
            computation_timestamp=BASE,
        ),
    )
    result = BacktestEngine(
        BacktestConfig(
            dataset_version="canonical-replay-fixture",
            instrument_symbols=("EUR/USD",),
            timeframe=Timeframe.H1,
            start=bars[0].timestamp,
            end=bars[-1].timestamp + Timeframe.H1.duration,
            starting_cash=Decimal("1000"),
            account_currency="USD",
            strategy_id=replay.strategy_id,
            strategy_version=replay.strategy_version,
            feature_versions=(("ema", 1), ("volatility_percentile", 1)),
        )
    ).run(bars, replay, features=features.observations)
    return replay, result


def test_canonical_replay_executes_real_phase_boundaries_and_keeps_next_bar_timing() -> None:
    replay, result = _replay(reject=False)
    created = next(
        event for event in replay.audit_events if event.terminal_state == "ORDER_CREATED"
    )

    assert created.regime is not None and created.regime.data_session.value == "active"
    assert len(created.strategy_results) == 2
    assert created.intent is not None and created.intent.direction.value == "long"
    assert created.risk_decision is not None and created.risk_decision.status.value == "approve"
    assert len(result.orders) == 1
    assert len(result.fills) == 1
    assert result.orders[0].submitted_timestamp == created.timestamp
    # Phase 3 represents the next bar's open and prior completed-bar decision
    # at their shared boundary timestamp; the fill must use the next-bar quote.
    assert result.fills[0].timestamp == created.timestamp
    assert result.fills[0].price == _bars()[61].ask


def test_canonical_replay_risk_veto_creates_no_phase3_order() -> None:
    replay, result = _replay(reject=True)

    rejected = next(
        event for event in replay.audit_events if event.terminal_state == "RISK_REJECTED"
    )
    assert rejected.risk_decision is not None and rejected.risk_decision.status.value == "reject"
    assert result.orders == ()
    assert result.fills == ()
