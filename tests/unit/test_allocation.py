"""SOFTWARE FIXTURES for deterministic portfolio allocation; never approvals."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from traderos.allocation import (
    AllocationConflictPolicy,
    AllocationPolicy,
    AllocationStatus,
    AssetClassBudget,
    BudgetLimit,
    DeterministicPortfolioAllocator,
    ExposureSnapshot,
    PortfolioAllocationSnapshot,
    SignalSemantics,
    StrategyAllocationSignal,
)
from traderos.data.instruments import AssetClass, Instrument
from traderos.data.timeframes import Timeframe
from traderos.strategies.models import SignalDirection

NOW = datetime(2026, 1, 5, 13, tzinfo=UTC)
USD_INSTRUMENT = Instrument(
    canonical_symbol="EUR/USD",
    asset_class=AssetClass.FOREX,
    base_currency="EUR",
    quote_currency="USD",
    trading_currency="USD",
)
EQUITY = Instrument(
    canonical_symbol="ABC",
    asset_class=AssetClass.EQUITY,
    base_currency="ABC",
    quote_currency="USD",
    trading_currency="USD",
)


def _policy(
    *,
    gross: str = "100",
    net: str = "100",
    incremental: str = "100",
    turnover: str = "100",
    instrument: str = "100",
    family: str = "100",
    strategy: str = "100",
    conflict: AllocationConflictPolicy = AllocationConflictPolicy.NET_OPPOSING,
) -> AllocationPolicy:
    return AllocationPolicy(
        policy_id="fixture-allocation",
        policy_version="1",
        max_gross_exposure=Decimal(gross),
        max_net_exposure=Decimal(net),
        max_incremental_exposure=Decimal(incremental),
        max_turnover_per_decision=Decimal(turnover),
        instrument_limits=(BudgetLimit("EUR/USD", Decimal(instrument)),),
        strategy_budgets=(
            BudgetLimit("alpha@1", Decimal(strategy)),
            BudgetLimit("beta@1", Decimal(strategy)),
        ),
        family_budgets=(BudgetLimit("shared", Decimal(family)),),
        asset_class_limits=(AssetClassBudget(AssetClass.FOREX, Decimal(gross)),),
        conflict_policy=conflict,
        allocation_increment=Decimal("0.01"),
    )


def _signal(
    strategy: str,
    amount: str,
    *,
    direction: SignalDirection = SignalDirection.LONG,
    source: str | None = None,
    instrument: Instrument = USD_INSTRUMENT,
    family: str = "shared",
) -> StrategyAllocationSignal:
    return StrategyAllocationSignal(
        source_signal_id=source or f"{strategy}-{direction.value}",
        strategy_id=strategy,
        strategy_version="1",
        artifact_hash=f"artifact-{strategy}",
        strategy_family_id=family,
        instrument=instrument,
        timeframe=Timeframe.H1,
        event_timestamp=NOW,
        availability_timestamp=NOW,
        decision_timestamp=NOW,
        direction=direction,
        semantics=SignalSemantics.DIRECTIONAL_NOTIONAL_REQUEST,
        reason="fixture directional request",
        configuration_identity=f"config-{strategy}",
        requested_notional=Decimal(amount),
    )


def _snapshot(
    *,
    positions: tuple[ExposureSnapshot, ...] = (),
    reservations: tuple[ExposureSnapshot, ...] = (),
    revision: int = 7,
) -> PortfolioAllocationSnapshot:
    return PortfolioAllocationSnapshot(
        account_id="fixture-account",
        account_currency="USD",
        timestamp=NOW,
        revision=revision,
        positions=positions,
        reservations=reservations,
    )


def test_allocation_is_deterministic_order_invariant_and_collapses_duplicates() -> None:
    allocator = DeterministicPortfolioAllocator()
    policy = _policy(gross="100", family="100")
    alpha = _signal("alpha", "80", source="alpha-source")
    beta = _signal("beta", "80", source="beta-source")
    duplicate = _signal("alpha", "80", source="alpha-source")

    first = allocator.evaluate((alpha, beta, duplicate), _snapshot(), policy)
    second = allocator.evaluate((beta, duplicate, alpha), _snapshot(), policy)

    assert first == second
    assert first.status is AllocationStatus.REDUCED
    assert [item.proposed_delta for item in first.contributions] == [
        Decimal("50.00"),
        Decimal("50.00"),
    ]
    assert "DUPLICATE_STRATEGY_EVIDENCE_COLLAPSED" in first.constraint_reasons


def test_opposing_signals_follow_explicit_net_policy() -> None:
    proposal = DeterministicPortfolioAllocator().evaluate(
        (_signal("alpha", "60"), _signal("beta", "40", direction=SignalDirection.SHORT)),
        _snapshot(),
        _policy(family="100"),
    )

    assert proposal.targets[0].proposed_target_exposure == Decimal("20")
    assert proposal.max_new_notional == Decimal("20")
    assert "CONFLICTING_SIGNALS_NETTED" not in proposal.constraint_reasons
    assert proposal.targets[0].required_delta == Decimal("20")


def test_shared_family_budget_is_pro_rata_and_account_constraints_are_visible() -> None:
    proposal = DeterministicPortfolioAllocator().evaluate(
        (_signal("alpha", "80"), _signal("beta", "80")),
        _snapshot(),
        _policy(family="100"),
    )

    assert [item.proposed_delta for item in proposal.contributions] == [
        Decimal("50.00"),
        Decimal("50.00"),
    ]
    assert "FAMILY_BUDGET_REDUCED" in proposal.constraint_reasons


def test_instrument_and_gross_budgets_include_existing_positions() -> None:
    position = ExposureSnapshot(USD_INSTRUMENT, Decimal("90"), "USD")
    proposal = DeterministicPortfolioAllocator().evaluate(
        (_signal("alpha", "50"),),
        _snapshot(positions=(position,)),
        _policy(instrument="100", gross="100"),
    )

    target = proposal.targets[0]
    assert target.existing_signed_exposure == Decimal("90")
    assert target.proposed_target_exposure == Decimal("100.00")
    assert target.new_risk_notional == Decimal("10.00")
    assert "INSTRUMENT_BUDGET_REDUCED" in proposal.constraint_reasons


def test_authoritative_asset_class_budget_limits_new_risk() -> None:
    policy = replace(
        _policy(gross="200", instrument="200"),
        asset_class_limits=(AssetClassBudget(AssetClass.EQUITY, Decimal("50")),),
    )
    proposal = DeterministicPortfolioAllocator().evaluate(
        (_signal("alpha", "80", instrument=EQUITY),),
        _snapshot(),
        policy,
    )

    assert proposal.targets[0].proposed_target_exposure == Decimal("50.00")
    assert "ASSET_CLASS_BUDGET_REDUCED" in proposal.constraint_reasons


def test_net_incremental_and_turnover_budgets_are_independent_constraints() -> None:
    position = ExposureSnapshot(USD_INSTRUMENT, Decimal("40"), "USD")
    allocator = DeterministicPortfolioAllocator()
    net_limited = allocator.evaluate(
        (_signal("alpha", "50"),),
        _snapshot(positions=(position,)),
        _policy(net="50", instrument="200", gross="200"),
    )
    incremental_limited = allocator.evaluate(
        (_signal("alpha", "50"),),
        _snapshot(),
        _policy(incremental="20", instrument="200", gross="200"),
    )
    turnover_limited = allocator.evaluate(
        (_signal("alpha", "50"),),
        _snapshot(),
        _policy(turnover="20", instrument="200", gross="200"),
    )

    assert net_limited.targets[0].required_delta == Decimal("10.00")
    assert "NET_BUDGET_REDUCED" in net_limited.constraint_reasons
    assert incremental_limited.max_new_notional == Decimal("20.00")
    assert "INCREMENTAL_BUDGET_REDUCED" in incremental_limited.constraint_reasons
    assert turnover_limited.targets[0].required_delta == Decimal("20.00")
    assert "TURNOVER_BUDGET_REDUCED" in turnover_limited.constraint_reasons


def test_pending_reservations_consume_capacity_and_remain_attributed_separately() -> None:
    position = ExposureSnapshot(USD_INSTRUMENT, Decimal("50"), "USD")
    reservation = ExposureSnapshot(
        USD_INSTRUMENT, Decimal("40"), "USD", reservation_id="pending-1"
    )
    proposal = DeterministicPortfolioAllocator().evaluate(
        (_signal("alpha", "30"),),
        _snapshot(positions=(position,), reservations=(reservation,)),
        _policy(instrument="100", gross="100"),
    )

    target = proposal.targets[0]
    assert target.existing_signed_exposure == Decimal("50")
    assert target.reserved_signed_exposure == Decimal("40")
    assert target.proposed_target_exposure == Decimal("99.99")
    assert target.required_delta == Decimal("9.99")


def test_existing_over_limit_is_not_hidden_but_reductions_are_allowed() -> None:
    position = ExposureSnapshot(USD_INSTRUMENT, Decimal("120"), "USD")
    allocator = DeterministicPortfolioAllocator()
    over_limit = allocator.evaluate(
        (_signal("alpha", "10"),),
        _snapshot(positions=(position,)),
        _policy(instrument="100", gross="100"),
    )
    reduction = allocator.evaluate(
        (_signal("alpha", "20", direction=SignalDirection.SHORT),),
        _snapshot(positions=(position,)),
        _policy(instrument="100", gross="100"),
    )

    assert over_limit.targets[0].proposed_target_exposure == Decimal("120")
    assert over_limit.max_new_notional == Decimal("0")
    assert "EXISTING_EXPOSURE_OVER_BUDGET" in over_limit.constraint_reasons
    assert reduction.targets[0].reduction_notional == Decimal("20")
    assert reduction.max_new_notional == Decimal("0")
    assert reduction.max_reduction_notional == Decimal("20")


def test_reductions_are_distinct_from_reversals() -> None:
    position = ExposureSnapshot(USD_INSTRUMENT, Decimal("100"), "USD")
    allocator = DeterministicPortfolioAllocator()
    reduction = allocator.evaluate(
        (_signal("alpha", "40", direction=SignalDirection.SHORT),),
        _snapshot(positions=(position,)),
        _policy(),
    )
    reversal = allocator.evaluate(
        (_signal("alpha", "150", direction=SignalDirection.SHORT),),
        _snapshot(positions=(position,)),
        _policy(
            gross="300",
            incremental="300",
            turnover="300",
            instrument="300",
            family="200",
            strategy="200",
        ),
    )

    assert reduction.max_reduction_notional == Decimal("40")
    assert reduction.max_new_notional == Decimal("0")
    assert reversal.max_reduction_notional == Decimal("100")
    assert reversal.max_new_notional == Decimal("50")


def test_missing_conversion_inputs_block_and_stale_revision_cannot_validate() -> None:
    cross_currency = ExposureSnapshot(USD_INSTRUMENT, Decimal("10"), "EUR")
    proposal = DeterministicPortfolioAllocator().evaluate(
        (_signal("alpha", "10"),),
        _snapshot(positions=(cross_currency,)),
        _policy(),
    )

    assert proposal.status is AllocationStatus.BLOCKED
    assert proposal.constraint_reasons == ("ALLOCATION_CONVERSION_INPUT_UNAVAILABLE",)

    valid = DeterministicPortfolioAllocator().evaluate(
        (_signal("alpha", "10"),), _snapshot(), _policy()
    )
    assert valid.validate_snapshot(account_id="fixture-account", revision=7)
    assert not valid.validate_snapshot(account_id="fixture-account", revision=8)


def test_explicit_conversion_input_is_consumed_without_guessing() -> None:
    converted = ExposureSnapshot(
        USD_INSTRUMENT,
        Decimal("10"),
        "EUR",
        conversion_rate_to_account=Decimal("1.10"),
        conversion_identity="fixture-eurusd-quote-1",
    )
    proposal = DeterministicPortfolioAllocator().evaluate(
        (_signal("alpha", "20"),),
        _snapshot(positions=(converted,)),
        _policy(instrument="100", gross="100"),
    )

    assert proposal.targets[0].existing_signed_exposure == Decimal("11.00")
    assert proposal.targets[0].proposed_target_exposure == Decimal("31.00")


def test_reject_opposing_policy_does_not_silently_net_conflicts() -> None:
    proposal = DeterministicPortfolioAllocator().evaluate(
        (_signal("alpha", "60"), _signal("alpha", "40", direction=SignalDirection.SHORT)),
        _snapshot(),
        _policy(conflict=AllocationConflictPolicy.REJECT_OPPOSING),
    )

    assert proposal.status is AllocationStatus.REJECTED
    assert proposal.max_new_notional == Decimal("0")
    assert "CONFLICTING_SIGNALS_REJECTED" in proposal.constraint_reasons


def test_missing_policy_and_missing_signals_are_fail_closed() -> None:
    allocator = DeterministicPortfolioAllocator()
    no_policy = allocator.evaluate((_signal("alpha", "10"),), _snapshot(), None)
    no_signal = allocator.evaluate((), _snapshot(), _policy())

    assert no_policy.status is AllocationStatus.BLOCKED
    assert no_policy.constraint_reasons == ("ALLOCATION_POLICY_UNAVAILABLE",)
    assert no_signal.status is AllocationStatus.REJECTED
    assert no_signal.constraint_reasons == ("ALLOCATION_SIGNAL_UNAVAILABLE",)


def test_policy_identity_changes_with_authority_changes() -> None:
    original = _policy()
    changed = _policy(gross="101")

    assert original.configuration_id != changed.configuration_id
