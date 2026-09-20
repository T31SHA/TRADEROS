# Phase 9 Research Validation

## Executive decision

**TRADEROS PHASE 9 — RESEARCH VALIDATION: INVALID for empirical strategy
evaluation.** The repository contains no versioned, empirically eligible
historical `MarketBar` dataset, no configured research universe, and no
approved immutable date range from which a development/walk-forward/locked-OOS
split can be derived. Local artifacts are inventoried, but their qualification
is rejected. The data inventory is recorded in
[`research/results/phase9_baseline_data_inventory.json`](../research/results/phase9_baseline_data_inventory.json).

This is not evidence that any baseline is unprofitable, and it is not a reason
to tune one. It means that the four hypotheses cannot be evaluated honestly
from this repository state. The Phase 9 framework is implemented and tested;
the empirical result remains invalid until a versioned, quality-reviewed dataset
is supplied.

## Research boundary

```text
immutable DatasetManifest + temporal ResearchSplit
        ↓
development-only candidate research
        ↓
frozen configuration + deterministic walk-forward folds
        ↓
explicit, non-selection-eligible LOCKED_OUT_OF_SAMPLE evaluation
        ↓
immutable ExperimentSpec + result hash + filesystem registry
        ↓
evidence matrix and human research decision
```

`traderos.research` is a research-only package. It neither mutates strategies,
feature/regime/fusion/risk configuration, paper accounts, or portfolio state,
nor has an order, broker, credential, network, or live-trading path. Its
`execute_backtest` adapter validates dataset, temporal scope, strategy version,
and the existing Phase 3 `BacktestConfig` identity before invoking the existing
causal backtester. It does not implement a second execution or accounting
engine.

## Dataset and data governance

| Item | Result |
| --- | --- |
| Versioned datasets available | None |
| Instruments available | None |
| Timeframes available | None |
| Usable date range | None |
| Development period | Not selected; cannot be invented |
| Walk-forward folds | Not run |
| Locked OOS period | Not selected; unavailable |
| Observed market costs | Not available |

`DatasetManifest.from_bars` hashes ordered canonical bar content plus timeframe
and raw/adjusted policy. It records data source, period, symbols, quality state,
historical membership, delisting coverage, and corporate-action verification.
The registry keeps the data hash, strategy/version, parameters, feature,
regime/fusion/risk identifiers, Phase 3 configuration identity, cost scenario,
code identity, random seed, temporal scope, and result hash together.

An equity result with missing historical membership, delistings, or verified
corporate actions is explicitly flagged by the manifest. The current Phase 1
architecture does not provide historical constituent membership, delisting
data, a corporate-action event provider, or a production exchange-holiday
source. Any future equity result is therefore conditional until those facts are
supplied and reviewed. Raw and adjusted bars must remain separate as enforced
by Phases 1–3; adjusted series cannot silently be used as executable prices.

## Temporal validation and locked OOS

All ranges are UTC half-open intervals, `[start, end)`. `ResearchSplit` rejects
overlap between development and locked OOS. `WalkForwardConfig` emits only
monotonic expanding or rolling train/forward folds and rejects a fold that would
enter the locked boundary. No random time-series split or shuffle is provided.

A `LOCKED_OUT_OF_SAMPLE` experiment must reference a frozen development
experiment and cannot be `selection_eligible`. The `ResearchPlan` independently
checks that the experiment lies in its declared split/dataset. If a locked-OOS
result is allowed to influence selection, it is marked contaminated in the
immutable result metadata and must never be described as pristine validation.

Researchers must select instruments, timeframes, parameter grids, feature
versions, regime/fusion settings, and cost assumptions using development data
only. Opening final OOS data for any of those choices contaminates it. A new
hypothesis requires a new development cycle and fresh locked validation data;
poor results must be recorded, not tuned away.

## Cost, execution, and benchmark method

The framework reuses Phase 3 `BacktestConfig`, `BacktestEngine`, and its
`NEXT_BAR_OPEN` market/limit/stop timing, `Decimal` accounting, spread,
commission, and adverse slippage. `CostScenario` creates an immutable scaled
copy of the same cost configuration for base, 1.5x, 2x, or other explicitly
declared stress cases; it never changes the base configuration. Gross and net
economics must be reported separately. Increasing stated non-negative costs
cannot improve a reported net P&L.

No actual market-specific cost observations are in the repository, so no cost
assumption has been selected and no cost/slippage survival claim is made.
When data becomes available, each prespecified candidate must be compared with
cash/no-trade and, for equities, the existing Phase 3 buy-and-hold benchmark.
Forex directional/control comparisons must also be prespecified in the dataset
manifest rather than selected after results are seen.

## Statistical and robustness method

The implementation provides a seeded moving-block bootstrap of means. It
resamples contiguous return/trade blocks instead of pretending financial
observations are IID; it reports `UNAVAILABLE` for inadequate samples. It also
provides a conservative Benjamini–Yekutieli adjusted p-value helper for a
pre-specified family under arbitrary dependence. Neither procedure proves an
edge or replaces locked OOS, transparent candidate counting, or walk-forward
validation.

Outlier analysis reports the contribution of the top one, five, and ten P&L
observations and the result after their removal. The evidence classifier is
multidimensional: it separates data integrity, pristine OOS, sample adequacy,
net expectancy, benchmark value, cost/slippage survival, parameter plateau,
walk-forward consistency, multiple-testing evidence, regime concentration, and
outlier dependence. It intentionally has no magic Sharpe threshold.

