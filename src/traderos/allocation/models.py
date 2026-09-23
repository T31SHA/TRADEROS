"""Immutable contracts for deterministic, fail-closed portfolio allocation.

The allocator deliberately consumes explicit notional requests.  A direction or
score alone is not a sizing instruction and is never converted into one here.
All exposure amounts are either already in the account currency or carry an
explicit conversion rate and identity; missing conversion inputs are rejected.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from traderos.data.instruments import AssetClass, Instrument
from traderos.data.time import require_utc
from traderos.data.timeframes import Timeframe
from traderos.strategies.models import SignalDirection

_ZERO = Decimal("0")


def _text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-blank")


def _money(value: Decimal, field: str, *, positive: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field} must be a finite Decimal")
    if positive and value <= _ZERO:
        raise ValueError(f"{field} must be positive")
    if not positive and value < _ZERO:
        raise ValueError(f"{field} must be non-negative")


def _finite_signed(value: Decimal, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field} must be a finite Decimal")


class SignalSemantics(StrEnum):
    """What the strategy evidence means to the allocation boundary."""

    DIRECTIONAL_NOTIONAL_REQUEST = "directional_notional_request"


class AllocationConflictPolicy(StrEnum):
    """Explicit treatment of opposing contributions."""

    NET_OPPOSING = "net_opposing"
    REJECT_OPPOSING = "reject_opposing"


class AllocationStatus(StrEnum):
    APPROVED = "approved"
    REDUCED = "reduced"
    REJECTED = "rejected"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class BudgetLimit:
    """One positive budget in account-currency notional."""

    key: str
    max_notional: Decimal

    def __post_init__(self) -> None:
        _text(self.key, "budget key")
        _money(self.max_notional, "budget max_notional", positive=True)


@dataclass(frozen=True)
class AssetClassBudget:
    """One authoritative asset-class gross exposure budget."""

    asset_class: AssetClass
    max_gross_exposure: Decimal

    def __post_init__(self) -> None:
        _money(self.max_gross_exposure, "asset-class max_gross_exposure", positive=True)


@dataclass(frozen=True)
class AllocationPolicy:
    """Versioned allocation authority with no implicit risk-bearing defaults."""

    policy_id: str
    policy_version: str
    max_gross_exposure: Decimal
    max_net_exposure: Decimal
    max_incremental_exposure: Decimal
    max_turnover_per_decision: Decimal
    instrument_limits: tuple[BudgetLimit, ...]
    strategy_budgets: tuple[BudgetLimit, ...]
    family_budgets: tuple[BudgetLimit, ...]
    asset_class_limits: tuple[AssetClassBudget, ...]
    conflict_policy: AllocationConflictPolicy
    allocation_increment: Decimal

    def __post_init__(self) -> None:
        _text(self.policy_id, "allocation policy id")
        _text(self.policy_version, "allocation policy version")
        for field in (
            "max_gross_exposure",
            "max_net_exposure",
            "max_incremental_exposure",
            "max_turnover_per_decision",
        ):
            _money(getattr(self, field), field, positive=True)
        _money(self.allocation_increment, "allocation_increment", positive=True)
        if not isinstance(self.conflict_policy, AllocationConflictPolicy):
            raise ValueError("allocation conflict policy is invalid")
        self._unique(self.instrument_limits, "instrument")
        self._unique(self.strategy_budgets, "strategy")
        self._unique(self.family_budgets, "family")
        classes = [item.asset_class for item in self.asset_class_limits]
        if len(classes) != len(set(classes)):
            raise ValueError("asset-class budgets must be unique")

    @staticmethod
    def _unique(values: tuple[BudgetLimit, ...], scope: str) -> None:
        keys = [item.key for item in values]
        if len(keys) != len(set(keys)):
            raise ValueError(f"{scope} budgets must be unique")

    @property
    def configuration_id(self) -> str:
        payload = {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "max_gross_exposure": str(self.max_gross_exposure),
            "max_net_exposure": str(self.max_net_exposure),
            "max_incremental_exposure": str(self.max_incremental_exposure),
            "max_turnover_per_decision": str(self.max_turnover_per_decision),
            "instrument_limits": [
                (item.key, str(item.max_notional)) for item in self.instrument_limits
            ],
            "strategy_budgets": [
                (item.key, str(item.max_notional)) for item in self.strategy_budgets
            ],
            "family_budgets": [
                (item.key, str(item.max_notional)) for item in self.family_budgets
            ],
            "asset_class_limits": [
                (item.asset_class.value, str(item.max_gross_exposure))
                for item in self.asset_class_limits
            ],
            "conflict_policy": self.conflict_policy.value,
            "allocation_increment": str(self.allocation_increment),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return f"{self.policy_id}-{hashlib.sha256(encoded).hexdigest()[:16]}"

    def instrument_budget(self, instrument: Instrument) -> Decimal:
        return next(
            (
                item.max_notional
                for item in self.instrument_limits
                if item.key == instrument.canonical_symbol
            ),
            self.max_gross_exposure,
        )

    def strategy_budget(self, strategy_id: str, strategy_version: str) -> Decimal:
        key = f"{strategy_id}@{strategy_version}"
        return next(
            (item.max_notional for item in self.strategy_budgets if item.key == key),
            self.max_incremental_exposure,
        )

    def family_budget(self, family_id: str) -> Decimal:
        return next(
            (item.max_notional for item in self.family_budgets if item.key == family_id),
            self.max_incremental_exposure,
        )

    def asset_class_budget(self, asset_class: AssetClass) -> Decimal:
        return next(
            (
                item.max_gross_exposure
                for item in self.asset_class_limits
                if item.asset_class is asset_class
            ),
            self.max_gross_exposure,
        )


@dataclass(frozen=True)
class StrategyAllocationSignal:
    """Verified strategy evidence plus an explicit notional request."""

    source_signal_id: str
    strategy_id: str
    strategy_version: str
    artifact_hash: str
    strategy_family_id: str
    instrument: Instrument
    timeframe: Timeframe
    event_timestamp: datetime
    availability_timestamp: datetime
    decision_timestamp: datetime
    direction: SignalDirection
    semantics: SignalSemantics
    reason: str
    configuration_identity: str
    requested_notional: Decimal
    horizon: timedelta | None = None
    target_preference: Decimal | None = None

    def __post_init__(self) -> None:
        for value, field in (
            (self.source_signal_id, "source_signal_id"),
            (self.strategy_id, "strategy_id"),
            (self.strategy_version, "strategy_version"),
            (self.artifact_hash, "artifact_hash"),
            (self.strategy_family_id, "strategy_family_id"),
            (self.reason, "reason"),
            (self.configuration_identity, "configuration_identity"),
        ):
            _text(value, field)
        for timestamp in (
            self.event_timestamp,
            self.availability_timestamp,
            self.decision_timestamp,
        ):
            require_utc(timestamp)
        if self.event_timestamp > self.decision_timestamp:
            raise ValueError("signal event cannot be after its decision")
        if self.availability_timestamp > self.decision_timestamp:
            raise ValueError("signal availability cannot be after its decision")
        _money(self.requested_notional, "requested_notional", positive=True)
        if self.target_preference is not None:
            _finite_signed(self.target_preference, "target_preference")
        if self.horizon is not None and self.horizon <= timedelta(0):
            raise ValueError("signal horizon must be positive when defined")
        if self.direction not in {SignalDirection.LONG, SignalDirection.SHORT}:
            raise ValueError("allocation signals must be directional")
        if self.semantics is not SignalSemantics.DIRECTIONAL_NOTIONAL_REQUEST:
            raise ValueError("unsupported signal semantics")

    @property
    def identity(self) -> str:
        return (
            f"{self.strategy_id}@{self.strategy_version}/"
            f"{self.artifact_hash}/{self.instrument.canonical_symbol}/"
            f"{self.timeframe.value}/{self.decision_timestamp.isoformat()}"
        )

    @property
    def signed_request(self) -> Decimal:
        return (
            self.requested_notional
            if self.direction is SignalDirection.LONG
            else -self.requested_notional
        )


@dataclass(frozen=True)
class ExposureSnapshot:
    """One position or outstanding reservation, separate from attribution."""

    instrument: Instrument
    signed_notional: Decimal
    currency: str
    conversion_rate_to_account: Decimal | None = None
    conversion_identity: str | None = None
    reservation_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.currency, "exposure currency")
        _finite_signed(self.signed_notional, "signed_notional")
        if self.conversion_rate_to_account is not None:
            _money(self.conversion_rate_to_account, "conversion_rate_to_account", positive=True)
            _text(self.conversion_identity or "", "conversion_identity")
        elif self.currency != "":
            # Same-currency values need no rate; cross-currency values are
            # rejected by ``to_account_currency`` rather than guessed.
            pass
        if self.reservation_id is not None:
            _text(self.reservation_id, "reservation_id")

    def to_account_currency(self, account_currency: str) -> Decimal:
        if self.currency == account_currency:
            if self.conversion_rate_to_account not in (None, Decimal("1")):
                raise ValueError("same-currency exposure has an invalid conversion rate")
            return self.signed_notional
        if self.conversion_rate_to_account is None or self.conversion_identity is None:
            raise ValueError("cross-currency exposure lacks an explicit conversion input")
        return self.signed_notional * self.conversion_rate_to_account


@dataclass(frozen=True)
class PortfolioAllocationSnapshot:
    """Coherent account state consumed by the pure allocator."""

    account_id: str
    account_currency: str
    timestamp: datetime
    revision: int
    positions: tuple[ExposureSnapshot, ...]
    reservations: tuple[ExposureSnapshot, ...]

    def __post_init__(self) -> None:
        _text(self.account_id, "account_id")
        _text(self.account_currency, "account_currency")
        require_utc(self.timestamp)
        if self.revision < 0:
            raise ValueError("portfolio revision must be non-negative")
        position_ids = [item.instrument.canonical_symbol for item in self.positions]
        if len(position_ids) != len(set(position_ids)):
            raise ValueError("portfolio positions must be unique by instrument")
        reservation_ids = [item.reservation_id for item in self.reservations]
        if any(item is None for item in reservation_ids) or len(reservation_ids) != len(
            set(reservation_ids)
        ):
            raise ValueError("portfolio reservations require unique identities")


@dataclass(frozen=True)
class AllocationContribution:
    """Attribution-level result for one strategy identity."""

    strategy_id: str
    strategy_version: str
    artifact_hash: str
    strategy_family_id: str
    instrument: Instrument
    requested_delta: Decimal
    proposed_delta: Decimal
    reduction_notional: Decimal
    new_risk_notional: Decimal
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AllocationTarget:
    """Account-level target, including pending exposure consumption."""

    instrument: Instrument
    existing_signed_exposure: Decimal
    reserved_signed_exposure: Decimal
    existing_gross_exposure: Decimal
    reserved_gross_exposure: Decimal
    proposed_target_exposure: Decimal
    required_delta: Decimal
    new_risk_notional: Decimal
    reduction_notional: Decimal
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AllocationProposal:
    """Stable, non-authorizing allocation result."""

    decision_id: str
    status: AllocationStatus
    account_id: str
    snapshot_revision: int
    decision_timestamp: datetime
    policy_id: str
    policy_version: str
    configuration_id: str
    targets: tuple[AllocationTarget, ...]
    contributions: tuple[AllocationContribution, ...]
    constraints_applied: tuple[str, ...]
    constraint_reasons: tuple[str, ...]
    max_new_notional: Decimal
    max_reduction_notional: Decimal

    def __post_init__(self) -> None:
        _text(self.decision_id, "allocation decision_id")
        _text(self.account_id, "allocation account_id")
        require_utc(self.decision_timestamp)
        if self.snapshot_revision < 0:
            raise ValueError("allocation snapshot revision must be non-negative")
        _money(self.max_new_notional, "max_new_notional")
        _money(self.max_reduction_notional, "max_reduction_notional")

    @property
    def actionable(self) -> bool:
        return self.status in {AllocationStatus.APPROVED, AllocationStatus.REDUCED} and bool(
            self.targets
        )

    def validate_snapshot(self, *, account_id: str, revision: int) -> bool:
        return self.account_id == account_id and self.snapshot_revision == revision


def deterministic_allocation_decision_id(
    *,
    account_id: str,
    snapshot_revision: int,
    decision_timestamp: datetime,
    policy_id: str,
    policy_version: str,
    configuration_id: str,
    targets: tuple[AllocationTarget, ...],
    contributions: tuple[AllocationContribution, ...],
    constraints: tuple[str, ...],
    reasons: tuple[str, ...],
) -> str:
    payload = {
        "account_id": account_id,
        "snapshot_revision": snapshot_revision,
        "decision_timestamp": require_utc(decision_timestamp).isoformat(),
        "policy_id": policy_id,
        "policy_version": policy_version,
        "configuration_id": configuration_id,
        "constraints": constraints,
        "targets": [
            (
                item.instrument.canonical_symbol,
                str(item.existing_signed_exposure),
                str(item.reserved_signed_exposure),
                str(item.proposed_target_exposure),
                str(item.required_delta),
                str(item.new_risk_notional),
                str(item.reduction_notional),
                item.reason_codes,
            )
            for item in targets
        ],
        "contributions": [
            (
                item.strategy_id,
                item.strategy_version,
                item.artifact_hash,
                item.strategy_family_id,
                item.instrument.canonical_symbol,
                str(item.requested_delta),
                str(item.proposed_delta),
                str(item.reduction_notional),
                str(item.new_risk_notional),
                item.reason_codes,
            )
            for item in contributions
        ],
        "reasons": reasons,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"{policy_id}-{hashlib.sha256(encoded).hexdigest()[:16]}"


__all__ = [
    "AllocationConflictPolicy",
    "AllocationContribution",
    "AllocationPolicy",
    "AllocationProposal",
    "AllocationStatus",
    "AllocationTarget",
    "AssetClassBudget",
    "BudgetLimit",
    "ExposureSnapshot",
    "PortfolioAllocationSnapshot",
    "SignalSemantics",
    "StrategyAllocationSignal",
    "deterministic_allocation_decision_id",
]
