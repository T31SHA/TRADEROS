# Paper Trading Engine

Phase 8 implements an offline, stateful paper-trading boundary. It is paper
infrastructure only; it does not establish a profitable strategy or live
readiness.

```text
UnifiedTradeIntent → Phase 7 RiskDecision → deterministic sizing
→ PaperOrder → PaperTradingEngine → PaperFill → durable portfolio projection
```

There is no public direct-order, direct-fill, direct-position, broker, HTTP,
socket, credential, or endpoint path. `PaperExecutionMode` has only `PAPER`.

## Authorization and sizing

`PaperTradingEngine.submit` accepts an approved, immutable Phase 7
`RiskDecision`, a current `PaperQuote`, and an idempotency key. It rejects a
rejected, future, stale, wrong-policy, wrong-instrument, or non-directional
decision. The account policy/configuration identity and the account evaluated
by Phase 7 must equal the paper account. One decision and one `(account,
idempotency key)` can produce only one durable order.

Before consuming a decision, Phase 8 recomputes the canonical Phase 7 decision
integrity ID over all persisted authorization fields. A decision changed after
firewall evaluation (instrument, direction, status, policy/configuration,
account, checks, timestamps, or bounds) fails closed before sizing.

Sizing is deterministic Decimal arithmetic. For new/increased risk it floors
`min(max_new_notional, account risk capacity remaining, long cash) / executable
quote` to the configured quantity increment. For Phase 7 `REDUCTION_ONLY` it
can only close the existing signed position up to the authorized reduction
notional. It never invents a stop model, increases authorization, or mutates a
portfolio. Phase 8 v1 supports same-currency settlement only and models no FX
conversion or broker margin.

## Durable state and transactions

`002_paper_trading.sql`, hardening migration `003_paper_trading_hardening.sql`,
and the aligned SQLAlchemy schema define accounts,
positions, orders, fills, reservations, locks, and append-only audit events.
Account rows are locked for submission/fill transitions. An order, reservation,
and related audit events commit in one transaction. Fills, their accounting
projection, lifecycle update, reservation release, and audit event likewise
commit together. PostgreSQL uses row locks; uniqueness constraints are a second
line of defence against duplicate orders, decisions, reservations, and fills.

Open orders and reservations survive restart. `recover` returns durable state;
`reconcile` fail-loudly verifies fill/order quantity conservation and
reservation totals rather than repairing financial state.

## Lifecycle and execution

```text
CREATED → SUBMITTED → ACCEPTED → PARTIALLY_FILLED → FILLED
                              ↘ CANCEL_REQUESTED → CANCELLED
                              ↘ REJECTED / EXPIRED
```

The engine owns transitions; callers cannot assign arbitrary persisted states.
It supports market, limit, and stop orders with GTC, DAY, and IOC metadata.
Market buys use ask and sells use bid, followed by configured deterministic
adverse slippage and commission. Limits do not fill at a worse price. Stops
trigger at the executable quote and gaps fill at that quote, never the stale
stop level. `max_fill_quantity` creates deterministic partial fills.

A quote must be UTC-aware, current within configuration, and strictly later
than submission. Every order records the last market event processed, so a
replayed or out-of-order quote cannot duplicate a fill. The engine obtains no
market data itself.

## Accounting and locks

All quantities, prices, cash, fees, P&L, exposure, and reservations are
`Decimal`. The position projection follows Phase 3 signed-quantity semantics:
open, increase, reduce, close, and authorized reversals calculate realized P&L
only for the closed slice. Cash changes once per fill; equity is cash plus
marked position value. Negative cash at actual fill is rejected.

Risk reservations prevent concurrent new orders from exceeding an account's
configured risk capacity. New/increased sizing subtracts both active
reservations and durable marked gross position exposure from that capacity;
therefore releasing a filled order's reservation cannot create new risk
capacity while its position remains open. Filled, cancelled, rejected, and
expired orders release their reservation. Daily loss is measured from an
explicit UTC-day starting-equity reference. Drawdown is measured from the
persisted high-water mark. Breaches activate durable daily-loss/drawdown
locks. A persisted lock blocks new/increased risk after restart while explicit
Phase 7 reduction-only authorization remains possible. Manual emergency and
system-health locks can also be durably activated; no public unlock bypass
exists.

## Limitations

This phase deliberately omits FX conversion, margin financing, corporate
actions, intrabar OHLC path simulation, multi-currency accounts, controlled
administrative unlock workflows, APIs, broker connectivity, and all live
execution. It remains offline and deterministic.

## PostgreSQL verification

The PostgreSQL integration suite is intentionally opt-in and skips unless
`TRADEROS_POSTGRES_TEST_URL` names a disposable database that already has
migrations 001, 002, and 003. `scripts/verify_phase8_postgres.sh` applies all
three migrations and runs the suite when given matching caller-supplied libpq and
SQLAlchemy URLs. The suite uses independently spawned processes and database
connections for reservation, idempotency, and fill/cancel races; it does not
substitute threads or SQLite for those assertions.
