# Risk Policy

## Purpose

Risk controls protect capital and preserve the validity of research. They are
not optional strategy preferences.

## Non-negotiable rules

1. No signal may reach execution without a risk decision.
2. Risk has veto authority over every strategy and every execution mode.
3. Risk-engine failure, stale market data, invalid sizing, missing limits, or
   uncertain portfolio state means **do not trade**.
4. Strategies and LLMs cannot modify hard limits, disable the kill switch,
   increase leverage, or approve their own deployment.
5. A no-trade decision is valid and preferred when expected edge does not exceed
   estimated costs and risk premium.
6. A daily dollar-profit target is never a position-sizing or trading rule.

## Required control families

The eventual risk firewall must cover position, portfolio, asset, sector,
currency, correlated exposure, leverage, daily loss, strategy drawdown,
portfolio drawdown, volatility, liquidity, spread, sessions, concurrent
positions, and emergency shutdown.

## Implemented Phase 7 firewall

Phase 7 implements `risk_firewall` v1 as a deterministic, immutable,
quantity-free pre-trade gate. It rejects a directional unified intent if the
kill switch is active; health, quote/data, regime, or portfolio snapshot is
missing/stale/future/invalid; liquidity/session state is unsafe; or configured
capital, margin, leverage, exposure, position, pending-intent, daily-loss,
drawdown, or persisted-lock limits are breached. All limits are strict frozen
configuration with a deterministic identity, and every result has ordered check
records and stable reason codes.

An approval is bounded by maximum new notional and maximum loss at stop, not an
order quantity. An opposite intent may receive a reduction-only authorization
with zero new notional; it cannot silently reverse an existing position. The
pure firewall cannot persist locks or intent reservations, so the future
portfolio/risk-state owner must supply those explicit snapshots and honor the
returned bounds before creating an order. Broker-specific margin, correlation
groups, strategy attribution, sizing, paper trading, and live execution remain
deferred.

## Live activation

Live trading is disabled by default. At minimum, both of these configuration
conditions must be satisfied before the live adapter can even be considered:

```text
TRADING_MODE=live
LIVE_TRADING_ENABLED=true
```

They do not replace human approval, validation gates, separate credentials, or
hard live limits. Phase 0 does not implement live execution.

## Incident behavior

The system must stop opening new risk when daily loss, drawdown, stale data,
abnormal spread, broker instability, rejection rate, execution deviation,
heartbeat failure, or risk-calculation failure breaches its configured safety
condition. Incident actions must be observable and auditable.
