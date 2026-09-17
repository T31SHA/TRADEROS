# Architecture

## Phase 0 decision

TRADEROS starts as a modular monolith. The repository uses explicit package
boundaries so that a component can later be extracted if its operational needs
justify it, but the initial system avoids the reliability and deployment cost
of unnecessary microservices.

## Dependency direction

```text
apps (API / dashboard / worker)
        ↓
application orchestration
        ↓
domain packages (data, features, regimes, strategies, signals, risk,
                portfolio, execution, research, learning, backtesting)
        ↓
core contracts, configuration, time, errors, observability
        ↓
adapters (database, Redis, providers, brokers, external services)
```

Domain logic must not depend on a specific market-data provider, broker, web
framework, database driver, or LLM. Adapters implement interfaces owned by the
domain/application layer. Strategies produce normalized signals; only the
execution path may submit orders, and only after the risk firewall approves.

## Package boundaries

| Boundary | Responsibility | Explicit non-responsibility |
| --- | --- | --- |
| `data` | provider-neutral contracts, ingestion, normalization, validation, retrieval | strategy decisions |
| `features` | leakage-safe derived observations | order placement |
| `regimes` | interpretable market-state classification | arbitrary model deployment |
| `strategies` | reproducible signal proposals | risk veto or broker calls |
| `signals` | signal schema and deterministic fusion | LLM override |
| `risk` | portfolio and trade constraints; veto authority | strategy optimization |
| `portfolio` | positions, cash, equity, attribution | broker-specific protocols |
| `backtesting` | event-driven simulation and metrics | claiming future profitability |
| `execution` | order lifecycle orchestration | bypassing risk |
| `research` | experiment proposals, validation, review artifacts | live deployment |
| `learning` | attribution and degradation analysis | automatic promotion |
| `models` | versioned strategies, features, datasets, models | untraceable production decisions |
| `database` | persistence adapters and migrations | manual schema drift |
| `monitoring` | logs, metrics, health, freshness, heartbeat | hiding failures |

## Safety boundary

The intended production path is:

```text
market data → features → regime → strategy signals → signal fusion
→ risk firewall → order intent → broker/paper adapter → fills
→ portfolio → attribution/monitoring
```

The risk firewall is a mandatory gate. If risk calculations fail, input data is
stale, or required controls cannot be evaluated, the default action is **do not
trade**. LLM-assisted research can create experiment proposals but cannot
modify risk limits, approve deployment, or submit an order.

## Configuration and environment

`traderos.core.config.Settings` is the single application configuration entry
point. Configuration is externalized through environment variables and `.env`.
Live mode has a fail-closed check requiring both `TRADING_MODE=live` and
`LIVE_TRADING_ENABLED=true`; this is only a configuration precondition, not
permission to trade.

## Data and time

UTC is the canonical internal timestamp convention. Provider adapters own
provider-specific schemas and credentials. Corporate actions, delistings,
market sessions, spreads, rollover, and calendars belong in the data/adapters
layers and must be represented explicitly rather than silently fabricated.

Phase 1 implements this path:

```text
MarketDataProvider → BarCandidate → UTC normalization → MarketBar validation
→ DataQualityEngine → MarketDataStore
```

`DeterministicLocalProvider` is the only implemented provider and requires no
credentials or network. `SqlAlchemyMarketDataStore` targets PostgreSQL and is
tested with SQLite; `InMemoryMarketDataStore` is for deterministic offline
tests. Raw and adjusted data are distinct through `adjustment_policy` and the
database uniqueness key includes that policy.

Phase 2 adds the leakage-safe feature path:

```text
validated MarketBar sequence → FeatureRegistry/FeatureContext
→ causal feature computation → FeatureObservation + lineage
→ FeatureSet validation → FeatureStore boundary
```

Feature code consumes only canonical bars. It uses the Phase 1 bar-start
timestamp convention and records separate observation, availability, and
decision timestamps. Current-bar descriptive features may use the completed
current bar; breakout thresholds use only the prior completed window. Raw and
adjusted bars cannot be mixed in a feature context. Phase 2 intentionally has
no resampling, label generation, strategy, model, execution, or live-trading
dependency.

Phase 3 adds the chronological replay path:

```text
validated bars + causal feature observations
→ event loop → strategy order proposal → order state machine
→ deterministic execution simulator → Decimal portfolio accounting
→ fill ledger + equity curve → performance metrics
```

The backtesting engine owns no provider or broker integration. It consumes
canonical bars and phase-appropriate feature observations, applies an explicit
next-bar execution policy, and keeps market-event time separate from decision
and fill time. It is offline and does not include the future risk firewall,
paper trading, or live execution.

Phase 4 adds the signal-only strategy boundary:

```text
causal FeatureObservation values → immutable StrategyContext
→ versioned StrategySignal/OrderIntent → BacktestStrategyAdapter
→ Phase 3 Order → execution simulator → portfolio/accounting
```

The strategy registry validates explicit identity, supported asset/timeframe
scope, and required feature definitions. Parameterized feature observations
remain distinct by their lineage parameters, which prevents two instances of a
feature such as fast and slow EMA from colliding in a backtest context.
Strategies cannot mutate portfolio state or call a broker. Phase 4 implements
four transparent baseline rules only; fusion, risk controls, optimization, ML,
paper trading, and live execution remain later phases.

Phase 5 adds an observation-only regime boundary:

```text
completed Phase 1 MarketBar + causal Phase 2 FeatureObservation values
→ immutable RegimeContext → versioned stateless detector → RegimeState
→ Phase 6 signal fusion
```

