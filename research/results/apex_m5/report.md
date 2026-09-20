# APEX Milestone 5 — First Governed Empirical Baseline Evaluation

Research verdict: **BLOCKED_DATA**

No strategy execution occurred because the configured historical dataset failed the authoritative data gate. Profitability was not evaluated.

## Frozen identities

- Protocol: `apex-m5-1a4560328c3cea8ebab4db67` / `1a4560328c3cea8ebab4db676652eeca08ba038a69df9e7f6c3da29f7cc095c7`
- Base commit: `a15d8f18e514764c19f9aff289b5698eb627ab6e`
- Source snapshot/content digest: `85ed34292520113c1de4faeca1130c8971bfd05d13e6ff2cdb46da64e702fb67`
- Dirty state: `dirty`
- Dataset: `f49cd41e3c0c431a8c9b8812827197fffde81446443c8f8f24fc573a03f7b3f4`
- Qualification: `3a631bd1808394e3e794be0974e4219b5780e1e54de3ab40abe4eef7b4a41425` (rejected)

## Data-gate blockers

- `COVERAGE_SHORT_FOR_DECLARED_PROTOCOL`
- `LICENSE_REFERENCE_MISSING`
- `QUALITY_UNEXPECTED_GAPS_40`
- `SOURCE_IDENTITY_INCOMPLETE`
- `SOURCE_VERSION_MISSING`

## Evaluation status

| Stage | Status | Metrics |
|---|---|---|
| Development | NOT_EXECUTED | UNAVAILABLE |
| Walk-forward | NOT_EXECUTED | UNAVAILABLE |
| Locked OOS | NOT_EXECUTED | UNAVAILABLE |

The predeclared baseline is `forex_trend_following.v1` with its registered 10/30 EMA parameters and target quantity 1. The protocol includes no-trade and buy-and-hold controls, explicit base/adverse cost scenarios, next-bar execution, warmup exclusion, sample-adequacy rules, and locked-OOS access rules. These declarations were frozen before any performance inspection.

No promotion, capital allocation, broker connection, or live execution occurred.
