# Data Policy

## Canonical conventions

- Internal timestamps are timezone-aware UTC.
- Provider timestamps must be normalized explicitly; ambiguous or naive values
  are rejected or resolved by a documented adapter rule.
- Missing observations are reported, never silently fabricated.
- Duplicate records, invalid prices, impossible OHLC relationships, and stale
  data are quality events.

## Market coverage

V1 is intentionally narrow: seven liquid Forex pairs and a configurable,
liquid US equity/ETF universe. Timeframes are Forex 15m/1h/4h/daily and
equities 1h/daily. The universe and timeframe configuration must remain
externalized.

## Bias prevention

Equity data must account for splits, dividends, sessions, holidays, and
delistings where relevant. Forex data must preserve bid/ask or spread information
where available and document session/rollover assumptions. Dataset versions and
provider provenance are required for reproducibility.

No feature pipeline may use future observations. Leakage and look-ahead tests are
blocking tests for any phase that introduces derived data.

Phase 2 implements this policy with causal feature definitions. Features use
ordered validated bar prefixes only; a feature on a bar-start timestamp is not
available until the bar's timeframe duration has elapsed. Feature observations
retain separate observation, availability, and decision timestamps, dataset
version, and raw/adjusted policy. Current-bar breakout thresholds are always
derived from the prior completed window. Warm-up rows remain explicit nulls;
missing rows are never forward-filled or synthesized.

## Credentials and providers

Providers are accessed through interfaces and adapters. Credentials come only
from environment variables or an approved secret manager. They must never be
committed, logged, or embedded in notebooks, tests, fixtures, or documentation.

## Implemented Phase 1 behavior

The canonical models are `Instrument`, `Timeframe`, `BarCandidate`, and
`MarketBar`. Provider-neutral candidates are normalized by
`normalize_and_validate_candidate`; naive timestamps are rejected unless a
source timezone is explicitly supplied. Ambiguous and nonexistent DST local
times are rejected. Canonical bar and ingestion timestamps must be UTC.

The only implemented provider is the deterministic `local` provider. It serves
caller-supplied test/offline candidates and uses no credentials or network.
External providers must implement `MarketDataProvider` and translate their
payloads inside the adapter.

`IngestionService` applies bounded exponential retries only to transient
provider errors, records rejected records as quality events, performs
source/timeframe/range checks, and upserts valid bars. The in-memory store and
the SQLAlchemy/PostgreSQL store enforce the logical identity:

```text
symbol + timeframe + timestamp + source + adjustment_policy
```

The PostgreSQL migration adds check constraints for prices, OHLC relationships,
volume, bid/ask fields, and uniqueness, plus indexes for bounded series range
queries. Raw and adjusted datasets are never overwritten into one another.

`ForexCalendar` remains the Phase 1 fixed Friday 22:00 UTC to Sunday 22:00 UTC
calendar. The empirical Dukascopy path uses the separately versioned
`DukascopyForexCalendar` (`dukascopy-forex-utc-session-v1`), whose 17:00
`America/New_York` weekly boundary resolves to 21:00 UTC in summer and 22:00
UTC in winter through IANA timezone rules. `UsEquityCalendar` models regular US
sessions in `America/New_York`; holidays and early closes are injected by the
caller. No holiday list is silently hardcoded.

Lineage records include provider, symbol, timeframe, range, adjustment policy,
configuration version, stable dataset version, quality status, ingestion runs,
and quality events. Ingestion timestamps are operational metadata and therefore
vary between runs; the dataset version excludes that non-deterministic field.

## First governed empirical Forex dataset

The canonical empirical source is an offline Dukascopy tick artifact. External
acquisition must first preserve the exact bytes; TRADEROS does not download
ticks. The exact source schema is
`timestamp,askPrice,bidPrice,askVolume,bidVolume`, with integer Unix epoch
milliseconds normalized to UTC. Raw ticks are validated in source order and
must have finite positive bid/ask prices, `bid <= ask`, and finite
non-negative bid/ask volumes. Malformed or invalid rows are rejected or
reported as blockers; they are never silently dropped or repaired.

Deterministic M15 aggregation uses BAR_START UTC semantics. It preserves bid
and ask OHLCV and closing/mean/minimum/maximum spread. Empty intervals remain
missing. The Dukascopy session calendar classifies missing intervals as
`EXPECTED_MARKET_CLOSURE`, `DATA_GAP`, or `UNKNOWN`; active-session gaps and
unexplained active-session zero-volume bars block admission. Zero volume is not
treated as missing automatically, and source volume is not claimed to be
centralized market-wide volume.

Tick-derived M15 bars do not use the previous one-tick source-candle
quantization repair. OHLC validation is exact and any violation blocks. The
manifest binds raw tick hashes, aggregation policy/version, calendar identity,
timestamp semantics, instrument, timeframe, price/volume sides, quote
configuration, quality policy/report, importer/schema versions, and normalized
content. The prior aggregated v1/v2 artifacts remain immutable compatibility
inputs but are not canonical empirical datasets. A real dataset becomes
empirical only when the explicit quality policy passes. Test fixtures remain
`DETERMINISTIC_TEST_FIXTURE` and cannot support an empirical claim.

Twelve Data is an independent OHLCV research source and is never merged with
Dukascopy ticks or Dukascopy-derived M15 bars. The caller downloads bounded
`EUR/USD`, `15min`, UTC CSV artifacts outside TRADEROS, preserves each exact
raw response with `RawArtifactStore`, and then invokes the offline
`traderos.data.twelvedata` parser and the same provider-neutral admission gate.
The governed interpretation is `TimestampFormat=ISO_8601`,
`TimestampSemantics=BAR_START`, and explicit UTC. Naive or non-UTC timestamps,
schema drift, duplicate/overlapping conflicts, missing active-session bars,
unknown gaps, stale sequences, suspicious jumps, invalid/nonpositive/nonfinite
prices, and volume anomalies remain quality findings; no rows are sorted,
filled, repaired, or quantized.

The Twelve Data calendar identity is
`twelve-data-forex-utc-session-v1`, separate from
`dukascopy-forex-utc-session-v1`. It represents the conventional FX weekend
closure as an explicit, auditable admission assumption; acquired artifacts must
be reviewed against the provider's actual published session behavior. Weekend
absence is therefore expected only when this calendar supports it, while
unexpected active-session gaps block admission.

Twelve Data bars preserve source-provided Forex volume when present. Missing
volume is represented as unavailable and is governed by policy; it is never
described as centralized exchange volume. Bid/ask OHLC and spread observations
are unavailable. Historical execution realism must use a separate explicit
transaction-cost scenario with recorded `cost_model_id`, `cost_model_version`,
and assumptions; source OHLCV is not historical spread evidence.

An optional close comparison may report Twelve Data versus Dukascopy-derived
close differences at overlapping timestamps. It is a data-quality report only,
never a merged dataset or a source-shopping/backtest selection mechanism.
