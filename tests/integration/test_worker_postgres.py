"""PostgreSQL persistence coverage for the fail-closed PAPER supervisor.

This is operational schema/application coverage only. It intentionally has no
market data, strategy approval, broker connection, or live capability.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text

from traderos.core.config import Settings, TradingMode
from traderos.worker import PaperWorker, WorkerHealth, WorkerState

POSTGRES_URL_ENV = "TRADEROS_POSTGRES_TEST_URL"


@pytest.fixture
def postgres_url() -> str:
    url = os.environ.get(POSTGRES_URL_ENV)
    if not url:
        pytest.skip(f"{POSTGRES_URL_ENV} is required for disposable PostgreSQL verification")
    return url


@pytest.mark.integration
def test_postgres_worker_persists_fail_closed_no_trade_cycle(postgres_url: str) -> None:
    workload_id = f"postgres-worker-{uuid4().hex}"
    settings = Settings(
        _env_file=None,
        trading_mode=TradingMode.PAPER,
        database_url=postgres_url,
        worker_workload_id=workload_id,
        worker_cycle_interval_seconds=0.01,
    )
    worker = PaperWorker.from_settings(settings)
    try:
        with worker.store.engine.connect() as connection:
            required_columns = {
                "last_trace",
                "data_latest_at",
            }
            state_columns = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = 'worker_states'"
                    )
                )
            }
            assert required_columns <= state_columns
            claimant_nullable = connection.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'worker_leases' "
                    "AND column_name = 'claimed_workload_id'"
                )
            ).scalar_one()
            assert claimant_nullable == "NO"

        status = worker.run_once()
        persisted = worker.store.status(workload_id)

        assert status.worker_state is WorkerState.STOPPED
        assert status.health is WorkerHealth.BLOCKED
        assert status.data_reason == "DATA_NOT_CONFIGURED"
        assert status.last_decision == "NO_TRADE"
        assert status.paper_order_count == 0
        assert status.paper_fill_count == 0
        assert persisted.last_reason_codes == status.last_reason_codes
        assert persisted.last_trace == status.last_trace
        assert worker.store.cycle_count(workload_id) == 1
    finally:
        worker.store.engine.dispose()
