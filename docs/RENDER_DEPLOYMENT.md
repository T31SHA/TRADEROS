# First Render deployment (blocked PAPER worker)

**FIRST EMPTY-DATABASE DEPLOYMENT SUPPORTED.**
**EXISTING-DATABASE UPGRADE NOT YET SUPPORTED.**

This procedure deploys one TRADEROS PAPER worker on Render against a new,
empty PostgreSQL 16 database. The global kill switch is active, and no broker
credentials or operational strategy, data, or allocation bindings are set. The
worker records a `NO_TRADE` / `KILL_SWITCH_ACTIVE` decision each cycle and
creates no paper orders or fills.

Initializing the schema is a separate operator step that you run once. It is
not part of the build, the start command, or a Render pre-deploy command.
Render offers a pre-deploy command on paid services, and TRADEROS deliberately
leaves it empty because Render runs it on every deploy. A worker restart never
initializes or migrates the schema. On PostgreSQL the worker only checks that
migrations 001–013 are present, and it fails closed when they are missing.

## 1. Resources

- **Render Postgres:** a new database with PostgreSQL major version **16**.
  Don't reuse an existing database. Restrict external access with the
  database's IP allowlist to the operator workstation that runs section 3.
- **Render Background Worker:** one service with the Python runtime and
  exactly **1** instance (no autoscaling). Set Auto-Deploy to **Off**.

| Setting | Value |
| --- | --- |
| Runtime | Python |
| Build command | `pip install .` |
| Start command | `traderos-worker start` |
| Pre-deploy command | *(empty)* |
| Instances | 1 |
| Auto-Deploy | Off |

## 2. Environment variables (Background Worker)

These are the `traderos.core.config.Settings` names (case-insensitive) plus
Render's `PYTHON_VERSION`.

| Variable | Value |
| --- | --- |
| `PYTHON_VERSION` | `3.12.14` |
| `TRADING_MODE` | `paper` |
| `LIVE_TRADING_ENABLED` | `false` |
| `KILL_SWITCH_ACTIVE` | `true` |
| `DATABASE_URL` | Internal Database URL, rewritten as shown below (secret) |
| `WORKER_WORKLOAD_ID` | `paper-primary` |
| `WORKER_CYCLE_INTERVAL_SECONDS` | `60` |
| `WORKER_LEASE_SECONDS` | `120` |
| `ENVIRONMENT` | `render-paper` |
| `LOG_LEVEL` | `INFO` |
| `TIMEZONE` | `UTC` |

`PYTHON_VERSION` needs a full patch version. Rehearsals used 3.12.14. If
Render reports that version as unavailable, use the newest 3.12.x it offers,
and never 3.13 or later.

Render shows database URLs as `postgres://USER:PASSWORD@HOST:5432/DB`.
SQLAlchemy rejects that scheme and the worker exits with `FAILED` (code 1). Set
`DATABASE_URL` to the same URL with the scheme changed to `postgresql+psycopg://`:

```text
postgresql+psycopg://USER:PASSWORD@INTERNAL_HOST:5432/DB
```

**Don't set** `WORKER_ACCOUNT_ID`, `WORKER_INSTRUMENT`, `WORKER_TIMEFRAME`,
`WORKER_DATASET_VERSION`, `WORKER_DATASET_ID`,
`WORKER_DATASET_QUALIFICATION_ID`, `WORKER_DATASET_QUALIFICATION_ROOT`,
`WORKER_STRATEGY_VERSIONS`, `BROKER_API_KEY`, `BROKER_API_SECRET`, or
`MARKET_DATA_API_KEY`. `REDIS_URL` is unused.

## 3. Fresh-database initialization (once, before the first start)

Run this from an operator workstation with a checkout of the release commit.

Required tooling: `bash` and a PostgreSQL **16 or newer** `psql` client. If
you use `pg_dump` for backups, it must also be version 16 or newer. If you
don't have these installed, the `postgres:16` container image provides all
three (see below).

```bash
# External Database URL from the Render dashboard; postgres:// is fine for psql.
export TRADEROS_POSTGRES_TEST_DSN='postgres://USER:PASSWORD@EXTERNAL_HOST:5432/DB?sslmode=require'

# 1. Confirm that the database is empty. The expected output is 0.
psql "$TRADEROS_POSTGRES_TEST_DSN" -Atc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"

# 2. Apply migrations 001–013 in order. The runner refuses to start without
#    TRADEROS_POSTGRES_FRESH=1 and refuses a database with any public table.
TRADEROS_POSTGRES_FRESH=1 scripts/apply_postgres_migrations.sh

# 3. Confirm the schema. The expected output is 24.
psql "$TRADEROS_POSTGRES_TEST_DSN" -Atc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
```

