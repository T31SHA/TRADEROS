"""Real Phase 4 → 5 → 6 → 7 boundary integration coverage."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from traderos.data.instruments import AssetClass, Instrument
from traderos.data.timeframes import Timeframe
from traderos.database.connection import create_database_engine
from traderos.paper import PaperQuote, PaperTradingEngine, SqlAlchemyPaperStore
from traderos.regimes import (
    DataSessionState,
    LiquidityRegime,
    RegimeState,
    TrendRegime,
    VolatilityRegime,
)
from traderos.risk import (
    MarketDataStatus,
    MarketRiskSnapshot,
    PortfolioRiskSnapshot,
    RiskDecisionStatus,
    RiskEvaluationContext,
    RiskFirewall,
    SystemHealthSnapshot,
    SystemHealthStatus,
)
from traderos.signals import FusionContext, MajorityVoteFusionPolicy, SignalFusionEngine
from traderos.strategies import FeatureRequirement, SignalDirection, StrategySignal


def test_strategy_signal_regime_fusion_and_firewall_preserve_the_safety_boundary() -> None:
    timestamp = datetime(2024, 1, 2, 12, tzinfo=UTC)
    instrument = Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
        trading_currency="USD",
    )
    regime = RegimeState(
        state_id="regime-1",
        instrument=instrument,
        asset_class=AssetClass.FOREX,
        timeframe=Timeframe.H1,
        decision_timestamp=timestamp,
        regime_detector_id="fixture",
        regime_detector_version="1",
        configuration_id="fixture-config",
        trend=TrendRegime.TRENDING_UP,
        volatility=VolatilityRegime.NORMAL,
        liquidity=LiquidityRegime.NORMAL,
        data_session=DataSessionState.ACTIVE,
        feature_provenance=(),
    )
    signal = StrategySignal(
        signal_id="phase4-signal",
        strategy_id="forex_trend_following",
        strategy_version="1",
        instrument=instrument,
        timestamp=timestamp,
        timeframe=Timeframe.H1,
        direction=SignalDirection.LONG,
        reason="causal_fixture",
        feature_provenance=(FeatureRequirement("ema", 1),),
    )
    intent = SignalFusionEngine(MajorityVoteFusionPolicy()).fuse(
        FusionContext(instrument, Timeframe.H1, timestamp, (signal,), regime)
    )
    decision = RiskFirewall().evaluate(
        RiskEvaluationContext(
            decision_timestamp=timestamp,
            intent=intent,
            regime_state=regime,
            market=MarketRiskSnapshot(
                instrument, timestamp, MarketDataStatus.VALID, Decimal("1.1000"), Decimal("1.1001")
            ),
            portfolio=PortfolioRiskSnapshot(
                timestamp=timestamp,
                account_id="pipeline-account",
                account_currency="USD",
                risk_day=date(2024, 1, 2),
                equity=Decimal("10000"),
                cash=Decimal("10000"),
                margin_used=Decimal("0"),
                margin_available=Decimal("10000"),
                daily_pnl=Decimal("0"),
                high_water_mark=Decimal("10000"),
            ),
            system_health=SystemHealthSnapshot(timestamp, SystemHealthStatus.HEALTHY),
        )
    )
    assert decision.status is RiskDecisionStatus.APPROVE
    assert decision.authorization is not None
    assert decision.account_id == "pipeline-account"
    assert decision.authorization.require_pretrade_sizing
    assert "quantity" not in decision.__dataclass_fields__
    assert timestamp + timedelta(hours=1) > decision.decision_timestamp


def test_real_phase4_to_phase8_pipeline_requires_the_firewall_decision() -> None:
    """Exercise the real signal, regime, fusion, risk, paper, and fill boundaries."""

    timestamp = datetime(2024, 1, 2, 12, tzinfo=UTC)
    instrument = Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
        trading_currency="USD",
    )
    regime = RegimeState(
        state_id="phase8-regime",
        instrument=instrument,
        asset_class=AssetClass.FOREX,
        timeframe=Timeframe.H1,
        decision_timestamp=timestamp,
        regime_detector_id="fixture",
        regime_detector_version="1",
        configuration_id="fixture-config",
        trend=TrendRegime.TRENDING_UP,
        volatility=VolatilityRegime.NORMAL,
        liquidity=LiquidityRegime.NORMAL,
        data_session=DataSessionState.ACTIVE,
        feature_provenance=(),
    )
    signal = StrategySignal(
        signal_id="phase8-signal",
        strategy_id="forex_trend_following",
        strategy_version="1",
        instrument=instrument,
        timestamp=timestamp,
        timeframe=Timeframe.H1,
        direction=SignalDirection.LONG,
        reason="causal_fixture",
        feature_provenance=(FeatureRequirement("ema", 1),),
    )
    intent = SignalFusionEngine(MajorityVoteFusionPolicy()).fuse(
        FusionContext(instrument, Timeframe.H1, timestamp, (signal,), regime)
    )
    firewall = RiskFirewall()
    decision = firewall.evaluate(
        RiskEvaluationContext(
            decision_timestamp=timestamp,
            intent=intent,
            regime_state=regime,
            market=MarketRiskSnapshot(
                instrument, timestamp, MarketDataStatus.VALID, Decimal("1.1000"), Decimal("1.1001")
            ),
            portfolio=PortfolioRiskSnapshot(
                timestamp=timestamp,
                account_id="phase8-pipeline",
                account_currency="USD",
                risk_day=date(2024, 1, 2),
                equity=Decimal("10000"),
                cash=Decimal("10000"),
                margin_used=Decimal("0"),
                margin_available=Decimal("10000"),
                daily_pnl=Decimal("0"),
                high_water_mark=Decimal("10000"),
            ),
            system_health=SystemHealthSnapshot(timestamp, SystemHealthStatus.HEALTHY),
        )
    )
    assert decision.status is RiskDecisionStatus.APPROVE

    store = SqlAlchemyPaperStore(create_database_engine("sqlite+pysqlite:///:memory:"))
    store.create_schema_for_testing()
    paper = PaperTradingEngine(store)
    paper.create_account(
        account_id="phase8-pipeline",
        account_currency="USD",
        starting_cash=Decimal("10000"),
        risk_capacity=Decimal("10000"),
        daily_loss_limit=Decimal("500"),
        max_drawdown=Decimal("1000"),
        risk_policy_id=firewall.policy_id,
        risk_policy_configuration_id=firewall.configuration_id,
        timestamp=timestamp,
    )
    order = paper.submit(
        account_id="phase8-pipeline",
        idempotency_key="phase4-through-phase8",
        risk_decision=decision,
        quote=PaperQuote(instrument, timestamp, Decimal("1.1000"), Decimal("1.1001")),
        timestamp=timestamp,
    )
    fills = paper.process_quote(
        account_id="phase8-pipeline",
        quote=PaperQuote(
            instrument,
            timestamp + timedelta(seconds=1),
            Decimal("1.1000"),
            Decimal("1.1001"),
        ),
        timestamp=timestamp + timedelta(seconds=1),
    )
    assert order.quantity > 0
    assert len(fills) == 1
    assert paper.portfolio_risk_snapshot("phase8-pipeline").positions[0].net_notional > 0
    store.engine.dispose()
