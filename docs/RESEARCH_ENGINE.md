# Phase 9 Research & Validation Engine

Phase 9 evaluates predeclared candidates. It is not a strategy generator,
optimizer, accounting engine, broker adapter, or live-promotion system. A
failed candidate is a valid result.

## Lifecycle

```text
immutable Phase 1 bars
  -> DatasetManifest -> DatasetQualification (fail closed)
  -> ResearchHypothesis -> StrategyCandidate / research family
  -> chronological train / validation / test folds
  -> frozen configuration -> locked OOS ExperimentSpec
  -> Phase 3 BacktestEngine result
  -> cost scenarios / robustness / Monte Carlo / statistics
  -> immutable JSON evidence package -> paper-only promotion recommendation
```

`ResearchValidationEngine` exposes `create_hypothesis`, `qualify_dataset`,
`create_candidate`, `create_experiment`, `lock_oos`, `run_backtest`,
`run_walk_forward`, `run_monte_carlo`, `run_statistical_validation`,
`build_evidence_package`, and `evaluate_promotion`. There is deliberately no
`optimize`, `discover`, `deploy`, or live-promotion operation.

## Existing boundary reuse

Research calls `research.execute_backtest`, which validates frozen
manifest/scope/configuration and calls the authoritative Phase 3
`BacktestEngine`. Fill rules, next-bar timing, costs, ledger, portfolio
accounting, and metrics are never reimplemented in Phase 9.

Candidate provenance records Phase 4 strategy version, Phase 5 regime
configuration, Phase 6 fusion configuration, and Phase 7 risk configuration.
Those components are executed by canonical historical replay rather than merely
recorded as identities. Phase 8 sizing and state are neither duplicated nor
mutated by research.

## Canonical historical replay

`CanonicalHistoricalReplay` is the Phase 3 strategy-protocol adapter used for
historical research. At each Phase 3 completed-bar decision event it executes:

```text
Phase 2 FeatureObservation → Phase 5 RegimeEngine → Phase 4 Strategy
→ Phase 6 SignalFusionEngine → Phase 7 RiskFirewall
→ Phase 8 quantity_for_authorization → Phase 3 Order → Phase 3 execution
```

It translates the read-only Phase 3 `StrategyContext` portfolio snapshot into
the existing Phase 7 snapshot contract and derives a temporary Phase 8 sizing
view solely for the pure `quantity_for_authorization` function. It neither
contains a fill simulator nor calculates cash, exposure, P&L, fees, or equity.
The Phase 3 engine continues to own all of those facts.

Every decision records an audit event, including fusion, risk, and sizing
rejections. A non-directional fused intent never reaches risk or Phase 3; a
rejected risk decision never creates a Phase 3 order. The adapter uses exact
Phase 3 completion/next-open boundaries without same-bar OHLC execution.
Deterministic integration coverage verifies stale-regime rejection,
cross-instrument and timeframe isolation, future mutation/append invariance,
fusion and risk vetoes, sizing bounds, and next-bar execution. This is
architectural verification only, not empirical strategy validation.

## Data, temporal, and OOS integrity

`DatasetManifest` hashes canonical bar content, source, timeframe, adjustment
policy, and coverage. `qualify_dataset` binds supplied bars to that hash,
checks duplicates, monotonicity, quote coverage, and delegates calendar checks
to Phase 1 where an asset-specific calendar is supplied. It never repairs or
fills data. Equity qualification can require historical membership, delistings,
and corporate-action lineage. UTC and raw/adjusted separation remain mandatory.

`ChronologicalValidationProtocol` emits only train/validation/test ranges.
Purge and embargo require a documented dependency reason. No shuffle is
available. OOS is a new immutable identity, never selection eligible, and must
reference a frozen development result. `lock_oos` rejects changes to dataset,
strategy/version, parameters, features, regime/fusion/risk configurations,
costs, code version, or candidate.

## Statistics, robustness, and evidence

Seeded moving-block bootstrap and trade-path Monte Carlo retain contiguous
blocks where dependence matters. They describe the observed sample; they are
not forecasts or proofs of edge. Insufficient samples remain unavailable.

Both Benjamini–Hochberg (only under its applicable assumptions) and
Benjamini–Yekutieli (arbitrary dependence) FDR adjustment are available.
Family and candidate IDs make all variants auditable. Cost scenarios produce
immutable scaled copies of Phase 3 cost configuration. Phase 3 does not yet
persist separate gross/spread/slippage attribution, which evidence labels as a
limitation rather than fabricating it.

