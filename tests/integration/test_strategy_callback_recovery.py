from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from global_quant.gate1a.coordinator import EventSourcedCoordinator
from global_quant.gate1a.ledger import AppendOnlyLedger
from global_quant.gate1a.recovery import DurableInbox
from global_quant.gate1a.recovery import RecoveryBlockedError
from global_quant.gate1a.recovery import RecoverySupervisor


ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "tests/helpers/strategy_callback_worker.py"
ORACLE = (
    ROOT
    / "src/global_quant/gate1a/fixtures"
    / "nt_gate_1a_strategy_callback_oracle_v2.json"
)
ORACLE_SHA256 = "6bb21fc49e604bf300ed676b90c2b4322fa7e04ef7f3d0c25172e983987e1a21"
PROTOCOL_TAG = "nt-gate-1a-v1.2-protocol"


def load_oracle() -> dict:
    content = ORACLE.read_bytes()
    assert hashlib.sha256(content).hexdigest() == ORACLE_SHA256
    frozen = subprocess.run(
        [
            "git",
            "show",
            f"{PROTOCOL_TAG}:src/global_quant/gate1a/fixtures/"
            "nt_gate_1a_strategy_callback_oracle_v2.json",
        ],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout
    assert content == frozen
    return json.loads(content)["scenarios"]


def run_worker(root: Path, mode: str) -> subprocess.CompletedProcess[str]:
    inherited_pythonpath = os.environ.get("PYTHONPATH")
    pythonpath = str(ROOT / "src")
    if inherited_pythonpath:
        pythonpath = f"{pythonpath}{os.pathsep}{inherited_pythonpath}"
    return subprocess.run(
        [str(ROOT / ".venv/bin/python"), str(WORKER), str(root), mode],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": pythonpath},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def recovery(root: Path) -> RecoverySupervisor:
    return RecoverySupervisor(
        ledger=AppendOnlyLedger(root / "events.jsonl"),
        initial_wallet=Decimal("10000"),
        checkpoint_path=root / "events.checkpoint.json",
        inbox_path=root / "events.inbox.jsonl",
        expected_source_hash="v1.2-source",
        expected_config_hash="v1.2-config",
    )


def test_real_strategy_fill_survives_sigkill_and_applies_exactly_once(
    tmp_path,
) -> None:
    expected = load_oracle()["real_strategy_fill_crash_recovery"]
    completed = run_worker(tmp_path, "known_fill_crash")
    assert completed.returncode == -signal.SIGKILL, completed.stderr

    inbox = DurableInbox(tmp_path / "events.inbox.jsonl")
    ledger = AppendOnlyLedger(tmp_path / "events.jsonl")
    assert len(inbox.read_all()) == expected["durable_inbox_records_before_crash"]
    assert sum(event.event_type == "FILL" for event in ledger.read_all()) == expected[
        "ledger_fill_events_before_crash"
    ]

    first = recovery(tmp_path).recover()
    assert first.inbox_events_applied == expected["recovered_inbox_events_applied"]
    assert sum(
        event.event_type == "FILL" for event in first.coordinator.ledger.read_all()
    ) == expected["recovered_fill_events"]
    assert first.coordinator.position("BTCUSDT-PERP.BINANCE").quantity == Decimal(
        expected["recovered_position_quantity"],
    )
    assert first.coordinator.wallet_balance == Decimal(
        expected["recovered_wallet_balance"],
    )
    assert first.coordinator.fail_closed is expected["fail_closed"]

    second = recovery(tmp_path).recover()
    assert second.inbox_events_applied == expected[
        "second_recovery_inbox_events_applied"
    ]
    assert sum(
        event.event_type == "FILL" for event in second.coordinator.ledger.read_all()
    ) == expected["second_recovery_fill_events"]
    assert second.coordinator.business_hash() == first.coordinator.business_hash()


def test_real_strategy_unknown_fill_is_durably_fail_closed(tmp_path) -> None:
    expected = load_oracle()["real_strategy_unknown_fill"]
    completed = run_worker(tmp_path, "unknown_fill")
    assert completed.returncode != 0
    assert expected["expected_callback_exception"] in completed.stderr

    inbox = DurableInbox(tmp_path / "events.inbox.jsonl")
    assert len(inbox.read_all()) == expected["durable_inbox_records"]
    replayed = EventSourcedCoordinator.replay(
        ledger=AppendOnlyLedger(tmp_path / "events.jsonl"),
        initial_wallet=Decimal("10000"),
    )
    assert replayed.ledger.read_all()[-1].event_type == expected[
        "last_ledger_event_type"
    ]
    assert replayed.fail_closed is expected["fail_closed"]
    with pytest.raises(RecoveryBlockedError):
        recovery(tmp_path).recover()