With an eligible dataset, reports must additionally calculate the existing
Phase 3 return, CAGR, annualized volatility, drawdown/recovery, Sharpe,
Sortino, Calmar, trade, win/loss, expectancy, profit-factor, turnover, holding
period, and fee metrics. Regime-conditioned, subperiod, strategy correlation,
trade-overlap, drawdown-overlap, fusion, parameter-surface, and path/bootstrap
analyses are observations; none silently becomes a new strategy rule.

## Baseline strategy scorecard

No numeric performance value below is zero; every `UNAVAILABLE` field means no
validated observation exists.

| Strategy | OOS return | CAGR | Sharpe | Sortino | Max drawdown | Expectancy | Profit factor | Trades | Turnover | Cost / slippage | Decision |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Forex Trend Following v1 | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | Not tested | INVALID — no dataset |
| Forex Breakout v1 | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | Not tested | INVALID — no dataset |
| Equity Momentum v1 | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | Not tested | INVALID — no dataset |
| Equity Breakout v1 | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE | Not tested | INVALID — no dataset |

The baseline parameter definitions remain frozen and unmodified:

| Strategy | Existing v1 default parameters | Supported timeframe scope |
| --- | --- | --- |
| Forex Trend Following | fast EMA 10, slow EMA 30, strength threshold 0 | Forex 15m / 1h / 4h / 1d |
| Forex Breakout | lookback 20, buffer 0 | Forex 15m / 1h / 4h / 1d |
| Equity Momentum | lookback 20, minimum momentum 0 | Equity/ETF 1h / 1d |
| Equity Breakout | lookback 20, buffer 0 | Equity/ETF 1h / 1d |

## Regime and fusion findings

**INCONCLUSIVE.** There is no empirically eligible historical dataset on which
to condition the existing Phase 5 trend, volatility, liquidity, data/session states or measure
the Phase 6 fusion policy. No regime rule, filter, weighting, or fusion policy
was changed. It is therefore unknown whether fusion diversifies returns,
duplicates signals, changes drawdown overlap, or improves risk-adjusted
performance.

## Decision matrix

| Dimension | Result | Interpretation |
| --- | --- | --- |
| Net OOS expectancy | UNAVAILABLE | No forward evaluation |
| OOS Sharpe / Sortino | UNAVAILABLE | No return sample |
| Max drawdown | UNAVAILABLE | No equity curve |
| Cost / slippage sensitivity | UNAVAILABLE | No dataset or prespecified cost inputs |
| Parameter robustness | UNAVAILABLE | No development parameter surface |
| Walk-forward consistency | UNAVAILABLE | No folds evaluated |
| Regime stability | UNAVAILABLE | No regime observations |
| Sample adequacy | INADEQUATE | Zero eligible trades/bars |
| Multiple-testing risk | UNQUANTIFIED | No predeclared candidate set evaluated |
| Benchmark advantage | UNAVAILABLE | No benchmarks evaluated |
| Outlier dependence | UNAVAILABLE | No trade P&Ls |
| Survivorship / corporate actions | UNRESOLVED | Current data architecture lacks required equity history |
| Leakage audit | Framework guards tested; no empirical path run | No future-data breach found in Phase 9 code |

## Strategy kill list and survivors

### Strategies killed

None. A missing dataset is a methodology failure, not negative economic
evidence. It cannot justify killing a hypothesis.

### Research-surviving strategies

None. No strategy met a survival standard, and none may be called
research-surviving, profitable, production-ready, or live-ready.

All four current baselines are **INVALID / not evaluable** pending an immutable
dataset and a new full development-to-locked-OOS run.

## Quantitative integrity audit

| Check | Status |
| --- | --- |
| Look-ahead / future feature leakage | No new Phase 9 path; Phase 9 adapter reuses Phase 3 causal engine and guards chronology |
| OOS contamination | Guarded in implementation and tests; no OOS dataset exists to evaluate |
| Random time-series shuffling | Not implemented |
| Calendar and bar-completion correctness | Reuses Phase 1/3 contracts; no empirical calendar audit possible |
| Cost-model correctness | Reuses Phase 3; no observed cost data to calibrate |
| Survivorship bias | Unresolved for equities |
| Corporate-action integrity | Unresolved for equities |
| Determinism / lineage | Manifest, spec, result hash, and registry tests pass |

## Required evidence before reconsideration

Supply an immutable, quality-reviewed Phase 1 dataset manifest for each
candidate universe, including UTC bars, source/version/hash, raw/adjusted
policy, verified calendar coverage, known data gaps, FX quote/spread policy,
and a predeclared cost/slippage schedule. Equity data also needs historical
membership, delistings, and corporate-action treatment. Then predeclare:

1. the development and locked-OOS boundaries from the actual available dates;
2. candidate strategies, parameters, instruments, timeframes, regimes, fusion
   policy, benchmarks, and all cost scenarios;
3. expanding/rolling fold design, block-bootstrap settings, and multiple-test
   family;
4. a fresh locked-OOS evaluation after development decisions are frozen.

Only then may the four baselines receive `PASS`, `CONDITIONAL`, or `KILL`
decisions based on evidence. A Phase 9 survival result would still not authorize
broker integration, live trading, strategy promotion, or capital deployment.

## Security and production boundary

Phase 9 adds no broker, live execution, credentials, API keys, network client,
production deployment, or automatic strategy promotion. It does not connect to
or mutate Phase 8 paper state.
