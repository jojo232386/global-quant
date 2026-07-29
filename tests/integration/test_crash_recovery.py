from __future__ import annotations

import json
import os
import signal
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from global_quant.gate1a.coordinator import EventSourcedCoordinator
from global_quant.gate1a.ledger import AppendOnlyLedger
from global_quant.gate1a.recovery import CheckpointIntegrityError
from global_quant.gate1a.recovery import CheckpointStore
from global_quant.gate1a.recovery import RecoverySupervisor
from global_quant.gate1a.recovery import RecoveryBlockedError


ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "tests" / "helpers" / "crash_worker.py"

CRASH_POINTS = [
    "decision_and_intent_persisted",
    "order_submitted",
    "order_acknowledged",
    "partial_fill",
    "cancel_requested",
    "protection_update",
    "ledger_before_checkpoint",
    "submit_side_effect_unconfirmed",
    "execution_confirm_unpersisted",
    "sibling_cancel_unpersisted",
]


def run_worker(root: Path, phase: str) -> subprocess.CompletedProcess[str]:
    inherited_pythonpath = os.environ.get("PYTHONPATH")
    pythonpath = str(ROOT / "src")
    if inherited_pythonpath:
        pythonpath = f"{pythonpath}{os.pathsep}{inherited_pythonpath}"
    environment = {
        **os.environ,
        "PYTHONPATH": pythonpath,
    }
    return subprocess.run(
        [str(ROOT / ".venv" / "bin" / "python"), str(WORKER), str(root), phase],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


@pytest.mark.parametrize("phase", CRASH_POINTS)
def test_sigkill_recovery_is_durable_idempotent_and_order_unique(
    tmp_path,
    phase,
) -> None:
    completed = run_worker(tmp_path, phase)
    assert completed.returncode == -signal.SIGKILL

    recovery = RecoverySupervisor(
        ledger=AppendOnlyLedger(tmp_path / "events.jsonl"),
        initial_wallet=Decimal("10000"),
        checkpoint_path=tmp_path / "checkpoint.json",
        inbox_path=tmp_path / "execution_inbox.jsonl",
    ).recover()
    replayed = recovery.coordinator
    actions = {
        (action.kind, action.client_order_id)
        for action in recovery.actions
    }
    if phase == "decision_and_intent_persisted":
        assert any(kind == "SUBMIT_ORDER" for kind, _ in actions)
    if phase in {
        "order_submitted",
        "order_acknowledged",
        "partial_fill",
        "submit_side_effect_unconfirmed",
    }:
        assert any(kind == "RECONCILE_ORDER" for kind, _ in actions)
    if phase in {
        "cancel_requested",
        "sibling_cancel_unpersisted",
        "ledger_before_checkpoint",
    }:
        assert any(kind == "RECONCILE_CANCEL" for kind, _ in actions)
    if phase == "ledger_before_checkpoint":
        checkpoint = CheckpointStore(tmp_path / "checkpoint.json").load()
        assert checkpoint["last_ledger_sequence"] < len(replayed.ledger.read_all())
    if phase == "protection_update":
        assert sum(kind == "RECONCILE_ORDER" for kind, _ in actions) >= 2
    if phase == "execution_confirm_unpersisted":
        assert any(
            event.source_event_id == "pending-fill"
            for event in replayed.ledger.read_all()
        )
        assert replayed.position("BTCUSDT-PERP.BINANCE").quantity == Decimal("1")

    if phase == "submit_side_effect_unconfirmed":
        marker = (tmp_path / "submit_side_effect.marker").read_text(encoding="utf-8")
        assert ("RECONCILE_ORDER", marker) in actions
    if phase == "sibling_cancel_unpersisted":
        sibling_id = (tmp_path / "sibling_cancel.marker").read_text(encoding="utf-8")
        assert ("RECONCILE_CANCEL", sibling_id) in actions

    before = replayed.business_hash()
    client_order_ids = list(replayed.orders)
    durable_events = replayed.ledger.read_all()

    for event in durable_events:
        replayed._reduce(event)

    assert replayed.business_hash() == before
    assert len(client_order_ids) == len(set(client_order_ids))
    replayed.assert_invariants()


def test_strategy_recovery_supervisor_refuses_corrupt_checkpoint(tmp_path) -> None:
    completed = run_worker(tmp_path, "write_checkpoint")
    assert completed.returncode == 0
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text('{"business_hash":"truncated"', encoding="utf-8")

    with pytest.raises(CheckpointIntegrityError):
        RecoverySupervisor(
            ledger=AppendOnlyLedger(tmp_path / "events.jsonl"),
            initial_wallet=Decimal("10000"),
            checkpoint_path=checkpoint,
            inbox_path=tmp_path / "execution_inbox.jsonl",
        ).recover()


@pytest.mark.parametrize(
    ("field", "source_hash", "config_hash"),
    [
        ("source", "wrong-source", None),
        ("config", None, "wrong-config"),
    ],
)
def test_recovery_refuses_durable_state_from_different_code_or_protocol(
    tmp_path,
    field,
    source_hash,
    config_hash,
) -> None:
    completed = run_worker(tmp_path, "write_checkpoint")
    assert completed.returncode == 0

    with pytest.raises(RecoveryBlockedError, match=field):
        RecoverySupervisor(
            ledger=AppendOnlyLedger(tmp_path / "events.jsonl"),
            initial_wallet=Decimal("10000"),
            checkpoint_path=tmp_path / "checkpoint.json",
            inbox_path=tmp_path / "execution_inbox.jsonl",
            expected_source_hash=source_hash,
            expected_config_hash=config_hash,
        ).recover()


def test_corrupted_checkpoint_is_fatal_and_never_silently_rebuilt(tmp_path) -> None:
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text('{"business_hash":"truncated"', encoding="utf-8")

    with pytest.raises(CheckpointIntegrityError):
        CheckpointStore(checkpoint).load()


def test_second_crash_during_replay_does_not_change_durable_ledger(tmp_path) -> None:
    first = run_worker(tmp_path, "partial_fill")
    assert first.returncode == -signal.SIGKILL
    ledger_before = (tmp_path / "events.jsonl").read_bytes()

    second = run_worker(tmp_path, "crash_during_replay")
    assert second.returncode == -signal.SIGKILL
    assert (tmp_path / "events.jsonl").read_bytes() == ledger_before

    ledger = AppendOnlyLedger(tmp_path / "events.jsonl")
    replayed = EventSourcedCoordinator.replay(
        ledger=ledger,
        initial_wallet=Decimal("10000"),
    )
    replayed.assert_invariants()


def test_checkpoint_matches_replay_or_fails_closed(tmp_path) -> None:
    completed = run_worker(tmp_path, "write_checkpoint")
    assert completed.returncode == 0

    ledger = AppendOnlyLedger(tmp_path / "events.jsonl")
    replayed = EventSourcedCoordinator.replay(
        ledger=ledger,
        initial_wallet=Decimal("10000"),
    )
    checkpoint = CheckpointStore(tmp_path / "checkpoint.json").load()
    assert checkpoint["business_hash"] == replayed.business_hash()
    assert checkpoint["last_event_hash"] == ledger.last_event_hash

    raw = json.loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    raw["business_hash"] = "0" * 64
    (tmp_path / "checkpoint.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(CheckpointIntegrityError):
        CheckpointStore(tmp_path / "checkpoint.json").validate_against(replayed)


def test_reversal_target_survives_restart_during_partial_close(tmp_path) -> None:
    ledger = AppendOnlyLedger(tmp_path / "events.jsonl")
    coordinator = EventSourcedCoordinator(
        ledger=ledger,
        initial_wallet=Decimal("10000"),
        strategy_id="gate1a",
        run_id="run-1",
        process_start_id="process-1",
        source_hash="source-hash",
        config_hash="config-hash",
    )
    opening = coordinator.request_target("open", "BTCUSDT-PERP.BINANCE", Decimal("2"))
    assert opening is not None
    coordinator.mark_submitted(opening.client_order_id)
    coordinator.mark_accepted(opening.client_order_id, "venue-open")
    coordinator.apply_fill(
        opening.client_order_id,
        fill_id="open-fill",
        quantity=Decimal("2"),
        price=Decimal("100"),
        fee=Decimal("0.2"),
    )
    closing = coordinator.request_target(
        "reverse",
        "BTCUSDT-PERP.BINANCE",
        Decimal("-1"),
    )
    assert closing is not None
    coordinator.mark_submitted(closing.client_order_id)
    coordinator.mark_accepted(closing.client_order_id, "venue-close")
    coordinator.apply_fill(
        closing.client_order_id,
        fill_id="partial-close",
        quantity=Decimal("1"),
        price=Decimal("99"),
        fee=Decimal("0.099"),
    )

    recovered = EventSourcedCoordinator.replay(
        ledger=AppendOnlyLedger(tmp_path / "events.jsonl"),
        initial_wallet=Decimal("10000"),
    )
    recovered.apply_fill(
        closing.client_order_id,
        fill_id="final-close",
        quantity=Decimal("1"),
        price=Decimal("98"),
        fee=Decimal("0.098"),
    )

    pending = recovered.active_orders("BTCUSDT-PERP.BINANCE")
    assert len(pending) == 1
    assert pending[0].role == "REVERSAL_OPEN"
    assert pending[0].quantity == Decimal("1")
