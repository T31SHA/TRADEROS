# Governed strategy artifacts

Milestone 2 stores each strategy version as an immutable `strategy-artifact.v1`
document.  `strategy_id` and `strategy_version` are lookup identity; the
SHA-256 `artifact_hash` covers the complete canonical JSON document and is a
separate integrity identity.  Parameters are JSON values with explicit type
tags (`integer`, `float`, `decimal`, `boolean`, `string`, `list`, `mapping`, or
`null`), so `1`, `1.0`, and `"1"` cannot silently become the same value.
Unknown fields, non-finite numbers, unsupported objects, unregistered logical
implementation references, and invalid research references are rejected.

An artifact may explicitly leave evidence dimensions unavailable.  Dataset,
configuration, code, environment, feature, cost, capacity, regime, risk,
training, validation, and OOS references are metadata; validation evidence is
attached separately so it cannot create a circular hash.

## Deployment stages

| Source | Allowed target | Required authority/evidence |
| --- | --- | --- |
| CANDIDATE | RESEARCH | trusted research actor |
| RESEARCH | VALIDATION | trusted research actor + exact evidence |
| VALIDATION | SHADOW | transition actor + exact passing empirical evidence + explicit approval |
| SHADOW | PAPER | transition actor + exact passing empirical evidence + explicit approval |
| PAPER | SHADOW | transition actor and demotion reason |
| SHADOW | VALIDATION | transition actor and demotion reason |
| Any non-terminal stage | RETIRED | trusted retirement actor and reason |
| CANARY / ACTIVE | any | unavailable in this milestone |
| RETIRED | any | terminal; create a new version with predecessor lineage |

SHADOW records eligible decisions without submitting orders.  PAPER eligibility
only permits a separate authorized paper orchestrator to consider the version;
the lifecycle service itself does not execute, allocate, or authorize capital.
Fixture evidence may support software tests and research memory but can never
authorize SHADOW or PAPER.  Contaminated, revoked, mismatched, unavailable, or
missing evidence fails closed with a stable reason code.
An explicitly recorded `StrategyHealth.FAILED` state also rejects operational
eligibility. Other health states remain policy-neutral until an approved health
policy defines their treatment; the lifecycle service does not invent runtime
thresholds or disable strategies autonomously.

## Trust and persistence boundary

Mutations are internal application-service calls.  The service resolves an
actor ID through an explicit in-process `ActorDirectory`; a caller-supplied
role string is never treated as proof.  This repository has no production
identity provider, so the service is not exposed as an unauthenticated HTTP
mutation API.  Research actors cannot issue approvals for their own artifact.

Artifacts, evidence, approvals, revocations, and lifecycle events are append
only.  The current state is a projection updated atomically with each event.
Transitions require the expected monotonic revision and an idempotency key;
the PostgreSQL store locks the projection row during the decision and update.
Recovery verifies the event chain and refuses silent projection repair.

Migration 007 is applied after migrations 001 through 006.  PostgreSQL is the
durable production target; SQLAlchemy schema creation exists only for isolated
local tests.  Live execution remains disabled and no lifecycle transition
creates orders or fills.
