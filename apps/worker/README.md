# Worker application

The repository’s PAPER supervisor is launched through the package module:

```bash
TRADING_MODE=paper DATABASE_URL=sqlite:////tmp/traderos-worker.db \
  python -m traderos.worker start
```

It is a local, restartable, fail-closed loop. It does not ingest data over the
network, connect to a broker, enable live execution, or run as an automatically
installed background service. Configure a verified, non-fixture dataset
qualification and governance-approved strategy before expecting anything
beyond durable `NO_TRADE` status. See `docs/PAPER_WORKER.md` for the complete
configuration, lifecycle controls, read-only `strategy-health` counters, and
verification record.