`RegimeContext` uses the Phase 1 calendar and accepts only same-instrument,
same-timeframe features available no later than the UTC decision timestamp. The
baseline detector independently returns trend, volatility, liquidity, and
data/session dimensions rather than a combinatorial trade taxonomy. A state
includes detector/version/configuration identity and compact feature
provenance. It has no strategy, backtesting execution, portfolio, risk,
network, broker, ML, optimization, paper, or live dependency. Phase 5 cannot
change an order, fill, P&L, or strategy decision.

Phase 6 adds the deterministic fusion boundary:

```text
Phase 4 StrategySignal values + Phase 5 RegimeState
→ immutable FusionContext → versioned FusionPolicy → UnifiedTradeIntent
→ future Phase 7 risk/sizing → existing Phase 3 order/execution path
```

Fusion normalizes the existing signal contract, enforces exact decision-time,
instrument, and timeframe alignment, evaluates explicit regime compatibility,
and resolves eligible strategy evidence with a versioned policy. Its unified
intent has no quantity and cannot become a Phase 3 order directly: position
sizing, portfolio constraints, risk veto, and adaptation to execution remain
the Phase 7 boundary. The Phase 6 package has no broker, network, credential,
portfolio, backtesting-execution, ML, optimization, paper, or live dependency.

Phase 7 adds the fail-closed risk boundary:

```text
Phase 6 UnifiedTradeIntent + aligned Phase 5 RegimeState + current risk inputs
→ immutable RiskEvaluationContext → RiskFirewall v1 → RiskDecision
→ future portfolio/sizing → existing Phase 3 order/execution path
```

The firewall has veto authority and is a pure, deterministic evaluator. It
returns either a rejection with stable machine-readable reasons or an approved
quantity-free authorization with maximum notional/loss constraints. It does not
size a position, mutate account state, submit an order, or adapt directly to a
Phase 3 order. Current inputs are explicit UTC snapshots for health, quote/data
quality, portfolio exposure, cash/margin, P&L, high-water mark, locks, and
duplicate reservations. Unknown, stale, future-dated, invalid, or missing
safety inputs reject. Persisting locks/reservations and creating a bounded order
remain future portfolio/sizing responsibilities; no execution semantics change.

Phase 8 adds the offline durable paper path:

```text
approved immutable RiskDecision → Decimal sizing → PaperOrder + reservation
→ later supplied PaperQuote → PaperFill → durable account/position projection
→ durable risk snapshot
```

`traderos.paper.PaperTradingEngine` is the sole quantity-bearing order path.
It accepts neither a `UnifiedTradeIntent` nor caller-created orders/fills, and
consumes an approval only for the account identified by its Phase 7 portfolio
snapshot. Its SQLAlchemy store uses the account row as the transaction lock point and commits
order/reservation/audit/risk-snapshot together, then
fill/portfolio-projection/audit/risk-snapshot together.
The append-only fill/audit ledger supports recovery and reconciliation. It
reuses Phase 3's signed-position, Decimal, spread/slippage/commission concepts
without turning the historical backtester into a mutable service. Market data
is supplied as an explicit UTC quote and cannot be fetched by the broker.

The only implemented execution mode is `PAPER`; no live adapter, endpoint,
credential, or network transport exists. Durable UTC-day loss, drawdown,
emergency, and system-health locks prevent new risk after restart, while a
Phase 7 reduction-only authorization remains bounded. FX conversion, margin,
and live execution are intentionally deferred.

Phase 9 adds a research-only validation boundary:

```text
immutable DatasetManifest + temporal ResearchSplit
→ development/walk-forward ExperimentSpec
→ frozen configuration
→ non-selection-eligible locked OOS ExperimentSpec
→ immutable result hash + filesystem registry → human evidence review
```

`traderos.research` validates lineage before reusing the Phase 3 backtester; it
does not create an alternate execution/accounting path. Locked OOS is never a
selection scope, and contamination is explicit in `ExperimentResult`. Moving
block bootstrap and conservative multiple-testing adjustment helpers support
review but never automatically promote a candidate. Research has no dependency
on paper state, brokers, credentials, networks, or live trading. The current
repository contains no versioned historical dataset, so Phase 9 includes no
empirical performance or survival claim.

Phase 9 also supplies fail-closed data qualification, immutable
hypothesis/candidate/family lineage, chronological train/validation/test folds
with documented purge/embargo, FDR controls, deterministic trade-path stress,
canonical evidence export, and paper-only promotion review. It delegates
execution/accounting to Phase 3 and records rather than duplicates Phase 5–8
provenance. See `docs/RESEARCH_ENGINE.md`.

The historical canonical path is now a thin Phase 3 strategy adapter:
`Phase 2 features → Phase 5 regime → Phase 4 strategies → Phase 6 fusion →
Phase 7 firewall → Phase 8 pure sizing → Phase 3 orders/execution`. It owns no
fill, portfolio, or accounting logic.

## Phase 0 scope

Phase 0 established policy, package boundaries, configuration, logging, and
quality gates. Phase 1 adds the market-data domain, deterministic local provider,
ingestion, quality, lineage, storage adapters, and PostgreSQL migration. Phase 2
adds deterministic feature definitions, indicators, feature lineage, causal
availability metadata, validation, and an in-memory storage boundary. Phase 3
adds offline event-driven backtesting and metrics. Redis, a real external
provider, broker, live execution, API server, dashboard, model training, and
live credentials remain out of scope.
