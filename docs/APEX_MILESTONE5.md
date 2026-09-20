# APEX Milestone 5

`scripts/run_apex_milestone5.py` is the governed first-baseline preflight and
evaluation entry point. It inventories only the configured local data root,
verifies the existing raw/normalized/quality artifacts, records an immutable
qualification fact, freezes the protocol, and stops before strategy execution
when a required gate fails.

The command requires an explicit dataset identity. It does not download data,
search private directories, select data by performance, tune parameters, or
connect to a broker:

```bash
./.venv/bin/python scripts/run_apex_milestone5.py \
  --dataset-id <inventoried-dataset-id> \
  --data-root data \
  --output-dir research/results/apex_m5
```

The frozen protocol uses the registered `forex_trend_following.v1` baseline
(10/30 EMA, target quantity 1), causal feature lineage, chronological
development and locked-OOS boundaries, warmup exclusion, no-trade and
buy-and-hold controls, observed/adverse spread scenarios, next-bar execution,
Phase 7 risk veto, Phase 8 sizing, sample-adequacy rules, and declared
uncertainty/test-family methods. It never changes the baseline implementation.

The output directory contains machine-readable `result.json`, `protocols/`,
`qualifications/`, and `reproducibility.json`, plus the concise `report.md`.
Raw market data, normalized data, manifests, caches, credentials, and private
files are excluded from the source snapshot digest. A dirty worktree is
reported as dirty; it is not represented as a clean reviewed commit.

For the executed Milestone 5 run, see
[`research/results/apex_m5/report.md`](../research/results/apex_m5/report.md).
The current local artifact is `BLOCKED_DATA`: its raw hashes and normalized
content verify, but its admission evidence has unexpected active-session gaps,
no source-version/license reference, and insufficient coverage for the frozen
adequacy design. No development, walk-forward, or locked-OOS performance
result is available.
