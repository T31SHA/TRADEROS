# PAPER worker

The local worker is a restartable, fail-closed supervisory loop. It accepts
only `TRADING_MODE=paper`, uses the configured local database, never acquires
market data over the network, and routes any actionable result through the
existing feature → strategy → fusion → risk → paper path. Missing data,
ineligible strategies, or a failed safety prerequisite remain `NO_TRADE`.

The first hosted deployment (one blocked PAPER worker on a new, empty
PostgreSQL 16 database) is described in
[`RENDER_DEPLOYMENT.md`](RENDER_DEPLOYMENT.md).

Example commands use an isolated SQLite file:

```bash
TRADING_MODE=paper DATABASE_URL=sqlite:////tmp/traderos-worker.db \
  python -m traderos.worker start

TRADING_MODE=paper DATABASE_URL=sqlite:////tmp/traderos-worker.db \
  python -m traderos.worker status

TRADING_MODE=paper DATABASE_URL=sqlite:////tmp/traderos-worker.db \
  python -m traderos.worker ready

TRADING_MODE=paper DATABASE_URL=sqlite:////tmp/traderos-worker.db \
  python -m traderos.worker --json status

TRADING_MODE=paper DATABASE_URL=sqlite:////tmp/traderos-worker.db \
  python -m traderos.worker stop

TRADING_MODE=paper DATABASE_URL=sqlite:////tmp/traderos-worker.db \
  python -m traderos.worker run-once

TRADING_MODE=paper DATABASE_URL=sqlite:////tmp/traderos-worker.db \
  python -m traderos.worker strategy-health --cycle-limit 1000 --json
```

When the package is installed, `traderos-worker` is the equivalent entry
point; the repository exercise uses `python -m traderos.worker`. Configure
`WORKER_ACCOUNT_ID`, `WORKER_INSTRUMENT`, `WORKER_TIMEFRAME`,
`WORKER_DATA_SOURCE`, `WORKER_DATASET_VERSION`, `WORKER_DATASET_ID`, and
`WORKER_DATASET_QUALIFICATION_ID`, `WORKER_DATASET_QUALIFICATION_ROOT`, and
`WORKER_STRATEGY_VERSIONS` explicitly before
expecting admission. A dataset is accepted only when exactly one dataset is
persisted for that exact series with an accepted quality status; missing,
ambiguous, stale, invalid, future-dated, fixture-only, or unqualified inputs
produce `NO_TRADE`. The qualification record must be an existing, verified,
non-fixture empirical qualification whose `dataset_id` matches
`WORKER_DATASET_ID` and whose content hash matches the exact persisted bars.
Ingestion records the canonical content hash of persisted bars, and worker
admission recomputes that hash over the exact persisted dataset span. A missing
or mismatched identity blocks activity; an artifact that binds to a dataset
identity is admitted only when that hash is present and matches.
Strategy artifacts must resolve through governance to one of the
explicitly registered runtime implementations with a matching implementation
locator and verified source-manifest content identity, parameters, features,
and scope. The locator is only a load/diagnostic identity; it is never used as
the code-content hash. Unknown, mismatched, or legacy locator-only artifacts
fail closed and do not inherit approval. The worker never promotes a strategy.
The risk pipeline also requires an explicit event-time system-health snapshot. The
worker CLI derives one from its local persistence, lease, reconciliation, data,
strategy, and pipeline checks; it does not attest to an external broker or
network service. Missing checks still fail closed with
`RISK_SYSTEM_UNHEALTHY`.

`start` and `run-once` return exit `0` for healthy or safely blocked `NO_TRADE`
cycles and exit `1` for `FAILED`/`UNKNOWN` or inconsistent persisted state.
`ready` retains
its probe contract: exit `0` only means healthy running PAPER state, while
blocked, failed, stopped, or unknown state returns exit `2`.

The read-only `strategy-health` command aggregates only structured
`strategy_decision` metadata already stored in the latest bounded worker-cycle
window. The default limit is 1,000 cycles and the report states the limit,
cycles considered, replayed-cycle count, whether older cycles were truncated,
and how many strategy events were unattributed. It reports decision,
signal-direction, and intent counters; it does not infer
profitability, disable strategies, change risk, or authorize execution. Legacy
free-form trace events without structured metadata are excluded rather than
being guessed.

