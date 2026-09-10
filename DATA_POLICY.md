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

## Credentials and providers

Providers are accessed through interfaces and adapters. Credentials come only
from environment variables or an approved secret manager. They must never be
committed, logged, or embedded in notebooks, tests, fixtures, or documentation.
