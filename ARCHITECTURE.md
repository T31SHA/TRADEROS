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

## Phase 0 scope

Phase 0 established policy, package boundaries, configuration, logging, and
quality gates. Phase 1 adds the market-data domain, deterministic local provider,
ingestion, quality, lineage, storage adapters, and PostgreSQL migration. Phase 2
adds deterministic feature definitions, indicators, feature lineage, causal
availability metadata, validation, and an in-memory storage boundary. Phase 3
adds offline event-driven backtesting and metrics. Redis, a real external
provider, broker, live execution, API server, dashboard, model training, and
live credentials remain out of scope.
