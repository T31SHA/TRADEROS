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
Those components remain canonical. The current Phase 3 strategy adapter
predates the Phase 6/7 path, so results must state when they are strategy-level
replays rather than fully fused/risk-gated portfolio replays. Phase 8 sizing
and state are neither duplicated nor mutated by research.

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
A fully fused and risk-gated historical adapter needs an explicit future Phase
3 integration, not a research-only substitute. Capacity, impact, historical
universe, corporate-action, and calendar data sources are also not added here.
Missing evidence stays `UNKNOWN`, `HOLD`, or `REJECT`.
