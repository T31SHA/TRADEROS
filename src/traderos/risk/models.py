"""Immutable contracts for deterministic, quantity-free risk evaluation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from traderos.data.instruments import Instrument
from traderos.data.time import require_utc
from traderos.regimes.models import RegimeState
from traderos.signals.models import FusionDirection, UnifiedTradeIntent


class RiskDecisionStatus(StrEnum):
    """The firewall has only an approval or veto outcome."""

    APPROVE = "approve"
    REJECT = "reject"


class RiskReasonCode(StrEnum):
    """Stable, machine-readable reasons for a safety decision."""

    KILL_SWITCH_ACTIVE = "kill_switch_active"
    INTENT_NOT_ACTIONABLE = "intent_not_actionable"
    SYSTEM_UNHEALTHY = "system_unhealthy"
    SYSTEM_HEALTH_STALE = "system_health_stale"
    FUTURE_DATED_INPUT = "future_dated_input"
    STALE_RISK_SNAPSHOT = "stale_risk_snapshot"
    DATA_UNAVAILABLE = "data_unavailable"
    DATA_STALE = "data_stale"
    DATA_INVALID = "data_invalid"
    PRICE_INVALID = "price_invalid"
    CROSSED_MARKET = "crossed_market"
    SPREAD_TOO_WIDE = "spread_too_wide"
    REGIME_UNAVAILABLE = "regime_unavailable"
    MARKET_INACTIVE = "market_inactive"
    LIQUIDITY_STRESSED = "liquidity_stressed"
    DUPLICATE_INTENT = "duplicate_intent"
    PORTFOLIO_INVALID = "portfolio_invalid"
    INSUFFICIENT_CAPITAL = "insufficient_capital"
    INSUFFICIENT_MARGIN = "insufficient_margin"
    MAX_LEVERAGE_EXCEEDED = "max_leverage_exceeded"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    RISK_DAY_MISMATCH = "risk_day_mismatch"
    DRAWDOWN_LOCKED = "drawdown_locked"
    RISK_LOCK_ACTIVE = "risk_lock_active"
    DRAWDOWN_LIMIT = "drawdown_limit"
    MAX_GROSS_EXPOSURE_EXCEEDED = "max_gross_exposure_exceeded"
    MAX_NET_EXPOSURE_EXCEEDED = "max_net_exposure_exceeded"
    MAX_LONG_EXPOSURE_EXCEEDED = "max_long_exposure_exceeded"
    MAX_SHORT_EXPOSURE_EXCEEDED = "max_short_exposure_exceeded"
    MAX_INSTRUMENT_EXPOSURE_EXCEEDED = "max_instrument_exposure_exceeded"
    MAX_ASSET_CLASS_EXPOSURE_EXCEEDED = "max_asset_class_exposure_exceeded"
    MAX_POSITIONS_EXCEEDED = "max_positions_exceeded"
    MAX_PENDING_INTENTS_EXCEEDED = "max_pending_intents_exceeded"


class SystemHealthStatus(StrEnum):
    """Broker-independent operational health supplied by an external monitor."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class MarketDataStatus(StrEnum):
    """Explicit quality/freshness result supplied by the data boundary."""

    VALID = "valid"
    STALE = "stale"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


class RiskAction(StrEnum):
    """Safe classification of a quantity-free directional instruction."""

    NEW_OR_INCREASE = "new_or_increase"
    REDUCTION_ONLY = "reduction_only"


class RiskLock(StrEnum):
    """Externally persisted locks that the pure firewall must honor."""

    DRAWDOWN = "drawdown"
    MANUAL = "manual"


@dataclass(frozen=True)
class SystemHealthSnapshot:
    """One explicit, timestamped health assertion; unknown never implies healthy."""

    timestamp: datetime
    status: SystemHealthStatus

    def __post_init__(self) -> None:
        require_utc(self.timestamp)


@dataclass(frozen=True)
class MarketRiskSnapshot:
    """Current quote and Phase 1 quality status used for one risk decision."""

    instrument: Instrument
    timestamp: datetime
    data_status: MarketDataStatus
    bid: Decimal | None
    ask: Decimal | None

    def __post_init__(self) -> None:
        require_utc(self.timestamp)


@dataclass(frozen=True)
class RiskPositionSnapshot:
    """Signed account-currency notional for one currently open instrument."""

    instrument: Instrument
    net_notional: Decimal


