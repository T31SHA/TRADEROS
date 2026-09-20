"""System coverage for the real PAPER worker command lifecycle."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from traderos.database.connection import create_database_engine
from traderos.paper.engine import PaperTradingEngine
from traderos.paper.store import SqlAlchemyPaperStore
from traderos.risk import RiskFirewall


def _create_paper_account(database_url: str, account_id: str) -> None:
    engine = create_database_engine(database_url)
    try:
        store = SqlAlchemyPaperStore(engine)
        store.create_schema_for_testing()
        firewall = RiskFirewall()
        PaperTradingEngine(store).create_account(
            account_id=account_id,
            account_currency="USD",
            starting_cash=Decimal("10000"),
            risk_capacity=Decimal("10000"),
            daily_loss_limit=firewall.parameters.daily_loss_limit,
            max_drawdown=firewall.parameters.max_drawdown,
            risk_policy_id=firewall.policy_id,
            risk_policy_configuration_id=firewall.configuration_id,
            timestamp=datetime.now(UTC),
        )
    finally:
        engine.dispose()

@pytest.mark.system
def test_worker_cli_start_status_ready_stop(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'worker.db'}"
    _create_paper_account(database_url, "paper-account")
    environment = os.environ.copy()
    environment["TRADING_MODE"] = "paper"
    environment["WORKER_ACCOUNT_ID"] = "paper-account"
    command_prefix = [
        sys.executable,
        "-m",
        "traderos.worker",
    ]
    process = subprocess.Popen(
        [
            *command_prefix,
            "start",
            "--database-url",
            database_url,
            "--interval",
            "0.05",
        ],
        cwd=Path(__file__).parents[2],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        deadline = time.monotonic() + 5.0
        status_output = ""
        while time.monotonic() < deadline:
            status = subprocess.run(
                [*command_prefix, "status", "--database-url", database_url],
                cwd=Path(__file__).parents[2],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            status_output = status.stdout
            if "RUNNING" in status_output:
                break
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout is not None else ""
                pytest.fail(f"worker exited before becoming RUNNING: {output}")
            time.sleep(0.05)
        else:
            pytest.fail(f"worker did not become RUNNING: {status_output}")

        assert "HEALTH=BLOCKED" in status_output
        assert "DATA=BLOCKED" in status_output
        assert "DATA_REASON=DATA_NOT_CONFIGURED" in status_output
        assert "RECONCILIATION=HEALTHY" in status_output
        assert "ACTION=NO_TRADE" in status_output

        ready = subprocess.run(
            [*command_prefix, "ready", "--database-url", database_url],
            cwd=Path(__file__).parents[2],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert ready.returncode == 2
        assert "HEALTH=BLOCKED" in ready.stdout

        contender = subprocess.run(
            [
                *command_prefix,
                "start",
                "--database-url",
                database_url,
                "--interval",
                "0.05",
            ],
            cwd=Path(__file__).parents[2],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert contender.returncode == 2
        assert "BLOCKED:" in contender.stdout

        stop = subprocess.run(
            [*command_prefix, "stop", "--database-url", database_url],
            cwd=Path(__file__).parents[2],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert stop.returncode == 0
        assert "STOP_REQUESTED workload=default" in stop.stdout

        assert process.wait(timeout=5) == 0
        output = process.stdout.read() if process.stdout is not None else ""
        assert "STOPPED" in output
        assert "STOP_REQUESTED=true" in output

        final_status = subprocess.run(
            [*command_prefix, "status", "--database-url", database_url],
            cwd=Path(__file__).parents[2],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert final_status.returncode == 0
        assert final_status.stdout.splitlines()[0] == "STOPPED"
        assert "PAPER_ORDERS=0" in final_status.stdout
        assert "PAPER_FILLS=0" in final_status.stdout

        restart = subprocess.run(
            [
                *command_prefix,
                "start",
                "--database-url",
                database_url,
                "--interval",
                "0.01",
                "--max-cycles",
                "2",
            ],
            cwd=Path(__file__).parents[2],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert restart.returncode == 0
        assert "STOPPED" in restart.stdout
        assert "HEALTH=BLOCKED" in restart.stdout
        assert "DATA_REASON=DATA_NOT_CONFIGURED" in restart.stdout
        assert "ACTION=NO_TRADE" in restart.stdout

        restarted_status = subprocess.run(
            [*command_prefix, "status", "--database-url", database_url],
            cwd=Path(__file__).parents[2],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert restarted_status.returncode == 0
        assert restarted_status.stdout.splitlines()[0] == "STOPPED"
        assert "STOP_REQUESTED=false" in restarted_status.stdout
        assert "DATA_REASON=DATA_NOT_CONFIGURED" in restarted_status.stdout
        assert "PAPER_ORDERS=0" in restarted_status.stdout
        assert "PAPER_FILLS=0" in restarted_status.stdout

        strategy_health = subprocess.run(
            [
                *command_prefix,
                "strategy-health",
                "--database-url",
                database_url,
                "--json",
            ],
            cwd=Path(__file__).parents[2],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert strategy_health.returncode == 0
        strategy_health_payload = json.loads(strategy_health.stdout)
        assert strategy_health_payload["workload_id"] == "default"
        assert strategy_health_payload["cycle_limit"] == 1000
        assert strategy_health_payload["cycles_considered"] >= 1
        assert strategy_health_payload["truncated"] is False
        assert strategy_health_payload["structured_decision_count"] == 0
        assert strategy_health_payload["unattributed_decision_count"] == 0
        assert strategy_health_payload["strategies"] == []
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


@pytest.mark.system
def test_worker_cli_reclaims_expired_lease_after_process_exit(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'crashed-worker.db'}"
    environment = os.environ.copy()
    environment["TRADING_MODE"] = "paper"
    environment["WORKER_LEASE_SECONDS"] = "0.2"
    command_prefix = [sys.executable, "-m", "traderos.worker"]
    process = subprocess.Popen(
        [
            *command_prefix,
            "start",
            "--database-url",
            database_url,
            "--interval",
            "0.01",
        ],
        cwd=Path(__file__).parents[2],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            status = subprocess.run(
                [*command_prefix, "status", "--database-url", database_url],
                cwd=Path(__file__).parents[2],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            if "RUNNING" in status.stdout:
                break
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout is not None else ""
                pytest.fail(f"worker exited before becoming RUNNING: {output}")
            time.sleep(0.05)
        else:
            pytest.fail("worker did not become RUNNING before crash exercise")

        process.kill()
        process.wait(timeout=5)
        time.sleep(0.35)

        restart = subprocess.run(
            [
                *command_prefix,
                "start",
                "--database-url",
                database_url,
                "--interval",
                "0.01",
                "--max-cycles",
                "2",
            ],
            cwd=Path(__file__).parents[2],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert restart.returncode == 0
        assert "STOPPED" in restart.stdout
        assert "HEALTH=BLOCKED" in restart.stdout
        assert "ACTION=NO_TRADE" in restart.stdout
        assert "PAPER_ORDERS=0" in restart.stdout
        assert "PAPER_FILLS=0" in restart.stdout
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


@pytest.mark.system
def test_worker_cli_sigterm_stops_cleanly(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'sigterm-worker.db'}"
    environment = os.environ.copy()
    environment["TRADING_MODE"] = "paper"
    command_prefix = [sys.executable, "-m", "traderos.worker"]
    process = subprocess.Popen(
        [
            *command_prefix,
            "start",
            "--database-url",
            database_url,
            "--interval",
            "0.05",
        ],
        cwd=Path(__file__).parents[2],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            status = subprocess.run(
                [*command_prefix, "status", "--database-url", database_url],
                cwd=Path(__file__).parents[2],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            if "RUNNING" in status.stdout:
                break
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout is not None else ""
                pytest.fail(f"worker exited before becoming RUNNING: {output}")
            time.sleep(0.05)
        else:
            pytest.fail("worker did not become RUNNING before SIGTERM")

        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=5) == 0
        output = process.stdout.read() if process.stdout is not None else ""
        assert "STOPPED" in output
        assert "STOP_REQUESTED=false" in output
        assert "PAPER_ORDERS=0" in output
        assert "PAPER_FILLS=0" in output
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


@pytest.mark.system
def test_worker_cli_simultaneous_starts_allow_one_lease_holder(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'simultaneous-worker.db'}"
    _create_paper_account(database_url, "paper-account")
    environment = os.environ.copy()
    environment["TRADING_MODE"] = "paper"
    environment["WORKER_ACCOUNT_ID"] = "paper-account"
    command_prefix = [sys.executable, "-m", "traderos.worker"]
    start_command = [
        *command_prefix,
        "start",
        "--database-url",
        database_url,
        "--interval",
        "0.05",
    ]
    processes = [
        subprocess.Popen(
            start_command,
            cwd=Path(__file__).parents[2],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(2)
    ]

    try:
        deadline = time.monotonic() + 5.0
        loser: subprocess.Popen[str] | None = None
        winner: subprocess.Popen[str] | None = None
        while time.monotonic() < deadline:
            exited = [process for process in processes if process.poll() is not None]
            if exited:
                loser = exited[0]
                winner = processes[1] if loser is processes[0] else processes[0]
                break
            time.sleep(0.02)

        assert loser is not None, "neither simultaneous worker reported lease contention"
        assert winner is not None
        loser_output, _ = loser.communicate(timeout=5)
        assert loser.returncode == 2
        assert "BLOCKED:" in loser_output
        assert winner.poll() is None

        stop = subprocess.run(
            [*command_prefix, "stop", "--database-url", database_url],
            cwd=Path(__file__).parents[2],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert stop.returncode == 0
        assert "STOP_REQUESTED workload=default" in stop.stdout
        assert winner.wait(timeout=5) == 0
        winner_output = winner.stdout.read() if winner.stdout is not None else ""
        assert "STOPPED" in winner_output
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