`EvidencePackage` exports canonical JSON only and rejects unsupported objects,
including callables. It contains identities, configuration, folds, performance,
costs, robustness, statistics, warnings, promotion decision, and lineage.

`PromotionPolicy` is versioned and justified. It consumes qualification,
pristine OOS, risk compatibility, walk-forward, cost-adjusted OOS, robustness,
parameter/regime stability, multiple-testing evidence, sample count, and
severe warnings. The positive recommendation is `PROMOTION_REVIEW`; it does
not authorize a paper order. `PROMOTE_TO_LIVE` does not exist.

## Current limitations

There is no production historical dataset or calibrated cost schedule in this
repository, so Phase 9 makes no empirical profitability or deployability claim.
A production-quality immutable historical dataset is still unavailable, so
architectural validation must not be confused with empirical strategy
validation. Capacity, impact, historical
universe, corporate-action, and calendar data sources are also not added here.
Missing evidence stays `UNKNOWN`, `HOLD`, or `REJECT`.

## Governed Forex dataset admission

Before any Phase 9 experiment, a local Dukascopy tick artifact must pass:

```text
Dukascopy ticks → Preserve immutable raw bytes → Aggregate M15
→ Quality audit → Qualify
```

This has no automatic research/backtest operation and supports bounded target
intervals only. The audit gates source and coverage lineage, exact tick schema,
UTC normalization, strict ordering, OHLC/prices, bid/ask integrity and spread,
bid/ask volume, calendar-aware missing M15 intervals, stale sequences, and
suspicious jumps. It blocks duplicate or non-monotonic ticks, invalid prices,
crossed quotes, invalid volumes, exact OHLC violations, active-session gaps,
unknown gaps, and unexplained active-session zero-volume bars. No repairs,
sorting, fill, interpolation, or manufactured quotes are permitted. The
existing aggregated v1/v2 artifacts remain immutable but are not canonical.

At this commit no actual EUR/USD tick artifact is supplied, so the empirical
qualification state remains `BLOCKED — artifact not supplied`. The local test
fixtures exercise the tick gate only and are not empirical datasets.

### Twelve Data OHLCV research source

Twelve Data is admitted through a separate offline path and is never mixed with
Dukascopy data:

```text
external Twelve Data download → immutable raw CSV
→ Twelve Data parser → provider-neutral OHLCV admission → manifest
```

The bounded target is EUR/USD, 15min, UTC, for the first one-month window
2024-01-02 through 2024-02-02. The parser requires explicit
`TimestampFormat=ISO_8601`, `TimestampSemantics=BAR_START`, and timezone UTC;
naive or non-UTC timestamps are rejected. It maps `datetime` to `timestamp`,
OHLC to canonical OHLC, and `volume` to source volume when present. It does not
manufacture bid/ask, spread, or missing volume and does not apply the
Dukascopy-specific quantization repair.

`RawArtifactStore` records source, source symbol, requested range/timeframe,
timezone, filename, download timestamp, SHA-256, and byte size for every
bounded response. Every raw hash is bound into the immutable manifest.
`TwelveDataForexCalendar` is an explicit versioned calendar identity rather
than a silent reuse of the Dukascopy identity; the acquired artifact must be
reviewed against its actual published session semantics. Missing weekend bars
are expected only where that calendar classifies them as closures. Active-
session gaps and all other admission blockers remain blocking.

The admission report includes row count, coverage, duplicates, ordering,
invalid/nonpositive/nonfinite OHLC, missing intervals, expected closures,
unexpected/unknown gaps, stale sequences, suspicious jumps, schema drift,
overlap conflicts, volume anomalies/unavailability, dataset ID, quality hash,
qualification state, and blockers. The local runner is
`scripts/admit_real_twelve_data.py`; it has no network or credential path.

This source is for strategy research OHLCV, not execution microstructure.
Dukascopy remains the source for bid/ask, spread, execution realism, and
microstructure validation. Any later empirical backtest must record a separate
explicit transaction-cost scenario with `cost_model_id`, `cost_model_version`,
and assumptions. The optional close comparison is data-quality evidence only;
it must not be used to choose the source by strategy performance.
