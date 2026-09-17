# Testing Policy

Testing is a release gate, not a postscript.

## Required layers

- Unit tests for deterministic domain calculations and configuration.
- Integration tests for adapters, persistence, data pipelines, APIs, and paper
  execution.
- System tests for signal → risk → order and order → fill → portfolio flows.
- Security tests for authentication, authorization, secrets, and invalid input.

## Critical invariants

The suite must explicitly test look-ahead bias, data leakage, duplicate orders,
race conditions, stale data, risk-limit bypass, kill-switch behavior, position
sizing, P&L, and timezone handling. Phase 9 additionally requires fail-closed
dataset qualification, hash-bound manifests, chronological/purged folds, OOS
lock identity, candidate/family lineage, seed determinism, FDR accounting,
data-only evidence export, and a paper-only promotion boundary.
Canonical replay tests must additionally prove Phase 2 → 5 → 4 → 6 → 7 →
Phase 8 sizing → Phase 3 timing, including a risk-veto path that creates no
Phase 3 order. Deterministic fixtures validate architecture only and must not
be represented as empirical market evidence.

## Local gate

```bash
ruff check .
mypy src
pytest
```

Tests must be deterministic and must not require real broker credentials,
external market data, or a live network unless a test is explicitly marked as
an opt-in integration test.

## Evidence

Every validation result must identify the code/configuration version, dataset,
parameters, timeframe, universe, dates, costs, and result artifact.
