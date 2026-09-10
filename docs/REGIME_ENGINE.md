# Regime Engine

Phase 5 adds a deterministic, causal market-observation subsystem. It answers
what environment a single instrument is in at a completed-bar decision time.
It does not produce signals or orders, select trades, size positions, optimize
parameters, call a network service, or use machine learning.

## Architecture

```text
validated Phase 1 market bar
        +
causal Phase 2 feature observations
        ↓
immutable RegimeContext
        ↓
versioned detector
        ↓
immutable RegimeState
        ↓
future Phase 6 signal fusion (not implemented)
```

`RegimeEngine` is only observation orchestration. A research or future
backtesting pipeline creates one bounded context when a completed bar becomes
available, then calls `evaluate`. The engine does not import strategy,
backtesting execution, portfolio, risk, broker, or signal-fusion code.

## Contract and causality

`RegimeContext` is a frozen dataclass containing one current `MarketBar`, UTC
decision timestamp, its Phase 1 `MarketCalendar`, detector parameters, and
only causal Phase 2 `FeatureObservation` values. It optionally accepts quality
events and a prior state for a future explicitly stateful detector. The Phase 5
detector is stateless and ignores the prior state.

The context rejects a feature from a different instrument or timeframe, a
feature observed after the current bar, or a feature whose availability time is
after the decision time. It also rejects future quality events and prior states
that are not strictly historical or that cross an instrument/timeframe. Each
required feature must be an observation for the current completed bar; older
features are not silently treated as current.

Phase 1 bar timestamps are bar starts. A decision cannot precede
`bar.timestamp + timeframe.duration`, and Phase 2 availability timestamps must
be no later than that decision. No resampling, cross-asset input,
cross-timeframe input, full-dataset normalization, interpolation, or forward
fill is present.

## Versioning and provenance

`EmaPercentileRegimeDetector` is `ema_percentile_regime` version `1`. Its
metadata declares supported asset classes/timeframes, exact versioned and
parameterized Phase 2 feature requirements, and its parameter schema. The
`RegimeRegistry` is instance-scoped and rejects duplicate `(detector_id,
detector_version)` registrations; it can validate feature dependencies against
the Phase 2 registry.

The detector's `configuration_id` is a SHA-256 hash of detector ID, detector
version, and canonical effective parameters. Every immutable `RegimeState`
contains that identity plus detector identity/version, instrument, asset class,
timeframe, UTC decision timestamp, compact feature provenance references, and a
deterministic `state_id`. Classification logic is semantic versioned: changing
the rule requires a new detector version rather than silently reinterpreting
historical states.

## Regime dimensions

The output is structured dimensions, not a combinatorial label.

| Dimension | States | Method | Inputs and warmup |
| --- | --- | --- | --- |
| Trend | `TRENDING_UP`, `TRENDING_DOWN`, `NEUTRAL`, `UNAVAILABLE` | Fast/slow EMA separation normalized by `abs(slow EMA)`. Strictly above/below the minimum separation is directional; equality is neutral. | Phase 2 `ema.v1` at the configured fast and slow windows. Until both current-bar values exist, the data/session state is `WARMUP`. |
| Volatility | `LOW`, `NORMAL`, `HIGH`, `UNAVAILABLE` | Current causal volatility percentile is below the low threshold, above the high threshold, or otherwise normal. Exact threshold values are normal. | Phase 2 `volatility_percentile.v1` with configured causal window. Its underlying percentile is prefix-only; warmup remains explicit. |
| Liquidity | `NORMAL`, `STRESSED`, `UNKNOWN` | Observed spread divided by completed close is compared with maximum spread fraction. Equality is normal. Equities/ETFs additionally require current positive provider volume. | Phase 1 `spread`, or `ask - bid`, and for equities/ETFs `volume`. Missing quote/spread, missing equity volume, or unusable evidence is `UNKNOWN`; no synthetic estimate is made. |
| Data/session | `ACTIVE`, `INACTIVE`, `WARMUP`, `DATA_UNAVAILABLE` | Uses the supplied Phase 1 market calendar and explicit Phase 1 quality events. | A calendar closure is `INACTIVE`; required-feature warmup is `WARMUP`; missing/undefined required inputs or an error-severity quality event is `DATA_UNAVAILABLE`. Trend and volatility are unavailable in these cases. |

Default parameters are fast EMA 10, slow EMA 30, minimum normalized trend
separation 0.001, causal volatility-percentile window 20, low/high percentile
thresholds 0.25/0.75, maximum spread fraction 0.002, and minimum equity volume
1. All are strict, finite where numeric, explicit, and intentionally not
optimized. Fast EMA period must be less than slow EMA period; low percentile
must be less than high percentile.

`trend_strength` is retained as the signed normalized EMA separation. It is a
deterministic rule-distance score, not probabilistic or calibrated confidence.

## Data-quality and calendar behavior

The detector never converts a bad, stale, missing, invalid, or unavailable
input into a legitimate trend or volatility regime. It uses Phase 1's existing
calendar rather than inferring sessions from wall-clock timestamps. A supplied
error-severity `DataQualityEvent` yields `DATA_UNAVAILABLE`; callers retain the
underlying quality record for audit. Warnings remain observable but are not
silently reclassified as errors by the detector.

The detector has no independent stale-clock heuristic: Phase 1 owns freshness
assessment, and its explicit stale-data event is propagated. This avoids a
second, contradictory calendar/freshness implementation.

## Stability analysis

`analyze_regime_stability` is an offline descriptive utility over an already
ordered, single-instrument, single-timeframe, single-detector-configuration
history. It reports per-dimension runs/durations, transition counts and
frequencies, and observation shares. It does not feed those full-history
statistics back into detection and therefore cannot affect any historical or
current classification.

## Tested guarantees and limitations

Tests cover future-bar mutation, future append, causal percentile invariance,
future-feature rejection, warmup, missing input, quality propagation, session
closure, trend/volatility threshold equality and just-above/below behavior,
normal/stressed/unknown liquidity, instrument isolation, unsupported timeframe
failure, deterministic replay, parameter identities, registry validation,
ordered observational replay, and stability analysis validation.

This is a deliberately simple stateless baseline. It does not infer liquidity
when quotes are absent, model order-book depth, use a historical volume anomaly
distribution, support multi-timeframe or cross-asset regimes, persist state,
or apply hysteresis. Hysteresis would require an explicitly supplied prior
state and a new semantic detector version. A regime state describes conditions;
it does not claim profitability or authorize a trade.

Phase 6 may consume `RegimeState` alongside strategy signals through an
explicit signal-fusion boundary. It must not couple a detector to strategy rule
execution, risk controls, broker access, paper trading, or live trading.
