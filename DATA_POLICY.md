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

`ForexCalendar` models the Phase 1 weekend closure from Friday 22:00 UTC to
Sunday 22:00 UTC. `UsEquityCalendar` models regular US sessions in
`America/New_York`; holidays and early closes are injected by the caller. No
holiday list is silently hardcoded. The known limitation is that broker-specific
Forex rollover and a production exchange-holiday source are not yet connected.

Lineage records include provider, symbol, timeframe, range, adjustment policy,
configuration version, stable dataset version, quality status, ingestion runs,
and quality events. Ingestion timestamps are operational metadata and therefore
vary between runs; the dataset version excludes that non-deterministic field.