The database stores the workload lease, heartbeat, cycle outcomes, stop
request, last decision, reconciliation state, locks, and paper counts. When a
paper account is configured, the existing lease is account-scoped so separate
workload labels cannot concurrently control the same account; account-less
runs remain workload-scoped. The
paper account is reconciled before any future risk activity; a reconciliation
failure remains visible and blocks execution. Live mode is rejected even if
the independent live enable switch is set.

The read-only `status` and `ready` commands also check the workload lease. An
active-state
row with a missing or expired lease is reported with the first status line
`UNKNOWN`, `HEALTH=FAILED`, and `LAST_ERROR=WORKER_LEASE_EXPIRED` rather than
being reported as a healthy running worker. Supervisor-level failures,
including heartbeat/lease failures, persist `HEALTH=FAILED` before the lease
is released. Lease ownership is renewed immediately before paper pipeline
entry; a lost lease skips execution and cannot overwrite a replacement
worker's status. Financial paper mutations additionally carry the lease's
monotonic fencing generation into their database transaction. The transaction
locks `worker_leases` first, then the paper account and financial rows; a
takeover therefore either waits for the complete mutation or makes the stale
context fail before any order, fill, reservation, or projection can change.

The worker distinguishes bar event time, data ingestion/availability time,
decision cutoff, and actual processing time. Ingestion is admitted when it is
available by the decision cutoff; quote and system-health freshness are checked
against processing time and rechecked immediately before paper mutation.
Health observations are never backdated to bar close. Historical replay must
provide its explicit simulated clock rather than relabeling future observations.

The portfolio allocation V1 boundary is documented in
`docs/PORTFOLIO_ALLOCATION.md`. It requires an explicitly injected, versioned
allocation policy; the default operational worker has no such policy and
therefore remains `NO_TRADE` for new risk. Verified strategy order intents are
converted into deterministic, reservation-aware allocation proposals with
gross/net, instrument, strategy/family, asset-class, incremental, and turnover
constraints. The proposal and its portfolio revision are revalidated before
the existing firewall and generation-fenced paper engine path. Allocation
trace metadata records policy identity, constraints, attribution, targets,
reductions, and downstream risk decisions; it makes no correlation or profit
claim.

Each cycle also persists a `DECISION_TRACE` containing the global gate,
persistence, reconciliation, data admission, strategy eligibility, feature,
regime, pending-order, fusion, risk-firewall, paper-submission, and final
decision stages when reached. A repeated intent is handled by the existing
paper engine idempotency boundary; a completed worker decision is checked
before re-entering the pipeline on restart and is recorded as a replay trace
without reprocessing it. Healthy and blocked prior outcomes retain their
recorded health; failed outcomes are retried rather than silently normalized.

## Current progress record

- Source identity: reviewed revision `56ad3ad73d4fe1dced223ee7f5553e2a80d03010`
  plus the current uncommitted worktree;
  unrelated changes preserved.
