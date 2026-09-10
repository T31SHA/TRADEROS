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
