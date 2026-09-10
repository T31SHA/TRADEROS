# Risk Firewall

Phase 7 is TRADEROS's deterministic pre-trade safety boundary. It evaluates an
already-fused `UnifiedTradeIntent`; it does not generate signals, select a
strategy, predict returns, size a position, create an order, call a broker, or
mutate portfolio/account state.

```text
UnifiedTradeIntent + RegimeState + current quote + immutable risk snapshot
                                  ↓
                            RiskFirewall v1
                                  ↓
               RiskDecision (REJECT or bounded APPROVE)
                                  ↓
              future portfolio / sizing → existing execution boundary
```

The authority hierarchy is deliberately one-way:

```text
Strategy → Signal Fusion → Risk Firewall → Portfolio / Sizing → Execution
```

The firewall can veto. Neither an upstream proposal nor a downstream component
may override a rejection. An approval is not an order and is not an
authorization to bypass future sizing or execution controls.

## Inputs and temporal contract

`RiskEvaluationContext` is immutable and contains exactly one Phase 6 intent,
the aligned Phase 5 state, a `MarketRiskSnapshot`, a `PortfolioRiskSnapshot`,
and a `SystemHealthSnapshot`. All timestamps must be UTC. The intent and regime
must have the exact decision timestamp. Quotes, portfolio state, and health may
be earlier only within explicit configured freshness bounds; future-dated and
stale inputs reject. There is no wall-clock access, cache, network call,
forward-fill, or hidden historical state.

The portfolio snapshot is deliberately narrow, and is not a second portfolio
engine. It contains account-currency values, signed account-currency position
notionals, UTC `risk_day`, daily P&L, high-water mark, active persisted locks,
and reserved intent IDs. `daily_pnl` is supplied as realized plus unrealized
P&L and applicable costs since the beginning of `risk_day` at 00:00 UTC. The
snapshot's `risk_day` must equal the UTC decision date; market-session-specific
reset rules are not invented here.

## Fail-closed controls

`risk_firewall` version `1` evaluates stable, ordered audit checks for:

- emergency kill switch;
- actionable Phase 6 direction and intent-time alignment;
- healthy, fresh system-health assertion;
- Phase 1 data status, quote freshness, positive finite bid/ask, crossed
  markets, and percentage spread;
- aligned active Phase 5 session/data state, unavailable regime dimensions, and
  stressed or unknown liquidity;
- immutable portfolio snapshot freshness and finite/valid account values;
- duplicate reserved intent, externally persisted risk locks, cash/margin,
  gross/net/long/short/instrument/asset-class exposure, leverage, concurrent
  positions, and pending intents;
- UTC daily-loss and absolute high-water-mark drawdown limits.

Any failed or unevaluable mandatory check rejects. Reason codes are typed and
stable (`KILL_SWITCH_ACTIVE`, `DATA_STALE`, `PRICE_INVALID`,
`MAX_LEVERAGE_EXCEEDED`, `DRAWDOWN_LIMIT`, and so on). Every decision retains
all check outcomes, observed/limit strings, input identities, policy/version,
configuration identity, and a deterministic decision ID.

## Configuration and authorization

`RiskFirewallParameters` is frozen, strict Pydantic configuration. It uses
explicit `Decimal` account-currency limits and positive bounded durations; no
environment variable is read during evaluation. Its SHA-256 configuration ID
includes every decision-relevant limit and switch. Limits are configured safety
parameters, never fitted to P&L, returns, Sharpe, or future observations.

For a new/increasing directional intent, an approval contains a bounded
`RiskAuthorization`: maximum new notional, maximum loss at stop, and remaining
portfolio capacity. It never contains an exact quantity. For an opposite
direction against an existing instrument position, the result can only be a
`REDUCTION_ONLY` authorization: maximum new notional is zero and maximum
reduction is the existing exposure. This prevents a quantity-free signal from
being silently interpreted as a reversal.

The future sizing/portfolio component must consume these constraints before it
can produce a quantity-bearing Phase 3 order. This is an architectural contract
rather than a direct adapter in Phase 7, preserving Phase 3 execution semantics.

## Locks, scope, and limitations

Drawdown locks are explicit `active_locks` supplied in each immutable snapshot.
The firewall has no mutable hidden lock; a state owner must persist a lock after
a drawdown breach and must provide an explicit governed unlock process. This
prevents a pure evaluator from silently resuming risk merely because a later
mark improves. The kill switch rejects all directional new intents, including
otherwise valid reductions.

Phase 7 does not implement exact sizing, stop calculation, broker-specific
margin, currency conversion, sector/correlation modelling, strategy-level
concentration, per-instrument position counts beyond the one-position snapshot
model, persistent reservations, paper trading, broker adapters, live trading,
ML, optimization, or adaptive thresholds. Strategy concentration cannot be
correctly attributed from a multi-strategy fused intent; correlation groups are
an explicit future portfolio-risk extension, not fabricated data.

The firewall establishes no claim about profitability. It only answers whether
bounded risk may proceed under explicitly supplied, current, valid evidence.