- Implemented: restartable PAPER supervisor, single-worker lease, durable
  cycle/idempotency state, reconciliation gate, completed-bar and qualified
  dataset admission, recomputed canonical dataset-content identity binding,
  explicit DatasetQualificationRegistry binding requiring a verified,
  non-fixture, empirically qualified record whose dataset identity and content
  hash match the configured persisted dataset,
  admission-to-execution snapshot consistency gate, pre-pipeline
  completed-decision replay gate, exact governance/runtime binding, existing
  feature, regime, fusion, risk-firewall, pending-order, and paper-engine
  composition, plus lease-aware status observation, failure persistence, and
  fail-closed classification of all safety/data risk vetoes as `BLOCKED`.
  Paper reconciliation also requires exact identity between open orders and
  active reservations, and verifies persisted fills against their order
  authorization before a restarted worker can continue.
  The bounded read-only strategy-health window uses completion time, start time,
  and cycle identity as deterministic ordering keys when timestamps tie.
  Offline walk-forward aggregation now rejects reordered folds and results whose
  backtest period does not match its declared forward-test window.
  Evidence-package construction also rejects a backtest result or configuration
  whose experiment identity differs from the immutable experiment specification.
  Lease fencing now renews ownership before pipeline entry, verifies ownership
  after the pipeline, stops on lease loss, and prevents an expired worker from
  clobbering a replacement owner's status. Worker status writes are also
  lease-conditional in the same persistence transaction, closing the
  validity-check-then-save race. Cycle persistence is now lease-conditional
  in the same coordination transaction as the owner check, so a stale worker
  cannot append a blocked or failed result that causes its replacement to
  replay an unexecuted decision.
  Strategy decision traces now carry structured identity, direction, and
  intent metadata. The read-only `strategy-health` projection aggregates
  those existing trace events through a bounded latest-cycle query without
  adding a provenance table or policy authority; replayed cycles remain visible
  in the bounded cycle window but are excluded from fresh strategy counters.
  Configured paper accounts use account-scoped lease coordination, preventing
  concurrent workers with different workload labels from sharing one account.
  Admission is also rechecked immediately before execution so same-timestamp
  content mutations cannot reach features, risk, or paper submission.
  Completed-decision identity also includes the pre-execution reconciled
  active risk-lock snapshot, so a lock-state transition can reopen the same
  market event for a new bounded evaluation without changing the identity of
  an already processed execution.
  The recheck and pipeline decision use current execution time rather than
  the cycle-start timestamp, preventing slow cycles from bypassing freshness
  limits.
  Reconciliation exceptions now overwrite any prior healthy status with
  `RECONCILIATION=FAILED` and keep execution blocked.
  An explicit reconciliation result of `FAILED` also remains
  `HEALTH=FAILED`, rather than being relabeled as a generic block.
  Persistence and reconciliation failures also persist explicit reason codes
  alongside `WORKER_CYCLE_FAILED`; if a cycle fails before data admission,
  its durable projection uses `DATA_NOT_EVALUATED` rather than an ambiguous
  placeholder reason.
  A pipeline-reported `FAILED` outcome remains `HEALTH=FAILED` and is retried
  on the next occurrence rather than being relabeled as a safety veto.
  Lease acquisition, heartbeats, status, and cycle persistence reject
  timestamps that are not already represented in UTC, preserving the
  system-wide time boundary instead of silently assuming a timezone.
  SQLite schema initialization serializes concurrent DDL behind an immediate
  writer transaction, so status probes and worker startups cannot race on
  `CREATE TABLE` during local process supervision.
  SQLite lease acquisition, lease-fenced status writes, and stop-control writes
  also use an immediate coordination transaction because SQLite does not honor
  row-level `FOR UPDATE` locks; this closes the read-then-write race while
  retaining the existing PostgreSQL row-lock path.
  Lease-owned status writes preserve a concurrent operator stop request while
  an intentional restart may explicitly clear the prior stop flag, closing the
  supervisor's stop-control read-modify-write race.
  PostgreSQL startup verifies the complete numbered-migration table, column,
  and required non-null constraint set and fails closed when it is incomplete;
  it never uses SQLAlchemy `create_all()` as a production schema deployment
  path.
  Persistence initialization failures are rendered as a concise CLI failure
  rather than an uncaught database traceback.
  Decision identity also binds the event-time system-health state, allowing a
  transient health veto to be reevaluated after recovery without replaying an
  identical health state.
  Status reports `EXECUTION=UNSUPPORTED` when the paper pipeline dependency is
  absent, rather than implying that an execution path is available.
  Account-scoped lease status also records the claimed workload, so a stale
  workload row is reported as `UNKNOWN`/`FAILED` when another workload owns the
  account lease.
  Status now persists and renders `DATA_LATEST` plus measured
  `DATA_FRESHNESS`, while unavailable measurements remain `UNKNOWN`; existing
  worker SQLite stores receive the additive column on startup.
  Automatic daily-loss and drawdown lock activation now atomically cancels
  open risk-increasing paper orders and releases their reservations; explicitly
  authorized reduction-only orders remain eligible.
  The global kill switch is a fail-closed gate before persistence, reconciliation,
  data, strategy, or execution activity.
  Operator stop requests update only the durable control fields under a row lock,
  preserving a concurrent worker heartbeat, decision trace, and cycle projection.
  The read-only status command also supports `--json` for machine consumers of
  the same durable state and trace. The `ready` command renders the same
  projection but exits `0` only for a running, healthy PAPER worker without a
  stop request, with explicitly healthy data admission and reconciliation and
  no persisted error; blocked, failed, stopped, or unknown states exit `2`.
  Pending paper orders are processed on qualified quotes before new strategy
  resolution, but only with a current healthy system-health snapshot; disabled
  strategies no longer strand already-approved pending orders, and unhealthy
  safety state never fills them.
  Each evaluated runtime strategy now contributes a deterministic
  `strategy_decision` event to the durable cycle trace, preserving its signal
  direction, reason, and intent count without granting it execution authority.
  Explicit `fixture` and `synthetic` labels in the worker source, dataset
  version, provider, or configuration identity are rejected as
  `DATA_FIXTURE_NOT_OPERATIONAL`; synthetic fixtures remain available only to
  isolated lower-level software tests.
  Governance now vetoes operational eligibility for an explicitly persisted
  `StrategyHealth.FAILED` state; no automatic health thresholds or self-
  disablement policy is introduced.
  Worker-store timestamp reads now normalize aware non-UTC database values to
  UTC as well as handling naive legacy SQLite values, so status and lease
  projections preserve the system-wide UTC boundary.
  Worker configuration rejects duplicate `(strategy_id, strategy_version)`
  entries before lease acquisition, preventing ambiguous duplicate strategy
  evaluation while preserving the existing fusion duplicate protection.
  The public paper decision pipeline applies the same duplicate-key veto for
  direct callers, after pending-order management, so already-approved orders
  are not stranded while no new strategy decision can pass.
  Completed-decision identity now includes the effective risk-firewall policy
  and fusion-policy configuration identities, so a governed policy change
  reevaluates the same market event instead of replaying an older decision.
  It also binds the regime-detector configuration and registered runtime code
  identities, so deployment changes cannot silently replay an older decision
  past the current runtime-binding checks.
  The direct decision pipeline rejects duplicate bar timestamps for the same
  instrument/timeframe as `DATA_DUPLICATE_TIMESTAMP` before pending-order,
  feature, risk, or paper execution activity.
  This protects direct application callers in addition to the persisted-data
  identity and qualification gates used by the supervisor.
  It also rejects mixed instrument/timeframe/source/adjustment-policy inputs as
  `DATA_SCOPE_MISMATCH` before any downstream activity.
  Qualification records checked in the future relative to the cycle clock are
  rejected as `DATA_QUALIFICATION_FUTURE_DATED`; no qualification TTL is
  inferred without an approved policy.
  The direct pipeline also rejects a latest bar with future-dated ingestion as
  `DATA_INGESTION_FUTURE_DATED`, matching the supervisor's existing admission
  invariant.
  It now applies the same availability check to every historical bar in the
  decision window, rejecting future ingestion and quote timestamps before
  features, risk, or paper execution can consume them.
  Startup lease/status persistence failures now release any acquired lease and
  surface as concise `FAILED` control results instead of leaving a startup
  lease behind or emitting an uncaught SQLAlchemy traceback.
  Typed regime, strategy, and fusion failures now persist explicit failed stage
  trace events and `NO_TRADE` outcomes instead of losing the failing stage in
  a generic worker-cycle exception projection.
  Risk-firewall trace events now include the deterministic decision identity,
  policy/configuration identity, serialized check results, and any bounded
  authorization limits in the existing cycle trace.
  Research walk-forward aggregation now also rejects reversed or overlapping
  forward-test periods even when fold indices are numerically ordered.
  Locked-OOS access recording now requires a registered developmental parent
  experiment whose research-defining lineage matches the OOS specification;
  arbitrary, missing, OOS, or incompatible parent identities are rejected.