Without a local client, use the container image instead:

```bash
docker run --rm --network host -v "$PWD":/repo:ro -w /repo \
  -e TRADEROS_POSTGRES_TEST_DSN -e TRADEROS_POSTGRES_FRESH=1 \
  postgres:16 scripts/apply_postgres_migrations.sh
```

If a migration fails partway through, the database is no longer empty and the
runner won't run against it again. Delete that Render database, create a new
empty PostgreSQL 16 database, and repeat this section. Never run this section
against a database that holds data, and never run it on a restart.

After the schema is in place, create or resume the Background Worker.

## 4. Inspecting heartbeat and status

The worker writes its heartbeat and last decision to PostgreSQL. It prints its
final status to the Render logs only when it stops. You can read status in two
ways:

- **Render Shell** (paid instances only; the shell uses the service's
  environment):

  ```bash
  traderos-worker status --json
  ```

- **Operator workstation** (Python 3.12 environment with `pip install .`):

  ```bash
  TRADING_MODE=paper KILL_SWITCH_ACTIVE=true WORKER_WORKLOAD_ID=paper-primary \
  DATABASE_URL='postgresql+psycopg://USER:PASSWORD@EXTERNAL_HOST:5432/DB?sslmode=require' \
    traderos-worker status
  ```

`status` is read-only. Expected values for this deployment:

- `RUNNING`, `HEALTH=BLOCKED`, `MODE=PAPER`
- `REASON_CODES=KILL_SWITCH_ACTIVE`, with `DUPLICATE_DECISION` added on
  repeated identical cycles
- `DECISION_TRACE` beginning `global_gate:BLOCKED:KILL_SWITCH_ACTIVE`
- `PAPER_ORDERS=0`, `PAPER_FILLS=0`
- `HEARTBEAT` no older than about `WORKER_CYCLE_INTERVAL_SECONDS`

`traderos-worker ready` exits with code 2 while the kill switch blocks trading.
That exit code is expected here and is not a fault.

To confirm there are no financial effects:

```bash
psql "$TRADEROS_POSTGRES_TEST_DSN" -Atc \
  "SELECT (SELECT count(*) FROM paper_orders), (SELECT count(*) FROM paper_fills)"
# expected: 0|0
```

## 5. Stopping and restarting safely

- **Stop:** In the Render dashboard, **Suspend** the worker. Render sends
  `SIGTERM`. The worker completes its safe boundary, records `STOPPED`,
  releases its lease, and exits with code 0 well inside Render's default
  30-second shutdown delay (the rehearsal took under 1 second).
  `traderos-worker stop` isn't a durable stop on Render. It ends the loop, but
  Render restarts the exited process, and `start` clears the stop request.
- **Restart:** **Resume** the worker. Don't rerun section 3. The worker reuses
  its durable state: cycle history, decision identity, and status.
- **Exclusive ownership:** Only one process can hold the `paper-primary`
  lease. A second process exits with
  `BLOCKED: … already has an active worker` (code 2). After a `SIGKILL`, the
  lease expires after `WORKER_LEASE_SECONDS`, and until then new starts are
  refused. Don't raise the instance count to work around this.
- **Code deploys:** Render starts the new instance before it sends `SIGTERM`
  to the old one. The new instance can't take the lease while the old one
  holds it. For this release, deploy only the first release commit while the
  service is new or suspended, and treat later deploys as out of scope.

## 6. Backup and restore

Suspend the worker first so that the backup is quiescent. Then run:

```bash
pg_dump --format=custom --no-owner --no-privileges \
  --file=traderos.dump "$TRADEROS_POSTGRES_TEST_DSN"
```

Restore only into a new, empty PostgreSQL 16 database. Don't run section 3
against it, because the dump already contains the schema:

```bash
pg_restore --no-owner --no-privileges --exit-on-error --single-transaction \
  --dbname="$RESTORE_DSN" traderos.dump
```

Before you start a worker against the restored database, keep the original
worker suspended and point `DATABASE_URL` at the restored database. Only one
worker may run for `paper-primary` at any time. Worker leases live inside each
database, so they can't enforce ownership across two databases.
After a restore, CHECK constraints may deparse to different text in
`pg_get_constraintdef` (for example, `ARRAY[...]::text[]` becomes
`ARRAY[(...)::text]`). The two forms are semantically identical.