@dataclass(frozen=True)
class PortfolioRiskSnapshot:
    """Narrow immutable portfolio state; it is not a second portfolio engine.

    All monetary amounts and position notionals are already expressed in the
    account currency by the caller. ``daily_pnl`` is realized plus unrealized
    P&L and applicable costs since the UTC ``risk_day`` began. Active locks
    are persisted by the owning account/risk-state service, never hidden here.
    """

    timestamp: datetime
    account_id: str
    account_currency: str
    risk_day: date
    equity: Decimal | None
    cash: Decimal | None
    margin_used: Decimal | None
    margin_available: Decimal | None
    daily_pnl: Decimal | None
    high_water_mark: Decimal | None
    positions: tuple[RiskPositionSnapshot, ...] = ()
    active_locks: frozenset[RiskLock] = frozenset()
    reserved_intent_ids: frozenset[str] = frozenset()
    pending_intent_count: int = 0
    position_mark_timestamp: datetime | None = None
    revision: int = 0

    def __post_init__(self) -> None:
        require_utc(self.timestamp)
        if self.position_mark_timestamp is not None:
            require_utc(self.position_mark_timestamp)
            if self.position_mark_timestamp > self.timestamp:
                raise ValueError("position mark cannot be future-dated")
        if not self.account_id.strip() or not self.account_currency.strip():
            raise ValueError("risk snapshot account identity and currency must not be blank")
        if self.pending_intent_count < 0:
            raise ValueError("pending intent count must be non-negative")
        if self.revision < 0:
            raise ValueError("risk snapshot revision must be non-negative")
        symbols = [item.instrument.canonical_symbol for item in self.positions]
        if len(set(symbols)) != len(symbols):
            raise ValueError("risk snapshot positions must be unique by instrument")
        if any(not item.strip() for item in self.reserved_intent_ids):
            raise ValueError("reserved intent identities must not be blank")


@dataclass(frozen=True)
class RiskEvaluationContext:
    """All bounded inputs visible to one deterministic firewall evaluation."""

    decision_timestamp: datetime
    intent: UnifiedTradeIntent
    regime_state: RegimeState | None
    market: MarketRiskSnapshot | None
    portfolio: PortfolioRiskSnapshot | None
    system_health: SystemHealthSnapshot | None

    def __post_init__(self) -> None:
        require_utc(self.decision_timestamp)


@dataclass(frozen=True)
class RiskCheckResult:
    """One deterministic safety check retained in the decision audit trail."""

    check_id: str
    passed: bool
    reason_code: RiskReasonCode | None = None
    observed: str | None = None
    limit: str | None = None

    def __post_init__(self) -> None:
        if not self.check_id.strip():
            raise ValueError("risk check identifier must not be blank")
        if self.passed != (self.reason_code is None):
            raise ValueError("passed checks have no reason and failed checks require one")


@dataclass(frozen=True)
class RiskAuthorization:
    """Binding quantity-free limits for a later sizing component.

    A reduction-only authorization permits no new notional. The future sizing
    component must enforce these bounds before it can create a Phase 3 order.
    """

    action: RiskAction
    max_new_notional: Decimal
    max_loss_at_stop: Decimal
    max_reduction_notional: Decimal
    require_pretrade_sizing: bool = True
    stop_risk_supported: bool = False

    def __post_init__(self) -> None:
        for value in (
            self.max_new_notional,
            self.max_loss_at_stop,
            self.max_reduction_notional,
        ):
            if not value.is_finite() or value < 0:
                raise ValueError("risk authorization limits must be finite and non-negative")
        if self.action is RiskAction.NEW_OR_INCREASE and (
            self.max_new_notional <= 0 or self.max_loss_at_stop <= 0
        ):
            raise ValueError("new-risk authorization requires positive bounded limits")
        if self.action is RiskAction.REDUCTION_ONLY and self.max_new_notional != 0:
            raise ValueError("reduction-only authorization cannot permit new notional")
        if type(self.require_pretrade_sizing) is not bool or type(
            self.stop_risk_supported
        ) is not bool:
            raise ValueError("risk authorization switches must be boolean")


@dataclass(frozen=True)
class RiskDecision:
    """Immutable result of a fail-closed risk decision with compact provenance."""

    decision_id: str
    status: RiskDecisionStatus
    decision_timestamp: datetime
    intent_id: str
    account_id: str | None
    instrument: Instrument
    direction: FusionDirection
    policy_id: str
    policy_version: str
    configuration_id: str
    reason_codes: tuple[RiskReasonCode, ...]
    checks: tuple[RiskCheckResult, ...]
    authorization: RiskAuthorization | None
    regime_state_id: str | None
    portfolio_snapshot_timestamp: datetime | None
    market_snapshot_timestamp: datetime | None
    portfolio_snapshot_revision: int | None = None

    def __post_init__(self) -> None:
        require_utc(self.decision_timestamp)
        if self.portfolio_snapshot_revision is not None and self.portfolio_snapshot_revision < 0:
            raise ValueError("risk decision portfolio revision must be non-negative")
        if not all(
            value.strip()
            for value in (
                self.decision_id,
                self.intent_id,
                self.policy_id,
                self.policy_version,
                self.configuration_id,
            )
        ):
            raise ValueError("risk decision identity fields must not be blank")
        if self.status is RiskDecisionStatus.APPROVE:
            if (
                self.reason_codes
                or self.authorization is None
                or self.account_id is None
                or not self.account_id.strip()
            ):
                raise ValueError("approved risk decisions require bounds and no failure reasons")
        elif (
            self.authorization is not None
            or not self.reason_codes
            or (self.account_id is not None and not self.account_id.strip())
        ):
            raise ValueError("rejected risk decisions require reasons and no authorization")