- Dataset admission now pins the latest bar and execution window to the
  qualified dataset's raw/adjusted policy, rejects mixed-policy latest bars,
  and includes that policy in the completed-decision identity.
- Strategy evidence and approval projections now use deterministic identity
  tie-breakers after timestamp ordering, so equal-time governance records
  cannot depend on database row order.
- Lease heartbeats now reject expired owners before renewal, and lease validity
  requires the requested workload to remain the durable claimant, preventing a
  stale worker or status probe from treating a replacement lease as its own.
- Account-scoped lease mutations now require both the owner and the claimed
  workload identity for status writes, heartbeats, cycle appends, and release;
  a reused owner identity cannot mutate or delete a replacement workload's
  lease.
- Active account lease acquisition also rejects a different workload even when
  the caller reuses the same owner identity; only the same owner/workload pair
  may renew an unexpired claim.
- Lease reclamation resets the durable `acquired_at` timestamp for a new owner
  while same-claim renewal preserves the original acquisition time.
- Cycle persistence rejects reversed completion intervals, preserving
  chronological restart history and bounded strategy-health ordering.
- Quality-error admission is scoped to the configured data source; source-less
  global errors still block, while unrelated-provider errors cannot veto an
  otherwise qualified series.
- Quality-error admission now applies the same global-field and decision-window
  boundaries as the pipeline: omitted source/symbol/timeframe fields veto the
  matching current series, while future-occurring or future-bar findings do not
  affect an earlier decision.
