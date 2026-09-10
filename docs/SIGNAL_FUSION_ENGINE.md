# Signal Fusion Engine

Phase 6 combines independently produced Phase 4 strategy signals and one
strictly aligned Phase 5 regime state into an immutable `UnifiedTradeIntent`.
It is a deterministic information-combination boundary, not a claim that a
combined signal is profitable.

## Responsibilities and boundary

```text
Phase 4 StrategySignal values + Phase 5 RegimeState
                    ↓
             FusionContext
                    ↓
      normalized eligibility + compatibility
                    ↓
        MajorityVoteFusionPolicy v1
                    ↓
          UnifiedTradeIntent (no quantity)
                    ↓
       Phase 7 risk/sizing and later execution
```

The package has no dependency on broker, network, credentials, portfolio,
backtesting execution, or order models. In particular, `UnifiedTradeIntent`
has no quantity, leverage, allocation, stop, take-profit, portfolio-risk, or
broker-order field. Phase 3 requires a quantity-bearing order, so an adapter is
intentionally deferred until Phase 7 establishes the risk and sizing boundary.
This preserves Phase 3 execution semantics rather than creating an unsafe
second execution path.

## Signal semantics and normalization

`NormalizedSignal` is the minimal wrapper needed to carry eligibility and an
explicit unavailable state while preserving a Phase 4 `StrategySignal` source
ID, strategy ID/version, instrument, timeframe, UTC decision timestamp,
deterministic score, reason, and feature provenance. It does not manufacture
probabilistic confidence; Phase 4 score is retained only as an auditable score
and is not used by the baseline vote.

| Direction | Meaning in fusion |
| --- | --- |
| `LONG` | Proposes positive exposure. |
| `SHORT` | Proposes negative exposure. |
| `FLAT` | Explicitly proposes no exposure. If no directional signal remains, the fused result is `FLAT`. |
| `HOLD` | No new directional instruction. It does not force an exit and remains `HOLD` when no directional or flat signal remains. |
| `UNAVAILABLE` | Cannot participate. It is excluded, never treated as neutral or flat. |

## Strict input and temporal contract

`FusionContext` is immutable and contains one instrument, timeframe, UTC
decision timestamp, one or more Phase 4 signals, and an optional Phase 5 state.
Every signal and supplied regime state must match the scope and decision
timestamp exactly. Future signals/states raise domain-specific errors; earlier
signals/states also fail rather than being forward-filled. Cross-instrument and
cross-timeframe values fail explicitly. Cross-asset and multi-timeframe fusion
are not implemented.

The context permits an exact duplicate source signal only so it can be recorded
as a duplicate exclusion. A strategy ID/version may otherwise contribute only
one signal at a decision point; conflicting IDs from one instance fail closed.

## Regime eligibility and compatibility

The baseline requires a known usable regime: `ACTIVE` data/session,
non-unavailable trend and volatility, and liquidity not `UNKNOWN`. Otherwise
the fused result is `UNAVAILABLE` with `REGIME_UNAVAILABLE`; it does not invent
a normal/neutral regime.

`RegimeCompatibilityRule` is declarative and versioned as policy configuration.
Rules can restrict a strategy ID/version and optional signal direction by allowed
trend, volatility, and liquidity states. Multiple applicable rules are an OR of
their complete constraints; a strategy with no configured rules is unrestricted,
while a strategy with rules must match at least one direction-specific rule.
Compatibility is a configured hypothesis, not evidence of
profitability. Every exclusion records a deterministic reason.

## Policy and conflicts

The implemented policy is `majority_vote` version `1`.

- It counts eligible `LONG` and `SHORT` strategy instances equally.
- It uses explicit positive integer `minimum_directional_votes` and
  `minimum_margin` parameters (both default to 1).
- Ties and unmet thresholds return `HOLD` / `NO_CONSENSUS`; no tie is broken
  arbitrarily.
- `FLAT` produces fused `FLAT` only if there is no eligible directional signal.
- `HOLD` does not vote for an exit.
- Scores, confidence, historical P&L, returns, Sharpe, win rate, drawdown, and
  future observations have no effect on the policy.

Duplicate source signals are represented in provenance as `DUPLICATE` and have
no additional vote. The policy never weights a strategy by repeated emission.

## Versioning, provenance, and failure behavior

`FusionPolicyMetadata` declares policy ID/version/schema. The instance-scoped
`FusionPolicyRegistry` rejects duplicate identities and ambiguous unversioned
lookups. `configuration_id` is SHA-256 over policy ID/version, validated
parameters, and canonical compatibility rules; no performance data is included.

`UnifiedTradeIntent` includes deterministic intent ID, policy and configuration
identity, Phase 5 state/detector identity, all eligible normalized signals,
every excluded signal and reason, and directional/non-directional counts. This
allows an investigator to reconstruct which source signals were considered,
why one was excluded, which regime applied, and exactly which configuration
resolved the result.

Invalid configuration, duplicate conflicting strategy instances, mismatched
scope, future/stale timestamps, and unknown policy versions fail explicitly.
Unavailable regime or signal information becomes an explicit no-instruction
result, never a fabricated directional vote.

## Limitations and deferrals

Phase 6 implements only unweighted majority voting. It has no weighted voting,
dynamic weights, learned weights, historical performance selection, optimizer,
ML, meta-labeling, cross-asset fusion, multi-timeframe fusion, persistence,
risk firewall, sizing, broker integration, paper trading, or live trading.
It also intentionally does not create Phase 3 orders until Phase 7 provides
the required risk and position-sizing decision.
