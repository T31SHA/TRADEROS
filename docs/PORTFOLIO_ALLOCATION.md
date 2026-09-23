# Deterministic Portfolio Allocation V1

TRADEROS allocation V1 is a pure, versioned proposal layer between verified
strategy order intents and the existing risk firewall. It does not authorize
orders, size fills independently, or replace account accounting.

An allocation signal must carry strategy/version/artifact/family identity,
instrument and timeframe, event/availability/decision timestamps, directional
semantics, configuration lineage, and an explicit notional request. A score or
direction alone is never converted into expected return, confidence,
volatility, or risk.

`AllocationPolicy` requires explicit account gross/net, incremental, and
turnover budgets. It can also define instrument, strategy, family, and
authoritatively classified asset-class limits. Its canonical configuration
identity changes whenever policy inputs change. Opposing contributions use the
declared `NET_OPPOSING` or `REJECT_OPPOSING` policy; the implementation makes
no correlation or diversification claim.

The pure allocator is ordering-invariant, collapses duplicate strategy
evidence, includes current positions and outstanding reservations, preserves
over-limit holdings in its proposal, and distinguishes reductions from new
risk and reversals. Cross-currency exposure requires an explicit conversion
rate and identity; otherwise the proposal blocks.

The worker revalidates the portfolio revision and strategy governance after
proposal creation. The existing firewall still has absolute veto authority,
and the allocator only narrows its approved authorization before the existing
generation-fenced paper engine consumes it. The default configured worker has
no approved allocation policy, so its operational path remains `NO_TRADE`
until an explicitly injected policy is supplied. No broker, live-trading, or
promotion authority is added.

V1's operational worker remains one instrument per cycle. The pure contract
supports multiple instruments, but coordinated multi-instrument orchestration
requires additional reservation and concurrency coverage before it can be
claimed.
