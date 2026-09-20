# APEX Milestone 5 acquisition gate

Verdict: **BLOCKED_RIGHTS**.

No market-content request was made. The bounded pilot and two-year bulk
acquisition were both recorded as `NOT_ATTEMPTED`; downloaded and verified
bytes are zero. No strategy, pilot backtest, qualification, new protocol, or
OOS access was run.

## Fixed acquisition plan

The plan fixes EUR/USD tick coverage to `2024-01-01T00:00:00Z` through
`2026-01-01T00:00:00Z`, because 2024 and 2025 are the two most recent complete
calendar years preceding the current UTC year. The pilot is the first fixed
six-hour interval, `2024-01-01T00:00:00Z` through `06:00:00Z`; it was not chosen
from market behavior.

The plan is machine-readable at
[`acquisition_plan.json`](acquisition_plan.json), with the immutable attempted-
request state at [`acquisition_manifest.json`](acquisition_manifest.json) and
terms findings at [`terms_evidence.json`](terms_evidence.json).

The proposed existing downloader is `dukascopy-node@1.50.0`, kept distinct
from any provider data revision. Its documented tick output matches the
repository's required bid/ask schema. It was not installed because the rights
gate failed first.

## Why acquisition stopped

1. The official Dukascopy export documentation describes a Requester Pays S3
   workflow. A Requester Pays request can create a charge, which violates the
   current zero-cost authorization.
2. The official Dukascopy Europe terms prohibit automated acquisition and
   database construction without prior express written consent. Whether that
   page governs the exact raw tick endpoint is unresolved, which is sufficient
   to fail closed.
3. The official XML agreement is a registration/approval agreement for XML
   data, not evidence of permission for raw `.bi5` bid/ask ticks.

The evidence inventory does not treat a local source label or SHA-256 as proof
of provider authenticity. The old local CSV remains rejected and unchanged;
its 40 open-session gaps and missing source/license identity are not repaired
by this plan.

## Conditional operator commands

These commands are copyable templates only. Do not run them until written
provider permission for the exact endpoint/use and a zero-cost access path
have been supplied. They contain no credentials and are not evidence of an
acquisition.

```bash
NPM_CONFIG_CACHE=/tmp/traderos-npm-cache npx --yes --package dukascopy-node@1.50.0 \
  dukascopy-node -i eurusd -from 2024-01-01 -to 2024-01-02 -t tick -f csv \
  -dir <PILOT_OUTPUT_DIR>
```

After the pilot is independently inspected and its native/converted hashes
are captured, admit only the fixed local artifact through the authoritative
path:

```bash
<PYTHON> scripts/admit_real_dukascopy_ticks.py <PILOT_OR_BULK_CSV> \
  --coverage-start <UTC_START> \
  --coverage-end <UTC_END> \
  --download-timestamp <ACQUISITION_UTC> \
  --source-version dukascopy-node@1.50.0 \
  --license-reference <WRITTEN_PROVIDER_PERMISSION_REFERENCE> \
  --data-root <DATA_ROOT>
```

The required next operator action is to supply the written permission/terms
decision and cost authorization described in `terms_evidence.json`. If those
inputs are supplied, rerun the fixed pilot, add its exact per-segment manifest
record, then acquire the remaining fixed coverage with the declared limits.
Do not run `run_apex_milestone5.py` until admission passes and a new protocol
is frozen against the new dataset.

## Identity and preservation

The current source is the dirty working tree at base commit
`a15d8f18e514764c19f9aff289b5698eb627ab6e`, with source snapshot/content digest
`45a13848ac6fe5759100c941172cd94dd0e864c38761d5696e6a174b0869ca9d` under the
repository's exclusion rules. Python is 3.14.4 in
`/home/sharahbill/TRADEROS/.venv/bin/python`; CI currently targets Python 3.12,
so this is not an exact CI-runtime reproduction.

The prior blocked result, qualification, frozen protocol, reproducibility
record, remediation gap inventory, and source/license inventory remain at
their original paths. No original evidence was overwritten.

## Safety confirmations

- No money spent or paid resource created.
- No credentials used and no terms accepted on the operator's behalf.
- No broker connection, live order, capital allocation, or strategy promotion.
- No raw market data was downloaded during this acquisition attempt.
- No original evidence or rejected artifact was overwritten.
