"""Real Phase 4 → 5 → 6 → 7 boundary integration coverage."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from traderos.data.instruments import AssetClass, Instrument
from traderos.data.timeframes import Timeframe
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