def deterministic_risk_decision_id(
    *,
    context: RiskEvaluationContext,
    policy_id: str,
    policy_version: str,
    configuration_id: str,
    status: RiskDecisionStatus,
    checks: tuple[RiskCheckResult, ...],
    authorization: RiskAuthorization | None,
) -> str:
    """Create a stable identity from the firewall inputs and resulting checks."""

    reasons = tuple(item.reason_code for item in checks if item.reason_code is not None)
    return _risk_decision_id(
        intent_id=context.intent.intent_id,
        account_id=context.portfolio.account_id if context.portfolio else None,
        instrument=context.intent.instrument,
        direction=context.intent.direction,
        decision_timestamp=context.decision_timestamp,
        policy_id=policy_id,
        policy_version=policy_version,
        configuration_id=configuration_id,
        status=status,
        reason_codes=reasons,
        checks=checks,
        authorization=authorization,
        regime_state_id=context.regime_state.state_id if context.regime_state else None,
        portfolio_snapshot_timestamp=(context.portfolio.timestamp if context.portfolio else None),
        market_snapshot_timestamp=context.market.timestamp if context.market else None,
        portfolio_snapshot_revision=(context.portfolio.revision if context.portfolio else None),
    )


def risk_decision_integrity_id(decision: RiskDecision) -> str:
    """Recompute a decision's tamper-evident deterministic identity.

    Phase 8 uses this before consuming an authorization.  It detects mutation
    through ``dataclasses.replace`` or a deserialization boundary without
    introducing hidden state into the pure Phase 7 evaluator.
    """

    return _risk_decision_id(
        intent_id=decision.intent_id,
        account_id=decision.account_id,
        instrument=decision.instrument,
        direction=decision.direction,
        decision_timestamp=decision.decision_timestamp,
        policy_id=decision.policy_id,
        policy_version=decision.policy_version,
        configuration_id=decision.configuration_id,
        status=decision.status,
        reason_codes=decision.reason_codes,
        checks=decision.checks,
        authorization=decision.authorization,
        regime_state_id=decision.regime_state_id,
        portfolio_snapshot_timestamp=decision.portfolio_snapshot_timestamp,
        market_snapshot_timestamp=decision.market_snapshot_timestamp,
        portfolio_snapshot_revision=decision.portfolio_snapshot_revision,
    )


def _risk_decision_id(
    *,
    intent_id: str,
    account_id: str | None,
    instrument: Instrument,
    direction: FusionDirection,
    decision_timestamp: datetime,
    policy_id: str,
    policy_version: str,
    configuration_id: str,
    status: RiskDecisionStatus,
    reason_codes: tuple[RiskReasonCode, ...],
    checks: tuple[RiskCheckResult, ...],
    authorization: RiskAuthorization | None,
    regime_state_id: str | None,
    portfolio_snapshot_timestamp: datetime | None,
    market_snapshot_timestamp: datetime | None,
    portfolio_snapshot_revision: int | None,
) -> str:
    """Hash every persisted authorization field that Phase 8 consumes."""

    payload = {
        "intent_id": intent_id,
        "account_id": account_id,
        "symbol": instrument.canonical_symbol,
        "asset_class": instrument.asset_class.value,
        "direction": direction.value,
        "decision_timestamp": decision_timestamp.isoformat(),
        "policy_id": policy_id,
        "policy_version": policy_version,
        "configuration_id": configuration_id,
        "status": status.value,
        "reason_codes": [item.value for item in reason_codes],
        "regime_state_id": regime_state_id,
        "portfolio_snapshot_timestamp": (
            portfolio_snapshot_timestamp.isoformat() if portfolio_snapshot_timestamp else None
        ),
        "market_snapshot_timestamp": (
            market_snapshot_timestamp.isoformat() if market_snapshot_timestamp else None
        ),
        "portfolio_snapshot_revision": portfolio_snapshot_revision,
        "checks": [
            (
                item.check_id,
                item.passed,
                item.reason_code.value if item.reason_code else None,
                item.observed,
                item.limit,
            )
            for item in checks
        ],
        "authorization": (
            None
            if authorization is None
            else (
                authorization.action.value,
                str(authorization.max_new_notional),
                str(authorization.max_loss_at_stop),
                str(authorization.max_reduction_notional),
                authorization.require_pretrade_sizing,
                authorization.stop_risk_supported,
            )
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"{policy_id}-{hashlib.sha256(encoded).hexdigest()[:16]}"


__all__ = [
    "MarketDataStatus",
    "MarketRiskSnapshot",
    "PortfolioRiskSnapshot",
    "RiskAction",
    "RiskAuthorization",
    "RiskCheckResult",
    "RiskDecision",
    "RiskDecisionStatus",
    "RiskEvaluationContext",
    "RiskLock",
    "RiskPositionSnapshot",
    "RiskReasonCode",
    "SystemHealthSnapshot",
    "SystemHealthStatus",
    "deterministic_risk_decision_id",
    "risk_decision_integrity_id",
]