- The pipeline now applies the same source/instrument/timeframe boundary when
  forwarding quality events into regime evaluation, preventing unrelated
  persisted findings from changing the active decision.
- Paper reconciliation now verifies the complete latest risk snapshot against
  authoritative account, position, order, reservation, and lock projections;
  tampered cash, equity, exposure, marks, or pending-state snapshots fail
  closed before further risk evaluation.
- Paper reconciliation also verifies each active reservation's order, intent,
  and risk-decision identities against the open-order projection, preventing
  authorization mismatches from surviving a restart.
- Paper order transitions now preserve every immutable order-shape and
  authorization identity, including idempotency, policy, sizing, and action;
  only fill/state fields may change during durable execution transitions.
- The read-only `ready` predicate now independently requires healthy data
  admission and reconciliation, plus no persisted worker error, so an
  inconsistent healthy status projection cannot advertise operational readiness.
- `start` and `run-once` now fail closed on inconsistent persisted projections:
  exit `0` requires PAPER mode with no persisted error, and a blocked result
  must also be an explicit `NO_TRADE` decision.
- Data-admission validation now catches only the existing typed
  `DataValidationError`; market-store or persistence failures remain explicit
  supervisor failures instead of being mislabeled as invalid market data.
- Worker instances now expose an idempotent `close()` resource boundary;
  construction failures dispose partially initialized engines, and every CLI
  command disposes its worker in `finally` after rendering or execution.
- Tested: focused worker/pipeline/lifecycle/monitoring coverage plus a real
  subprocess lifecycle test for `start`, `status`, `ready`, and `stop`; full
  suite `532 passed, 12 skipped` locally; full Ruff; and full mypy (`115 source
  files`). The disposable PostgreSQL migration/concurrency verification also
  passed `13 tests` against PostgreSQL 16. The
  CI and manual PostgreSQL verification paths now share one explicit ordered
  migration runner; its missing-DSN guard was exercised locally and made no
  database connection.
  configured-worker fixture also covers SQLite UTC quote round-trip recovery,
  content-hash verification, real governance rejection, and the actionable
  restart replay boundary. Worker lease-loss tests cover fail-closed pipeline
  exclusion, post-pipeline shutdown, lease-conditional status writes, replacement-
  owner status preservation, lease-fenced cycle persistence, structured
  strategy-health aggregation, and machine-readable CLI rendering;
  worker data tests cover
  both timestamp and same-timestamp content changes; reconciliation tests cover
  failure after a prior healthy cycle; timing tests cover slow-cycle freshness
  behavior; account-scoped lease tests cover different workload labels.
  Failed-cycle recovery tests verify a retry is not mislabeled as a duplicate
  replay, while completed healthy or blocked decisions remain idempotent.
  Failure-code tests cover persistence and reconciliation paths; status tests
  cover freshness rendering, legacy-schema upgrade, lock-state-sensitive
  decision replay, system-health recovery reevaluation, explicit
  unsupported-execution status rendering, and explicit reconciliation-failure
  classification. Account-scoped status tests cover stale workload claims.
  Paper-engine lock tests cover automatic loss-lock cancellation and reservation
  release while preserving reduction-only semantics. The global kill-switch test
  covers downstream-stage suppression and the no-order invariant. Atomic stop-
  control tests cover projection preservation and unknown-workload initialization;
  status tests cover the machine-readable JSON projection and trace. The
  synthetic pipeline test covers one pending fill after strategy disablement,
  while the health gate prevents fills for unavailable or stale safety state;
  the worker gate test confirms pending management remains reachable with zero
  newly eligible strategies. Pipeline trace coverage verifies the per-strategy
  signal decision is durable alongside the risk and paper stages.
  The system test launches the actual module entry point against isolated
  SQLite with a paper account but no dataset, observes the bounded
  `DATA_NOT_CONFIGURED`/no-trade worker, verifies `ready` is rejected, rejects
  a concurrent second start, sends the durable stop request, confirms clean
  process exit, and restarts the same workload for two bounded cycles with its
  durable no-trade state intact. A second system exercise terminates a worker
  process, waits for its short lease to expire, and confirms a new process
  reclaims the workload without orders or fills. A third system exercise sends
  SIGTERM to a running worker and confirms clean `STOPPED` exit with no orders
  or fills. A fourth system exercise launches two workers simultaneously for
  the same paper account and confirms exactly one lease holder while the other
  exits `BLOCKED`.
  Configured-worker coverage verifies explicit fixture labels cannot reach the
  pipeline, including metadata labels when the storage source itself is local.
  Adjustment-policy coverage rejects a latest adjusted bar when the admitted
  dataset is raw; the focused selection passed `3 tests in 0.54s`.
