-- Foundation hardening: preserve the availability time of aggregated quotes.
-- A bar's bid/ask values may be closing observations and are not executable at
-- the bar opening unless this timestamp proves they were already available.

ALTER TABLE market_data
    ADD COLUMN IF NOT EXISTS quote_timestamp TIMESTAMPTZ;
