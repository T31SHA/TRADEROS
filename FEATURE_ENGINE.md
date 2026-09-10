# Feature Engine

Phase 2 converts validated Phase 1 `MarketBar` observations into deterministic,
versioned feature observations. The package has no provider, strategy, broker,
execution, portfolio, ML, or network dependency.

## Causal timestamp contract

Phase 1 uses bar-start semantics: `MarketBar.timestamp` is the opening instant
of the interval. Phase 2 uses the completed current bar when calculating a
feature. Therefore, for a feature on bar `t`:

```text
observation_timestamp = t
availability_timestamp = t + timeframe.duration + definition.availability_lag
decision_timestamp = availability_timestamp + context.decision_lag
```

The decision timestamp is the earliest safe decision timestamp represented by
this phase. Execution timestamps are deliberately not implemented. A higher
timeframe observation must not be used before that higher timeframe bar is
complete.

Every calculation walks the ordered bar prefix ending at the current row.
Previous-window breakout levels use the window immediately before the current
bar; current high/low values cannot contaminate those levels.

## Architecture

```text
MarketBar sequence
      ↓ validate_bar_series
FeatureRegistry + FeatureRequest + FeatureContext
      ↓ controlled indicator implementation
FeatureObservation + FeatureLineage
      ↓ validate_feature_set
FeatureSet (long form)
      ↓
FeatureStore / future research consumers
```

`FeatureDefinition` is the centralized metadata contract. It contains the
version, scope, required inputs, dependencies, default configuration, lookback,
causal classification, null policy, and implementation version. `FeatureRegistry`
is instance-scoped; duplicate name/version registration is rejected.

Feature values are stored as a long-form observation model. A logical storage
key is:

```text
symbol + timeframe + observation_timestamp + feature_name + feature_version
+ source_dataset_version + adjustment_policy
```

Phase 2 supplies `InMemoryFeatureStore` for reproducible offline research and
tests. A database adapter can implement the same `FeatureStore` protocol later
without changing numerical code. The source dataset version and adjustment
policy are never optional in the feature context or lineage.

## Supported feature library

All features are version 1 and return `float` values. Initial warm-up rows are
represented by `value=None` and `status=WARMUP`; no forward filling is used.
Unavailable optional inputs are `MISSING_INPUT`, and mathematically undefined
values (for example a zero-width range) are `UNDEFINED`.

| Feature | Formula / method | Default lookback |
| --- | --- | ---: |
| `simple_return` | `close_t / close_(t-1) - 1` | 1 |
| `log_return` | `log(close_t / close_(t-1))` | 1 |
| `rolling_return`, `rate_of_change` | `close_t / close_(t-n) - 1` | 20 |
| `sma` | arithmetic mean of the current inclusive window | 20 |
| `ema` | seeded with the first SMA, then standard alpha `2/(n+1)` | 20 |
| `moving_average_distance` | `close / moving_average - 1` | 20 |
| `rolling_volatility` | population standard deviation of simple returns | 20 |
| `realized_volatility` | population standard deviation of log returns | 20 |
| `atr` | SMA of true range; first true range is `high-low` | 14 |
| `rolling_high`, `rolling_low` | inclusive rolling bar high/low | 20 |
| `range_position` | `(close - rolling_low)/(rolling_high - rolling_low)` | 20 |
| `distance_to_previous_high/low` | close distance from a prior completed window | 20 |
| `breakout_state` | `1`, `0`, `-1` versus prior completed high/low window | 20 |
| `rsi` | Wilder RSI with explicit first-period averages | 14 |
| `volatility_percentile` | rank of current rolling volatility within a causal window | 20 |
| `volatility_ratio` | default 10-window volatility / 30-window volatility | 30 |
| `equity_volume` | provider-reported equity/ETF volume | 1 |
| `dollar_volume` | equity/ETF `close * volume` | 1 |

Volatility is not annualized by default. Annualization must be an explicit
future feature parameter with a documented periods-per-year convention. FX
volume is not exposed through the equity volume features and is never silently
treated as centralized exchange volume.

## Missing data and calendars

The engine accepts validated bars only, preserves their timestamps, and never
inserts or interpolates rows. A weekend, holiday, early close, or other market
closure is therefore not converted into synthetic hourly observations. Returns
are calculated between adjacent supplied observations; consumers must use the
Phase 1 data-quality/calendar results when deciding whether a gap is acceptable.

The initial window semantics are observation-count based, not elapsed-time
based. A future calendar-aware window policy must be added explicitly rather
than inferred by feature code.

## Lineage and reproducibility

Each observation records the source dataset version, symbol, asset class,
timeframe, raw/adjusted policy, feature name/version, effective parameters,
input columns, dependencies, implementation version, and computation timestamp.
The computation timestamp is operational metadata; numeric values are a pure
function of bars, parameters, and feature version. Raw and adjusted bars cannot
be mixed in one context.

To reproduce a result, use the same dataset version, adjustment policy, ordered
validated bars, feature request, and feature implementation version.

## Leakage defenses

The tests cover future-row mutation, appending future rows, causal rolling
boundaries, previous-window breakout levels, UTC/naive timestamp rejection,
raw/adjusted separation, deterministic repeated computation, and availability
timestamps after bar completion. No scaler, label generator, resampler, or
future-data transformation is included in Phase 2.

## Known limitations

- Feature storage is currently an in-memory reference adapter; PostgreSQL
  persistence and feature migrations are deferred until query workloads are
  established.
- Computation uses observation-count windows. It does not infer missing bars or
  session-aware elapsed-time windows.
- Corporate-action timing and survivorship-aware instrument-universe assembly
  remain responsibilities of the Phase 1 dataset/provider lineage and future
  research layers.
- No multi-timeframe resampling is performed. This prevents incomplete
  higher-timeframe bars from being accidentally treated as known.
