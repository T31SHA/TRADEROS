"""Focused Phase 9 qualification, lineage, temporal, and promotion invariants."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from traderos.data.bars import MarketBar
from traderos.data.calendars import ForexCalendar
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.lineage import AdjustmentPolicy
from traderos.data.timeframes import Timeframe
from traderos.research.analysis import (
    benjamini_hochberg_adjusted_p_values,
    trade_path_monte_carlo,
)
from traderos.research.dataset import (
    DatasetQualification,
    DatasetQualificationStatus,
    QualificationPolicy,
    qualify_dataset,
)
from traderos.research.governance import CandidateStatus, ResearchHypothesis, StrategyCandidate
from traderos.research.models import DatasetManifest, ResearchError, TemporalRange
from traderos.research.promotion import (
    PromotionDecision,
    PromotionEvidence,
    PromotionPolicy,
    evaluate_promotion,
)
from traderos.research.validation import ChronologicalValidationProtocol

BASE = datetime(2024, 1, 2, tzinfo=UTC)


def _bars() -> list[MarketBar]:
    instrument = Instrument(
        canonical_symbol="EUR/USD",
        asset_class=AssetClass.FOREX,
        base_currency="EUR",
        quote_currency="USD",
        trading_currency="USD",
        provider_symbols={"fixture": "EURUSD"},
    )
    return [
        MarketBar(
            instrument=instrument,
            timeframe=Timeframe.H1,
            adjustment_policy=AdjustmentPolicy.RAW,
            timestamp=BASE + timedelta(hours=index),
            open=Decimal("1.10"),
            high=Decimal("1.11"),
            low=Decimal("1.09"),
            close=Decimal("1.10"),
            bid=Decimal("1.099"),
            ask=Decimal("1.101"),
            source="fixture",
            ingestion_timestamp=BASE,
        )
        for index in range(12)
    ]


def _manifest() -> DatasetManifest:
    return DatasetManifest.from_bars(
        dataset_version="phase9-fixture-v2",
        bars=_bars(),
        quality_status="verified_fixture",
        historical_membership_available=True,
        delistings_available=True,
        corporate_actions_verified=True,
    )


def test_dataset_qualification_is_hash_bound_and_fails_closed_on_incomplete_quotes() -> None:
    manifest = _manifest()
    qualified = qualify_dataset(
        manifest=manifest,
        bars=_bars(),
        policy=QualificationPolicy("dataset", "1", require_quotes=True),
        checked_at=BASE,
    )
    assert qualified.status is DatasetQualificationStatus.QUALIFIED
    assert (
        qualified.identity
        == qualify_dataset(
            manifest=manifest,
            bars=_bars(),
            policy=QualificationPolicy("dataset", "1", require_quotes=True),
            checked_at=BASE + timedelta(days=1),
        ).identity
    )

    no_quotes = [bar.model_copy(update={"bid": None, "ask": None}) for bar in _bars()]
    no_quote_manifest = DatasetManifest.from_bars(
        dataset_version="phase9-no-quotes",
        bars=no_quotes,
        quality_status="verified_fixture",
        historical_membership_available=True,
        delistings_available=True,
        corporate_actions_verified=True,
    )
    rejected = qualify_dataset(
        manifest=no_quote_manifest,
        bars=no_quotes,
        policy=QualificationPolicy("dataset", "1", require_quotes=True),
        checked_at=BASE,
    )
    assert rejected.status is DatasetQualificationStatus.REJECTED
    with pytest.raises(ResearchError, match="failed closed"):
        rejected.require_qualified()


def test_candidate_lineage_is_deterministic_and_locked_state_cannot_reopen() -> None:
    hypothesis = ResearchHypothesis.create(
        hypothesis_id="h-1",
        name="fixture",
        description="test only",
        asset_class="forex",
        instrument_scope=("EUR/USD",),
        timeframe_scope=("1h",),
        economic_rationale="fixture",
        expected_market_regime="active",
        candidate_strategy="fixture_strategy",
        candidate_parameters={"lookback": 3},
        feature_dependencies=("ema",),
        cost_assumptions="fixed",
        risk_assumptions="canonical firewall",
        research_owner="test",
        created_at=BASE,
        version="1",
    )
    candidate = StrategyCandidate.create(
        dataset=_manifest(),
        family_id="family-1",
        hypothesis_id=hypothesis.hypothesis_id,
        strategy_id="fixture_strategy",
        strategy_version="1",
        parameters={"lookback": 3},
        feature_versions=(("ema", 1),),
        regime_configuration_id="regime-1",
        fusion_configuration_id="fusion-1",
        risk_configuration_id="risk-1",
        creation_experiment_id="creation-1",
        parent_candidate_id=None,
    )
    assert hypothesis.identity.startswith("hypothesis-")
    assert candidate.candidate_id.startswith("candidate-")
    locked = (
        candidate.transition(CandidateStatus.RESEARCH)
        .transition(CandidateStatus.VALIDATING)
        .transition(CandidateStatus.OOS_LOCKED)
    )
    with pytest.raises(ResearchError, match="invalid candidate transition"):
        locked.transition(CandidateStatus.RESEARCH)


def test_chronological_protocol_requires_justified_gaps_and_never_leaks_forward() -> None:
    with pytest.raises(ResearchError, match="dependency reason"):
        ChronologicalValidationProtocol(
            "protocol",
            "1",
            timedelta(hours=3),
            timedelta(hours=1),
            timedelta(hours=1),
            timedelta(hours=1),
            purge=timedelta(hours=1),
        )
    protocol = ChronologicalValidationProtocol(
        "protocol",
        "1",
        timedelta(hours=3),
        timedelta(hours=1),
        timedelta(hours=1),
        timedelta(hours=2),
        purge=timedelta(hours=1),
        embargo=timedelta(hours=1),
        dependency_reason="one-bar holding-period overlap",
    )
    boundary = TemporalRange(BASE, BASE + timedelta(hours=20))
    folds = protocol.folds(start=boundary, boundary=boundary)
    assert folds[0].train.end + folds[0].purge == folds[0].validation.start
    assert folds[0].validation.end + folds[0].embargo == folds[0].test.start
    assert folds[0].test.end <= boundary.end


def test_multiple_testing_monte_carlo_and_promotion_are_deterministic_and_paper_bounded() -> None:
    assert benjamini_hochberg_adjusted_p_values({"a": 0.01, "b": 0.02}) == {"b": 0.02, "a": 0.02}
    first = trade_path_monte_carlo((0.01, -0.02, 0.03), simulations=30, seed=9, block_size=2)
    assert first == trade_path_monte_carlo(
        (0.01, -0.02, 0.03), simulations=30, seed=9, block_size=2
    )
    policy = PromotionPolicy("paper-gate", "1", "fixture policy", minimum_trade_count=5)
    evidence = PromotionEvidence(True, True, True, True, True, True, True, True, None, 5)
    assert evaluate_promotion(policy, evidence) is PromotionDecision.PROMOTION_REVIEW
    assert "live" not in {decision.value for decision in PromotionDecision}

    with pytest.raises(ResearchError, match="justification"):
        PromotionPolicy("paper-gate", "1", "", minimum_trade_count=5)
    assert (
        evaluate_promotion(
            policy,
            PromotionEvidence(False, True, True, True, True, True, True, True, None, 5),
        )
        is PromotionDecision.REJECT
    )
    assert (
        evaluate_promotion(
            policy,
            PromotionEvidence(True, True, True, True, True, True, True, True, None, 4),
        )
        is PromotionDecision.HOLD
    )
    assert (
        evaluate_promotion(
            policy,
            PromotionEvidence(True, True, True, False, True, True, True, True, None, 5),
        )
        is PromotionDecision.REJECT
    )
    with pytest.raises(ResearchError, match="minimum trade"):
        PromotionPolicy("paper-gate", "1", "policy", minimum_trade_count=0)
    with pytest.raises(ResearchError, match="p-value"):
        PromotionPolicy(
            "paper-gate", "1", "policy", minimum_trade_count=1, allowed_adjusted_p_value=0
        )
    p_value_policy = PromotionPolicy(
        "paper-gate", "1", "policy", minimum_trade_count=1, allowed_adjusted_p_value=0.05
    )
    assert (
        evaluate_promotion(
            p_value_policy,
            PromotionEvidence(True, True, True, True, True, True, True, True, None, 1),
        )
        is PromotionDecision.HOLD
    )
    assert (
        evaluate_promotion(
            p_value_policy,
            PromotionEvidence(True, True, True, True, True, True, True, True, 0.1, 1),
        )
        is PromotionDecision.REJECT
    )


def test_qualification_and_governance_reject_incomplete_artifacts() -> None:
    with pytest.raises(ResearchError, match="policy identity"):
        QualificationPolicy("", "1")
    with pytest.raises(ResearchError, match="hypothesis fields"):
        ResearchHypothesis.create(
            hypothesis_id="h",
            name="",
            description="d",
            asset_class="forex",
            instrument_scope=("EUR/USD",),
            timeframe_scope=("1h",),
            economic_rationale="r",
            expected_market_regime="active",
            candidate_strategy="s",
            candidate_parameters={},
            feature_dependencies=(),
            cost_assumptions="c",
            risk_assumptions="r",
            research_owner="o",
            created_at=BASE,
            version="1",
        )
    with pytest.raises(ResearchError, match="instrument and timeframe"):
        ResearchHypothesis.create(
            hypothesis_id="h",
            name="n",
            description="d",
            asset_class="forex",
            instrument_scope=(),
            timeframe_scope=("1h",),
            economic_rationale="r",
            expected_market_regime="active",
            candidate_strategy="s",
            candidate_parameters={},
            feature_dependencies=(),
            cost_assumptions="c",
            risk_assumptions="r",
            research_owner="o",
            created_at=BASE,
            version="1",
        )
    with pytest.raises(ResearchError, match="candidate lineage"):
        StrategyCandidate("", "h", "s", "1", (), (), None, None, None, "dataset", "creation", None)
    manifest = _manifest()
    with pytest.raises(ResearchError, match="counts"):
        DatasetQualification(
            manifest, "policy", DatasetQualificationStatus.QUALIFIED, BASE, 0, 0, 0, 1, (), ()
        )
    with pytest.raises(ResearchError, match="quote coverage"):
        DatasetQualification(
            manifest, "policy", DatasetQualificationStatus.QUALIFIED, BASE, 1, 0, 0, 2, (), ()
        )
    with pytest.raises(ResearchError, match="cannot retain"):
        DatasetQualification(
            manifest, "policy", DatasetQualificationStatus.QUALIFIED, BASE, 1, 0, 0, 1, (), ("bad",)
        )
    with pytest.raises(ResearchError, match="requires explicit"):
        DatasetQualification(
            manifest, "policy", DatasetQualificationStatus.REJECTED, BASE, 1, 0, 0, 1, (), ()
        )
    assert (
        "qualification_id"
        in DatasetQualification(
            manifest, "policy", DatasetQualificationStatus.QUALIFIED, BASE, 1, 0, 0, 1, (), ()
        ).canonical()
    )
    with pytest.raises(ResearchError, match="requires bars"):
        qualify_dataset(
            manifest=manifest,
            bars=(),
            policy=QualificationPolicy("dataset", "1"),
            checked_at=BASE,
        )
    with pytest.raises(ResearchError, match="do not match"):
        qualify_dataset(
            manifest=manifest,
            bars=[
                _bars()[0].model_copy(update={"close": Decimal("1.12"), "high": Decimal("1.12")})
            ],
            policy=QualificationPolicy("dataset", "1"),
            checked_at=BASE,
        )
    with_calendar = qualify_dataset(
        manifest=manifest,
        bars=_bars(),
        policy=QualificationPolicy("dataset", "1"),
        checked_at=BASE,
        calendar=ForexCalendar(),
    )
    assert with_calendar.status is DatasetQualificationStatus.QUALIFIED
