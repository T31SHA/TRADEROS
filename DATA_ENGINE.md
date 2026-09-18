# Phase 1 Data Engine

## Operational flow

```text
provider adapter
  → provider-neutral BarCandidate
  → explicit timestamp normalization
  → validated MarketBar
  → calendar-aware DataQualityEngine
  → idempotent MarketDataStore
```

## Provider boundary

Implementations satisfy `MarketDataProvider` and expose canonical instrument
metadata plus `BarCandidate` values. Vendor response parsing and symbol/timeframe
translation remain inside the adapter. Phase 1 ships only
`DeterministicLocalProvider`; it is suitable for tests and offline research and
does not use credentials or a network.

## Time and sessions

Aware timestamps are converted to UTC. Naive timestamps require an explicit
source timezone; ambiguous or nonexistent DST values are rejected. Canonical
bars and lineage timestamps are UTC-aware.

Forex uses the Phase 1 weekend policy of Friday 22:00 UTC through Sunday 22:00
UTC closed. US equities use the regular 09:30–16:00 America/New_York session.
Holiday and early-close data is injected into `UsEquityCalendar`; it is not
silently fabricated.

## Validation and quality

Invalid prices, OHLC relationships, volumes, timestamps, symbol/timeframe
mismatches, out-of-range provider records, duplicates, missing in-session bars,
unexpected timestamps, suspicious jumps, stale data, and mixed logical series
are represented as explicit quality findings. Findings are warnings or errors;
the system does not silently repair or delete data.

## Storage and lineage

`InMemoryMarketDataStore` supports deterministic offline tests. The production
target is `SqlAlchemyMarketDataStore` backed by PostgreSQL. The migration creates
`instruments`, `data_sources`, `dataset_versions`, `market_data`,
`data_ingestion_runs`, and `data_quality_events` with constraints and indexes.

Raw and adjusted data are distinguished by `adjustment_policy`, which is part of
the logical bar identity. Dataset versions hash the request, policy,
configuration, and canonical bar content; operational ingestion timestamps are
recorded separately.

## Retry and failure behavior

Only transient provider failures are retried, with a configurable bounded retry
count and exponential backoff. Authentication, malformed-response, validation,
storage, and other permanent errors fail explicitly. Failed runs are recorded
with status and error text; credentials are never logged or persisted.

## Current limitation

No external provider adapter, production holiday-data source, Redis cache, API,
or CLI is included in Phase 1. These are deliberate boundaries, not simulated
capabilities.

## Empirical historical-data admission gate

The Phase 9 research framework now has a separate, local-only admission path
for the first real Forex dataset. It does not download data and it does not run
a backtest:

```text
Dukascopy ticks → immutable raw bytes → deterministic M15 aggregation
→ canonical bid/ask JSONL + MarketBar mapping → quality/admission gate
```

`traderos.data.ticks` accepts only the exact Dukascopy-node schema
`timestamp,askPrice,bidPrice,askVolume,bidVolume`. Epoch milliseconds are
normalized directly to UTC. Ticks must remain strictly increasing in raw order;
bid/ask, positive-price, and finite non-negative-volume defects are reported
and block admission. Aggregation uses bar-start UTC buckets and preserves both
bid and ask OHLCV plus closing, mean, minimum, and maximum spread. Empty M15
intervals are absent; no quote, volume, spread, OHLC, or missing bar is
fabricated. The existing single-side `MarketBar` contract is populated only by
an explicit bid/ask mapping.

The existing `traderos.data.dukascopy` bar importer remains available for
immutable v1/v2 artifacts and focused compatibility tests. Those aggregated
artifacts are not the canonical empirical source.

`RawArtifactStore` content-addresses files by streaming SHA-256 under
`data/raw/dukascopy/<hash>/`; it never overwrites bytes. Tick metadata records
source, source symbol, coverage start/end, timezone, original filename, hash,
byte size, download timestamp, and optional known source version. Normalized
content, quality report, and manifest are immutable paths in
`data/normalized/` and `data/manifests/`. These local artifacts are git-ignored.

The versioned `AdmissionPolicy` has explicit zero-default tolerances for
duplicate/non-monotonic ticks, invalid prices, crossed quotes, volume defects,
OHLC violations, unexpected or unknown gaps, and active-session zero-volume
bars. The tick audit reports raw rows, M15 bars, coverage, missing intervals,
closures, stale sequences, suspicious jumps, zero-volume classifications, and
spread statistics. Tick-derived M15 bars do not use the prior one-tick
source-candle quantization repair. A changed raw byte, tick, aggregation policy,
metadata, calendar, timestamp semantics, quality policy, or quote configuration
produces a new dataset identity. The bounded local runner
`scripts/admit_real_dukascopy_ticks.py` accepts a caller-supplied artifact and
never downloads or expands its interval. The states are
`DETERMINISTIC_TEST_FIXTURE`, `EMPIRICALLY_QUALIFIED_DATASET`, and `BLOCKED`.
The offline `scripts/diagnose_dukascopy_sessions.py` tool reports every missing
run and groups active gaps and zero-volume bars by UTC hour, weekday, month,
DST state, run length, and OHLC movement without modifying the raw artifact.

## Twelve Data Forex OHLCV research admission

Twelve Data is a separate OHLCV research source. Acquisition is external:

```text
Twelve Data EUR/USD 15min CSV → immutable local raw bytes
→ source-specific CSV parser → provider-neutral OHLCV record
→ existing empirical quality/admission gate
```

`traderos.data.twelvedata` accepts only local CSV artifacts. It requires the
observed conceptual header `datetime,open,high,low,close` and optionally maps
`volume`; header aliases are explicit and header signatures are recorded for
schema-drift detection. Timestamps must be explicit UTC ISO-8601 values with
BAR_START semantics. Naive values are rejected, rows are not sorted, and no
OHLC repair or quantization adjustment is applied.

The raw artifact metadata uses `source=twelve_data`, `source_symbol=EUR/USD`,
`requested_timeframe=15min`, UTC, filename, download timestamp, SHA-256, and
byte size. Multiple bounded artifacts may be preserved independently; every
hash enters the immutable manifest. `TwelveDataForexCalendar` has its own
versioned identity and conservatively models the conventional FX weekend
closure pending validation of the acquired artifact's published session
semantics.

The resulting manifest explicitly records `source_mode=ohlcv_bar`,
`quote_configuration=bid_ask_unavailable`, and
`volume_semantics=source-provided Forex volume`. No bid/ask, spread, or
centralized-volume observation is fabricated. Any backtest using this source
must record an explicit transaction-cost model and version separately from the
source data. `scripts/admit_real_twelve_data.py` performs preservation and
admission only; it never downloads, calls a broker, or runs strategies.

`compare_close_series` is an optional data-quality report for overlapping
timestamps. It keeps Twelve Data and Dukascopy separate and reports absolute
close-difference distribution metrics plus signed mean difference; it cannot
select a source using strategy performance.
