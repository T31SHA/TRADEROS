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

The Dukascopy-compatible importer is an offline import boundary, not a live
provider: external data must first be preserved as a local immutable raw
artifact. It requires an explicit source timezone and bar-start/bar-end
declaration; it never assumes UTC or guesses timestamp semantics. UTC is the
normalized system boundary and Phase 1's Forex weekend calendar determines
expected closures. Missing in-session bars are data gaps; closed intervals are
not silently filled or treated as corruption. A present zero-volume flat bar is
retained and classified against the Forex calendar. An active-session
zero-volume bar is a blocker by default; a weekend/closed-session zero-volume
bar is an expected closure observation. Dukascopy-node volume is preserved as
source-provided Dukascopy volume and is not interpreted as centralized,
market-wide traded volume.

The importer may apply the explicit, versioned Dukascopy fixed-price
quantization policy only to a finite, positive one-tick OHLC ordering inversion
when `price_tick_size` is configured. The normalized record retains raw artifact
hash and source row provenance, while the immutable raw CSV remains authoritative
evidence. Larger or multi-field inconsistencies remain blocking quality errors.

The admission manifest binds hashes of all raw artifacts, source metadata,
instrument, timeframe, quote convention, timestamp semantics, adjustment
policy, calendar, quality policy/report, importer, canonical schema, and
normalized content. A real dataset becomes empirical only when the explicit
quality policy passes. Test fixtures are permanently marked
`DETERMINISTIC_TEST_FIXTURE`; they can never support an empirical claim.
