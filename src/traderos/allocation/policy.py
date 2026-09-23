"""Pure deterministic portfolio allocation evaluator."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import NamedTuple

from traderos.allocation.models import (
    AllocationConflictPolicy,
    AllocationContribution,
    AllocationPolicy,
    AllocationProposal,
    AllocationStatus,
    AllocationTarget,
    PortfolioAllocationSnapshot,
    StrategyAllocationSignal,
    deterministic_allocation_decision_id,
)
from traderos.data.instruments import AssetClass, Instrument

_ZERO = Decimal("0")
_ONE = Decimal("1")
_CONSTRAINTS = (
    "account_gross",
    "account_net",
    "asset_class",
    "family_contribution",
    "incremental_exposure",
    "instrument",
    "strategy_contribution",
    "turnover",
)


class _GroupedSignal(NamedTuple):
    strategy_id: str
    strategy_version: str
    artifact_hash: str
    family_id: str
    instrument: Instrument
    requested_delta: Decimal
    source_count: int
    conflict: bool


@dataclass
class _MutableGroup:
    identity: _GroupedSignal
    value: Decimal
    requested: Decimal
    reason_codes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _ExposureTotals:
    position_signed: dict[str, Decimal]
    reservation_signed: dict[str, Decimal]
    position_gross: dict[str, Decimal]
    reservation_gross: dict[str, Decimal]
    instruments: dict[str, Instrument]
    net: Decimal
    gross: Decimal


class DeterministicPortfolioAllocator:
    """Evaluate bounded proposals without mutation or account authority."""

    def evaluate(
        self,
        signals: tuple[StrategyAllocationSignal, ...],
        snapshot: PortfolioAllocationSnapshot,
        policy: AllocationPolicy | None,
    ) -> AllocationProposal:
        """Return a stable proposal; missing authority always blocks."""

        ordered_signals = tuple(
            sorted(
                signals,
                key=lambda item: (
                    item.instrument.canonical_symbol,
                    item.strategy_id,
                    item.strategy_version,
                    item.artifact_hash,
                    item.strategy_family_id,
                    item.source_signal_id,
                ),
            )
        )
        decision_timestamp = (
            ordered_signals[0].decision_timestamp if ordered_signals else snapshot.timestamp
        )
        if policy is None:
            return self._blocked(
                snapshot,
                decision_timestamp,
                "ALLOCATION_POLICY_UNAVAILABLE",
                policy_id="unconfigured",
                policy_version="0",
                configuration_id="unconfigured",
            )
        if snapshot.timestamp > decision_timestamp:
            return self._blocked(
                snapshot,
                decision_timestamp,
                "ALLOCATION_SNAPSHOT_FUTURE_DATED",
                policy=policy,
            )
        if any(item.decision_timestamp != decision_timestamp for item in ordered_signals):
            return self._blocked(
                snapshot,
                decision_timestamp,
                "ALLOCATION_DECISION_TIMESTAMP_MISMATCH",
                policy=policy,
            )
        try:
            exposure = self._exposure(snapshot)
        except ValueError:
            return self._blocked(
                snapshot,
                decision_timestamp,
                "ALLOCATION_CONVERSION_INPUT_UNAVAILABLE",
                policy=policy,
            )
        if not ordered_signals:
            return self._proposal(
                snapshot=snapshot,
                decision_timestamp=decision_timestamp,
                policy=policy,
                status=AllocationStatus.REJECTED,
                targets=(),
                contributions=(),
                constraints=_CONSTRAINTS,
                reasons=("ALLOCATION_SIGNAL_UNAVAILABLE",),
                max_new=_ZERO,
                max_reduction=_ZERO,
            )

        groups = self._group(ordered_signals, policy)
        self._cap_groups(groups, policy, scope="strategy")
        self._cap_family_groups(groups, policy)
        self._apply_portfolio_constraints(groups, exposure, policy)

        by_instrument: dict[str, list[_MutableGroup]] = defaultdict(list)
        for group in groups:
            by_instrument[group.identity.instrument.canonical_symbol].append(group)
        targets: list[AllocationTarget] = []
        contributions: list[AllocationContribution] = []
        total_new = _ZERO
        total_reduction = _ZERO
        for symbol in sorted(by_instrument):
            instrument_groups = by_instrument[symbol]
            instrument = instrument_groups[0].identity.instrument
            position = exposure.position_signed.get(symbol, _ZERO)
            reserved = exposure.reservation_signed.get(symbol, _ZERO)
            position_gross = exposure.position_gross.get(symbol, _ZERO)
            reserved_gross = exposure.reservation_gross.get(symbol, _ZERO)
            base = position + reserved
            delta = sum((group.value for group in instrument_groups), _ZERO)
            target = base + delta
            reduction = min(abs(delta), abs(base)) if delta * base < _ZERO else _ZERO
            new_risk = abs(delta) - reduction
            target_reasons = tuple(
                sorted({reason for group in instrument_groups for reason in group.reason_codes})
            )
            if position_gross + reserved_gross > policy.instrument_budget(instrument):
                target_reasons = tuple(sorted({*target_reasons, "EXISTING_EXPOSURE_OVER_BUDGET"}))
            targets.append(
                AllocationTarget(
                    instrument=instrument,
                    existing_signed_exposure=position,
                    reserved_signed_exposure=reserved,
                    existing_gross_exposure=position_gross,
                    reserved_gross_exposure=reserved_gross,
                    proposed_target_exposure=target,
                    required_delta=delta,
                    new_risk_notional=new_risk,
                    reduction_notional=reduction,
                    reason_codes=target_reasons,
                )
            )
            total_new += new_risk
            total_reduction += reduction
            for group in sorted(instrument_groups, key=self._group_key):
                group_reduction = (
                    min(abs(group.value), abs(base)) if group.value * base < _ZERO else _ZERO
                )
                group_new = abs(group.value) - group_reduction
                contributions.append(
                    AllocationContribution(
                        strategy_id=group.identity.strategy_id,
                        strategy_version=group.identity.strategy_version,
                        artifact_hash=group.identity.artifact_hash,
                        strategy_family_id=group.identity.family_id,
                        instrument=instrument,
                        requested_delta=group.requested,
                        proposed_delta=group.value,
                        reduction_notional=group_reduction,
                        new_risk_notional=group_new,
                        reason_codes=tuple(sorted(set(group.reason_codes))),
                    )
                )

        reasons = tuple(
            sorted(
                {reason for item in targets for reason in item.reason_codes}
                | {reason for item in contributions for reason in item.reason_codes}
            )
        )
        requested = sum((abs(group.requested) for group in groups), _ZERO)
        proposed = sum((abs(group.value) for group in groups), _ZERO)
        status = (
            AllocationStatus.APPROVED
            if proposed == requested and proposed > _ZERO
            else AllocationStatus.REDUCED
            if proposed > _ZERO
            else AllocationStatus.REJECTED
        )
        return self._proposal(
            snapshot=snapshot,
            decision_timestamp=decision_timestamp,
            policy=policy,
            status=status,
            targets=tuple(sorted(targets, key=lambda item: item.instrument.canonical_symbol)),
            contributions=tuple(
                sorted(
                    contributions,
                    key=lambda item: (
                        item.instrument.canonical_symbol,
                        item.strategy_id,
                        item.strategy_version,
                        item.artifact_hash,
                    ),
                )
            ),
            constraints=_CONSTRAINTS,
            reasons=reasons,
            max_new=total_new,
            max_reduction=total_reduction,
        )

    @staticmethod
    def _group(
        signals: tuple[StrategyAllocationSignal, ...], policy: AllocationPolicy
    ) -> list[_MutableGroup]:
        grouped: dict[tuple[str, str, str, str, str, str, str], list[StrategyAllocationSignal]] = {}
        for signal in signals:
            key = (
                signal.strategy_id,
                signal.strategy_version,
                signal.artifact_hash,
                signal.strategy_family_id,
                signal.instrument.canonical_symbol,
                signal.timeframe.value,
                signal.decision_timestamp.isoformat(),
            )
            grouped.setdefault(key, []).append(signal)
        result: list[_MutableGroup] = []
        for key in sorted(grouped):
            values = grouped[key]
            # Same strategy evidence is one contribution.  Repeated identical
            # evidence is therefore incapable of buying more budget.
            long_values = {item.requested_notional for item in values if item.signed_request > 0}
            short_values = {item.requested_notional for item in values if item.signed_request < 0}
            conflict = bool(long_values and short_values)
            if conflict and policy.conflict_policy is AllocationConflictPolicy.REJECT_OPPOSING:
                requested = _ZERO
            else:
                requested = max(long_values, default=_ZERO) - max(short_values, default=_ZERO)
            identity = _GroupedSignal(
                key[0],
                key[1],
                key[2],
                key[3],
                values[0].instrument,
                requested,
                len(values),
                conflict,
            )
            reasons = []
            if len(values) > 1:
                reasons.append("DUPLICATE_STRATEGY_EVIDENCE_COLLAPSED")
            if conflict:
                reasons.append(
                    "CONFLICTING_SIGNALS_REJECTED"
                    if policy.conflict_policy is AllocationConflictPolicy.REJECT_OPPOSING
                    else "CONFLICTING_SIGNALS_NETTED"
                )
            result.append(_MutableGroup(identity, requested, requested, reasons))
        return result

    @staticmethod
    def _group_key(group: _MutableGroup) -> tuple[str, ...]:
        identity = group.identity
        return (
            identity.instrument.canonical_symbol,
            identity.strategy_id,
            identity.strategy_version,
            identity.artifact_hash,
            identity.family_id,
        )

    def _cap_groups(
        self, groups: list[_MutableGroup], policy: AllocationPolicy, *, scope: str
    ) -> None:
        for group in groups:
            budget = policy.strategy_budget(
                group.identity.strategy_id, group.identity.strategy_version
            )
            if abs(group.value) > budget:
                group.value = self._bounded(group.value, budget, policy.allocation_increment)
                group.reason_codes.append(f"{scope.upper()}_BUDGET_REDUCED")

    def _cap_family_groups(self, groups: list[_MutableGroup], policy: AllocationPolicy) -> None:
        grouped: dict[str, list[_MutableGroup]] = defaultdict(list)
        for group in groups:
            grouped[group.identity.family_id].append(group)
        for family_id, members in grouped.items():
            budget = policy.family_budget(family_id)
            self._pro_rata_cap(
                members, budget, policy.allocation_increment, "FAMILY_BUDGET_REDUCED"
            )

    def _apply_portfolio_constraints(
        self,
        groups: list[_MutableGroup],
        exposure: _ExposureTotals,
        policy: AllocationPolicy,
    ) -> None:
        by_instrument: dict[str, list[_MutableGroup]] = defaultdict(list)
        for group in groups:
            by_instrument[group.identity.instrument.canonical_symbol].append(group)
        for symbol, members in by_instrument.items():
            instrument = members[0].identity.instrument
            base = exposure.position_signed.get(symbol, _ZERO) + exposure.reservation_signed.get(
                symbol, _ZERO
            )
            base_gross = exposure.position_gross.get(
                symbol, _ZERO
            ) + exposure.reservation_gross.get(symbol, _ZERO)
            desired = sum((group.value for group in members), _ZERO)
            new_risk = self._new_risk(base, desired)
            capacity = policy.instrument_budget(instrument) - base_gross
            if new_risk > _ZERO and capacity <= _ZERO:
                self._scale(
                    members,
                    _ZERO,
                    "INSTRUMENT_BUDGET_REDUCED",
                    policy.allocation_increment,
                )
            elif new_risk > max(capacity, _ZERO):
                self._scale(
                    members,
                    max(capacity, _ZERO) / new_risk,
                    "INSTRUMENT_BUDGET_REDUCED",
                    policy.allocation_increment,
                )

        by_class: dict[AssetClass, list[_MutableGroup]] = defaultdict(list)
        for group in groups:
            by_class[group.identity.instrument.asset_class].append(group)
        for asset_class, members in by_class.items():
            base_gross = sum(
                (
                    exposure.position_gross.get(symbol, _ZERO)
                    + exposure.reservation_gross.get(symbol, _ZERO)
                    for symbol, instrument in exposure.instruments.items()
                    if instrument.asset_class is asset_class
                ),
                _ZERO,
            )
            desired_new = self._new_risk_by_instrument(members, exposure)
            capacity = policy.asset_class_budget(asset_class) - base_gross
            if desired_new > _ZERO and desired_new > max(capacity, _ZERO):
                self._scale(
                    members,
                    max(capacity, _ZERO) / desired_new,
                    "ASSET_CLASS_BUDGET_REDUCED",
                    policy.allocation_increment,
                )

        self._cap_global(groups, exposure, policy)

    def _cap_global(
        self,
        groups: list[_MutableGroup],
        exposure: _ExposureTotals,
        policy: AllocationPolicy,
    ) -> None:
        desired_new = self._new_risk_by_instrument(groups, exposure)
        gross_capacity = policy.max_gross_exposure - exposure.gross
        factor = _ONE
        if desired_new > _ZERO and desired_new > max(gross_capacity, _ZERO):
            factor = min(factor, max(gross_capacity, _ZERO) / desired_new)
            for group in groups:
                group.reason_codes.append("GROSS_BUDGET_REDUCED")

        current_net = exposure.net
        desired_delta = sum((group.value for group in groups), _ZERO)
        net_increase = max(abs(current_net + desired_delta) - abs(current_net), _ZERO)
        net_capacity = policy.max_net_exposure - abs(current_net)
        if net_increase > _ZERO and net_increase > max(net_capacity, _ZERO):
            factor = min(factor, max(net_capacity, _ZERO) / net_increase)
            for group in groups:
                group.reason_codes.append("NET_BUDGET_REDUCED")

        if desired_new > _ZERO and desired_new > policy.max_incremental_exposure:
            factor = min(factor, policy.max_incremental_exposure / desired_new)
            for group in groups:
                group.reason_codes.append("INCREMENTAL_BUDGET_REDUCED")
        desired_turnover = sum((abs(group.value) for group in groups), _ZERO)
        if desired_turnover > policy.max_turnover_per_decision:
            factor = min(factor, policy.max_turnover_per_decision / desired_turnover)
            for group in groups:
                group.reason_codes.append("TURNOVER_BUDGET_REDUCED")
        if factor < _ONE:
            self._scale(
                groups,
                factor,
                "PORTFOLIO_BUDGET_REDUCED",
                policy.allocation_increment,
            )

    @staticmethod
    def _exposure(snapshot: PortfolioAllocationSnapshot) -> _ExposureTotals:
        position_signed: dict[str, Decimal] = {}
        reservation_signed: dict[str, Decimal] = defaultdict(lambda: _ZERO)
        position_gross: dict[str, Decimal] = {}
        reservation_gross: dict[str, Decimal] = defaultdict(lambda: _ZERO)
        instruments: dict[str, Instrument] = {}
        for item in snapshot.positions:
            symbol = item.instrument.canonical_symbol
            value = item.to_account_currency(snapshot.account_currency)
            position_signed[symbol] = value
            position_gross[symbol] = abs(value)
            instruments[symbol] = item.instrument
        for item in snapshot.reservations:
            symbol = item.instrument.canonical_symbol
            value = item.to_account_currency(snapshot.account_currency)
            reservation_signed[symbol] += value
            reservation_gross[symbol] += abs(value)
            instruments[symbol] = item.instrument
        return _ExposureTotals(
            position_signed=position_signed,
            reservation_signed=dict(reservation_signed),
            position_gross=position_gross,
            reservation_gross=dict(reservation_gross),
            instruments=instruments,
            net=sum(position_signed.values(), _ZERO) + sum(reservation_signed.values(), _ZERO),
            gross=sum(position_gross.values(), _ZERO) + sum(reservation_gross.values(), _ZERO),
        )

    @staticmethod
    def _new_risk(base: Decimal, delta: Decimal) -> Decimal:
        reduction = min(abs(delta), abs(base)) if delta * base < _ZERO else _ZERO
        return abs(delta) - reduction

    def _new_risk_by_instrument(
        self, groups: list[_MutableGroup], exposure: _ExposureTotals
    ) -> Decimal:
        totals: dict[str, Decimal] = defaultdict(lambda: _ZERO)
        for group in groups:
            totals[group.identity.instrument.canonical_symbol] += group.value
        return sum(
            (
                self._new_risk(
                    exposure.position_signed.get(symbol, _ZERO)
                    + exposure.reservation_signed.get(symbol, _ZERO),
                    delta,
                )
                for symbol, delta in totals.items()
            ),
            _ZERO,
        )

    def _scale(
        self,
        groups: list[_MutableGroup],
        factor: Decimal,
        reason: str,
        increment: Decimal,
    ) -> None:
        bounded = max(_ZERO, min(_ONE, factor))
        for group in groups:
            if bounded < _ONE:
                group.reason_codes.append(reason)
            group.value = self._bounded(group.value * bounded, None, increment)

    @staticmethod
    def _bounded(value: Decimal, limit: Decimal | None, increment: Decimal) -> Decimal:
        bounded = value if limit is None else max(-limit, min(limit, value))
        magnitude = (abs(bounded) / increment).to_integral_value(rounding=ROUND_DOWN) * increment
        return magnitude if bounded >= _ZERO else -magnitude

    def _pro_rata_cap(
        self,
        groups: list[_MutableGroup],
        limit: Decimal,
        increment: Decimal,
        reason: str,
    ) -> None:
        total = sum((abs(group.value) for group in groups), _ZERO)
        if total > limit:
            factor = limit / total
            self._scale(groups, factor, reason, increment)

    def _proposal(
        self,
        *,
        snapshot: PortfolioAllocationSnapshot,
        decision_timestamp: datetime,
        policy: AllocationPolicy,
        status: AllocationStatus,
        targets: tuple[AllocationTarget, ...],
        contributions: tuple[AllocationContribution, ...],
        constraints: tuple[str, ...],
        reasons: tuple[str, ...],
        max_new: Decimal,
        max_reduction: Decimal,
    ) -> AllocationProposal:
        decision_id = deterministic_allocation_decision_id(
            account_id=snapshot.account_id,
            snapshot_revision=snapshot.revision,
            decision_timestamp=decision_timestamp,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            configuration_id=policy.configuration_id,
            targets=targets,
            contributions=contributions,
            constraints=_CONSTRAINTS,
            reasons=reasons,
        )
        return AllocationProposal(
            decision_id=decision_id,
            status=status,
            account_id=snapshot.account_id,
            snapshot_revision=snapshot.revision,
            decision_timestamp=decision_timestamp,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            configuration_id=policy.configuration_id,
            targets=targets,
            contributions=contributions,
            constraints_applied=constraints,
            constraint_reasons=reasons,
            max_new_notional=max_new,
            max_reduction_notional=max_reduction,
        )

    def _blocked(
        self,
        snapshot: PortfolioAllocationSnapshot,
        decision_timestamp: datetime,
        reason: str,
        *,
        policy: AllocationPolicy | None = None,
        policy_id: str | None = None,
        policy_version: str | None = None,
        configuration_id: str | None = None,
    ) -> AllocationProposal:
        if policy is None:
            assert (
                policy_id is not None
                and policy_version is not None
                and configuration_id is not None
            )
            policy_id_value, policy_version_value, configuration_id_value = (
                policy_id,
                policy_version,
                configuration_id,
            )
            # A blocked no-policy result has no targets and therefore needs no
            # policy object to manufacture a risk-bearing fallback.
            targets: tuple[AllocationTarget, ...] = ()
            contributions: tuple[AllocationContribution, ...] = ()
            decision_id = f"allocation-blocked-{snapshot.revision}-{reason.lower()}"
            return AllocationProposal(
                decision_id=decision_id,
                status=AllocationStatus.BLOCKED,
                account_id=snapshot.account_id,
                snapshot_revision=snapshot.revision,
                decision_timestamp=decision_timestamp,
                policy_id=policy_id_value,
                policy_version=policy_version_value,
                configuration_id=configuration_id_value,
                targets=targets,
                contributions=contributions,
                constraints_applied=(),
                constraint_reasons=(reason,),
                max_new_notional=_ZERO,
                max_reduction_notional=_ZERO,
            )
        return self._proposal(
            snapshot=snapshot,
            decision_timestamp=decision_timestamp,
            policy=policy,
            status=AllocationStatus.BLOCKED,
            targets=(),
            contributions=(),
            constraints=_CONSTRAINTS,
            reasons=(reason,),
            max_new=_ZERO,
            max_reduction=_ZERO,
        )


__all__ = ["DeterministicPortfolioAllocator"]
