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
Raw Source → Immutable Artifact → Dukascopy-compatible CSV Import
→ canonical local JSONL / MarketBar mapping → Quality Audit
→ immutable dataset manifest → qualification gate
```

`traderos.data.dukascopy` accepts only a local UTF-8 CSV with an explicitly
declared source timezone, timestamp format (`iso_8601` or
`epoch_milliseconds`), timestamp convention (`bar_start` or `bar_end`), and
selected unmodified quote side. Epoch milliseconds are converted from integer
milliseconds directly to UTC. It preserves complete bid/ask OHLC and volumes
in the normalized JSONL. Dukascopy-node's bare `volume` column maps to
`bid_volume` only when `QuoteConvention.BID` is explicitly selected;
`ask_volume` is never invented. Its `to_market_bar` mapping uses the configured
side for the existing single-OHLC `MarketBar` contract and uses bid/ask closes
only when supplied. No quote, spread, price, volume, or missing bar is
fabricated.

`RawArtifactStore` content-addresses files by streaming SHA-256 under
`data/raw/dukascopy/<hash>/`; it never overwrites bytes. Metadata records
source, optional source version, source symbol, requested range/timeframe,
timezone, original name, byte size, hash, and UTC download time. Normalized
content, quality report, and manifest are immutable paths in `data/normalized/`
and `data/manifests/`. These local artifacts are git-ignored.

The versioned `AdmissionPolicy` has explicit zero-default tolerances for
duplicates, non-monotonic timestamps, OHLC/price/volume/crossed-quote defects,
unexpected gaps, active-session zero-volume bars, and schema drift. The audit
reports coverage, gaps and Forex weekend closures, stale sequences, jumps,
flat/zero-volume classifications, and exact spread statistics. A source-aware,
versioned quantization policy may normalize only one finite, positive OHLC
ordering inversion no larger than the configured price tick; raw artifact hash
and row number remain attached to every normalized record. This is explicit
source normalization, not fabrication. A changed raw byte, row, timestamp,
price, tick size, quantization policy, metadata, source convention, calendar,
or schema produces a new dataset identity. The local runner
`scripts/admit_real_dukascopy.py` preserves and admits the local EUR/USD 15m
bid-only Dukascopy-node artifact with `BAR_START`, UTC,
`EPOCH_MILLISECONDS`, `QuoteConvention.BID`, `price_tick_size=0.00001`, and
`dukascopy-forex-utc-session-v1`. Its admission result is an audit decision only; it
does not start empirical strategy research. The states are
`DETERMINISTIC_TEST_FIXTURE`, `EMPIRICALLY_QUALIFIED_DATASET`, and `BLOCKED`.
The offline `scripts/diagnose_dukascopy_sessions.py` tool reports every missing
run and groups active gaps and zero-volume bars by UTC hour, weekday, month,
DST state, run length, and OHLC movement without modifying the raw artifact.
