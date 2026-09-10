# AGENTS.md

## Mission

TRADEROS (Trading Research, Execution, Risk & Decision Operating System) is a
research-grade quantitative platform for Forex and equities. The project must
remain auditable, reproducible, risk-controlled, and useful even when optional
AI services are unavailable.

## Engineering rules

- Work phase-by-phase according to `ROADMAP.md`.
- Preserve the lifecycle: research → backtest → cost-aware backtest →
  walk-forward → out-of-sample → robustness/stress → paper → human approval →
  controlled live deployment.
- Strategies emit signals; they never place orders.
- The risk firewall has veto authority. A failed risk check, stale-data check,
  or unavailable safety dependency means **do not trade**.
- Live trading is disabled by default and requires both `TRADING_MODE=live` and
  `LIVE_TRADING_ENABLED=true`, plus the human approvals defined by policy.
- Do not add real credentials, real broker connectivity, or live execution to
  the repository without explicit approval and the required validation gate.
- All times are UTC at system boundaries. Market-specific calendars are domain
  concerns, not global configuration hacks.
- Never use future information in features, labels, validation, or simulation.
- Prefer deterministic, typed, small modules over hidden global state and
  premature microservices.

## Workflow

Before changing a phase:

1. Inspect the repository, tests, and current documentation.
2. Make the smallest coherent implementation for the requested phase.
3. Add or update tests for every behavior and safety invariant introduced.
4. Run formatting/linting, type checks, tests, and relevant integration checks.
5. Review the diff and update documentation to match the implementation.

## Local commands

```bash
python -m pytest
ruff check .
mypy src
```

The project targets Python 3.12+. Use the package manager configured by the
developer environment; do not commit generated environments or secrets.

## Change boundaries

The Phase 0 package layout is an architectural boundary, not permission to
implement future trading features early. Any future phase must preserve the
public interfaces and safety guarantees documented in `ARCHITECTURE.md` and
`RISK_POLICY.md`.