- Exercised: isolated SQLite `run-once` and a bounded three-cycle `start`
  behavior;
  the completed status showed `STOPPED`, `HEALTH=BLOCKED`, `MODE=PAPER`,
  `DATA=BLOCKED`,
  `DATA_LATEST=UNKNOWN`, `DATA_FRESHNESS=UNKNOWN`,
  `ELIGIBLE_STRATEGIES=0`, `ACTION=NO_TRADE`, with zero paper orders/fills;
  the bounded worker ended `STOPPED` cleanly. A live-mode invocation was
  rejected. A fresh two-cycle lease-smoke run also ended `STOPPED` with the
  same blocked/no-trade status. A fresh post-fence three-cycle CLI run
  persisted exactly three `NO_TRADE`/`BLOCKED` cycle rows and zero orders or
  fills. Its read-only JSON `strategy-health` query returned the empty set,
  because no strategy decision stage was reached; with a two-cycle limit it
  reported `cycles_considered=2`, `structured_decision_count=0`, and
  `unattributed_decision_count=0`, and `truncated=true`.
  A fresh two-cycle admission-recheck smoke run
  did the same. A fresh two-cycle reconciliation smoke run also ended
  `STOPPED` with no orders or fills. The synthetic fixture
  account-scope exercise observed `RUNNING`/`HEALTH=FAILED` with an absent
  paper account, rejected a second workload targeting that account with
  `BLOCKED`, and stopped the first worker cleanly. The synthetic fixture
  pipeline reached the existing firewall and paper engine once, then rejected
  the repeated intent without creating a second order. The full synthetic
  worker fixture also routed a persisted, content-verified dataset through the
  real supervisor, which rejected the fixture-labelled dataset before pipeline
  entry and created no order. The isolated direct pipeline fixture still
  reached the existing firewall and paper engine, submitted one order, and a
  later synthetic bar filled that pending order exactly once. These fixtures
  create no production approval or empirical qualification.
  Dataset metadata that does not cover the latest complete bar is rejected
  before activity. The latest CLI smoke started an actual worker, observed
  `RUNNING` with the same blocked/no-trade state, accepted `stop`, exited
  `STOPPED` with `STOP_REQUESTED=true`, and left no `traderos.worker` process.
  A fresh long-lived CLI worker rejected a concurrent second start with
  `BLOCKED`/exit 2, then stopped cleanly; a simultaneous two-process start
  also produced exactly one lease holder and one `BLOCKED`/exit-2 process;
  restarting the same SQLite workload
  for two bounded cycles preserved the durable blocked/no-trade state. Focused
  worker tests cover restart reconciliation and completed-decision replay,
  strategy disablement, risk locks, duplicate-event protection, and the
  account/workload lease boundary. The `ready` probe test confirms blocked
  state returns exit 2; the actual no-data CLI probe also returned exit 2,
  while the predicate requires healthy running PAPER state and no stop request.
  Qualification-boundary tests cover missing qualification, missing and
  mismatched dataset identity, content-hash mismatch, rejected, and
  matching non-fixture records; the matching record is a temporary software
  test double and is not empirical evidence or an operational approval.
  A temporary isolated SQLite application exercise persisted both
  `DATA_QUALIFICATION_DATASET_ID_NOT_CONFIGURED` and
  `DATA_QUALIFICATION_DATASET_ID_MISMATCH` as `NO_TRADE` with zero orders.
  A direct
  `TRADING_MODE=live LIVE_TRADING_ENABLED=true ... run-once` invocation
  exited 1 with the explicit PAPER-only rejection.
  The machine-readable CLI status exercise emitted one JSON object containing
  the persisted `STOPPED` state, `NO_TRADE` reason codes, counts, and decision
  trace.
  In the latest bounded application exercise,
  `TRADING_MODE=paper DATABASE_URL=sqlite:////tmp/traderos-paper-worker-iter.Lkmo20/worker.db
  WORKER_WORKLOAD_ID=bounded-iter WORKER_DATA_SOURCE=unqualified-local
  ./.venv/bin/python -m traderos.worker --max-cycles 3 --interval 0.01 start`
  exited `STOPPED` with `3` durable `NO_TRADE`/`BLOCKED` cycles,
  `DATA_NOT_CONFIGURED`, and zero orders/fills; the subsequent process check
  found no worker process. The latest long-lived lifecycle exercise observed
  `RUNNING`/`HEALTH=BLOCKED`, `ready` exit `2`, concurrent-start exit `2`,
  `STOP_REQUESTED`, and a clean `STOPPED` exit with no worker process left.
  The corrected focused safety/fixture selection passed `13 tests in 1.21s`;
  one earlier selection named two removed tests and ran zero tests, then was
  corrected before any result was counted.
  The current exit-contract regression selection passed `2 tests`; the current
  CLI lifecycle module passed `4 tests in 8.42s`, the focused restart/lease,
  disablement, lock, and replay selection passed `8 tests`, and the focused
  fixture pipeline selection passed `2 tests`. The qualification-boundary
  selection passed `7 tests`, and corrupted restart reconciliation passed `1
  test` (with the existing SQLite datetime-adapter warning). The strategy-health
  selection passed `4 tests`; an isolated SQLite projection exercise showed
  `CYCLES_CONSIDERED=2`, `REPLAYED_CYCLES=1`, `STRUCTURED_DECISIONS=1`, and one
  alpha decision after recording one original and one replayed cycle. A fresh
  bounded CLI run emitted JSON with `replayed_cycle_count=1`, two durable
  cycles, `DATA_NOT_CONFIGURED`, `NO_TRADE`, and zero paper orders/fills.
  Admission fault-injection coverage passed `3 tests`, distinguishing typed
  invalid-bar blocking from a failed market-store dependency.
  A coverage-only worker slice passed `105 tests`; it still emits test-harness
  SQLite `ResourceWarning`s from direct unit helpers that do not call `close()`,
  while normal suite warnings remain limited to the existing two SQLite
  datetime-adapter deprecations. Engine-cleanup regression coverage passed
  `2 tests`.
  Typed failure-trace coverage passed `4 tests in 0.74s`, and the full pipeline
  integration module passed `17 tests in 1.33s`.
  Risk-trace approval and veto coverage passed `2 tests in 0.65s`.
  Research service/orchestration coverage passed `5 tests in 0.35s` after the
  chronological test-window guard was added.
  Research registry coverage passed `17 tests in 0.67s` with the locked-OOS
  parent-lineage guard.
- Open gap: this is a software slice only. Genuine market-data rights and
  qualification remain unresolved. Local ignored market artifacts exist, but
  the recorded qualification is rejected (`license_reference` and
  `source_version` are blocked), and no production account/strategy has been
  authorized for autonomous paper operation. Automatic strategy-health
  de-risking thresholds are not implemented; introducing those policy
  thresholds requires explicit review, so governance disablement remains the
  current de-risking authority.
- External blocker: genuine market-data rights/qualification remain unresolved;
  no network acquisition was attempted.
- Next executable task: validate a genuinely rights-qualified local dataset
  and a separately approved strategy artifact against this content-identity
  gate; until then, operational paper execution remains externally blocked.
