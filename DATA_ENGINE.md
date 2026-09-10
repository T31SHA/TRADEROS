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
