from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable

from global_quant.gate1a.coordinator import EventSourcedCoordinator
from global_quant.gate1a.coordinator import UnexplainedEventError
from global_quant.gate1a.ledger import AppendOnlyLedger
from global_quant.gate1a.recovery import CheckpointIntegrityError
from global_quant.gate1a.recovery import CheckpointStore


BTC = "BTCUSDT-PERP.BINANCE"
ROOT = Path(__file__).resolve().parents[3]
SOURCE_HASH = hashlib.sha256(
    (ROOT / "src/global_quant/gate1a/strategy.py").read_bytes(),
).hexdigest()
CONFIG_HASH = hashlib.sha256(
    (ROOT / "protocols/NT_GATE_1A.md").read_bytes(),
).hexdigest()

REQUIRED_SCENARIOS = (
    "new_order_rejected",
    "submitted_unacknowledged",
    "partial_then_complete",
    "partial_then_cancel",
    "cancel_reject_fill_race",
    "reversal_before_old_close",
    "protection_fill_cancels_sibling",
    "main_close_cancels_protection",
    "duplicate_events",
    "out_of_order_events",
    "unknown_external_event",
    "snapshot_replay_mismatch",
)


@dataclass
class ScenarioResult:
    name: str
    status: str
    initial_state: dict
    input_events: list[str]
    expected_orders: list[str]
    expected_fills: list[str]
    final_positions: dict
    final_wallet: str
    protection_state: dict
    exit_code: int
    fail_closed: bool
    expected_failure: str | None
    observed_events: list[str]
    ledger_hash: str
    business_hash: str
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _coordinator(root: Path, scenario: str) -> EventSourcedCoordinator:
    return EventSourcedCoordinator(
        ledger=AppendOnlyLedger(root / scenario / "events.jsonl"),
        initial_wallet=Decimal("10000"),
        strategy_id="gate1a",
        run_id=f"scenario-{scenario}",
        process_start_id=f"process-{scenario}",
        source_hash=SOURCE_HASH,
        config_hash=CONFIG_HASH,
    )


def _submit_accept(
    coordinator: EventSourcedCoordinator,
    order_id: str,
    venue_id: str,
) -> None:
    coordinator.mark_submitted(order_id)
    coordinator.mark_accepted(order_id, venue_id)


def _open_long(
    coordinator: EventSourcedCoordinator,
    *,
    decision_id: str = "open",
    quantity: Decimal = Decimal("1"),
) -> str:
    order = coordinator.request_target(decision_id, BTC, quantity)
    assert order is not None
    _submit_accept(coordinator, order.client_order_id, f"venue-{decision_id}")
    coordinator.apply_fill(
        order.client_order_id,
        fill_id=f"fill-{decision_id}",
        quantity=quantity,
        price=Decimal("100"),
        fee=Decimal("0.10"),
    )
    return order.client_order_id


def _protection_state(coordinator: EventSourcedCoordinator) -> dict:
    return {
        group: {
            order_id: coordinator.orders[order_id].status
            for order_id in sorted(order_ids)
        }
        for group, order_ids in sorted(coordinator.protection_groups.items())
    }


def _result(
    name: str,
    coordinator: EventSourcedCoordinator,
    *,
    initial_state: dict,
    input_events: list[str],
    expected_orders: list[str],
    expected_fills: list[str],
    expected_failure: str | None = None,
) -> ScenarioResult:
    coordinator.assert_invariants()
    snapshot = coordinator.business_snapshot()
    return ScenarioResult(
        name=name,
        status="PASS",
        initial_state=initial_state,
        input_events=input_events,
        expected_orders=expected_orders,
        expected_fills=expected_fills,
        final_positions=snapshot["positions"],
        final_wallet=snapshot["wallet_balance"],
        protection_state=_protection_state(coordinator),
        exit_code=0,
        fail_closed=coordinator.fail_closed,
        expected_failure=expected_failure,
        observed_events=[
            event.event_type
            for event in coordinator.ledger.read_all()
        ],
        ledger_hash=coordinator.ledger.last_event_hash,
        business_hash=coordinator.business_hash(),
    )


def _new_order_rejected(root: Path) -> ScenarioResult:
    coordinator = _coordinator(root, "new_order_rejected")
    order = coordinator.request_target("d1", BTC, Decimal("1"))
    assert order is not None
    coordinator.mark_submitted(order.client_order_id)
    coordinator.mark_rejected(order.client_order_id)
    assert coordinator.orders[order.client_order_id].status == "REJECTED"
    assert coordinator.position(BTC).quantity == 0
    return _result(
        "new_order_rejected",
        coordinator,
        initial_state={"position": "0", "wallet": "10000"},
        input_events=["decision", "submit", "reject"],
        expected_orders=[order.client_order_id],
        expected_fills=[],
    )


def _submitted_unacknowledged(root: Path) -> ScenarioResult:
    coordinator = _coordinator(root, "submitted_unacknowledged")
    order = coordinator.request_target("d1", BTC, Decimal("1"))
    assert order is not None
    coordinator.mark_submitted(order.client_order_id)
    assert coordinator.orders[order.client_order_id].status == "SUBMITTED"
    return _result(
        "submitted_unacknowledged",
        coordinator,
        initial_state={"position": "0", "wallet": "10000"},
        input_events=["decision", "submit"],
        expected_orders=[order.client_order_id],
        expected_fills=[],
    )


def _partial_then_complete(root: Path) -> ScenarioResult:
    coordinator = _coordinator(root, "partial_then_complete")
    order = coordinator.request_target("d1", BTC, Decimal("2"))
    assert order is not None
    _submit_accept(coordinator, order.client_order_id, "venue-1")
    coordinator.apply_fill(
        order.client_order_id,
        fill_id="fill-1",
        quantity=Decimal("0.75"),
        price=Decimal("100"),
        fee=Decimal("0.075"),
    )
    coordinator.apply_fill(
        order.client_order_id,
        fill_id="fill-2",
        quantity=Decimal("1.25"),
        price=Decimal("101"),
        fee=Decimal("0.12625"),
    )
    assert coordinator.orders[order.client_order_id].status == "FILLED"
    assert coordinator.position(BTC).quantity == Decimal("2")
    return _result(
        "partial_then_complete",
        coordinator,
        initial_state={"position": "0", "wallet": "10000"},
        input_events=["decision", "submit", "accept", "partial_fill", "final_fill"],
        expected_orders=[order.client_order_id],
        expected_fills=["fill-1", "fill-2"],
    )


def _partial_then_cancel(root: Path) -> ScenarioResult:
    coordinator = _coordinator(root, "partial_then_cancel")
    order = coordinator.request_target("d1", BTC, Decimal("2"))
    assert order is not None
    _submit_accept(coordinator, order.client_order_id, "venue-1")
    coordinator.apply_fill(
        order.client_order_id,
        fill_id="fill-1",
        quantity=Decimal("0.75"),
        price=Decimal("100"),
        fee=Decimal("0.075"),
    )
    coordinator.request_cancel(order.client_order_id)
    coordinator.mark_canceled(order.client_order_id)
    assert coordinator.orders[order.client_order_id].remaining_quantity == Decimal(
        "1.25",
    )
    return _result(
        "partial_then_cancel",
        coordinator,
        initial_state={"position": "0", "wallet": "10000"},
        input_events=["decision", "submit", "accept", "partial_fill", "cancel"],
        expected_orders=[order.client_order_id],
        expected_fills=["fill-1"],
    )


def _cancel_reject_fill_race(root: Path) -> ScenarioResult:
    coordinator = _coordinator(root, "cancel_reject_fill_race")
    order = coordinator.request_target("d1", BTC, Decimal("1"))
    assert order is not None
    _submit_accept(coordinator, order.client_order_id, "venue-1")
    coordinator.request_cancel(order.client_order_id)
    coordinator.mark_cancel_rejected(order.client_order_id)
    coordinator.request_cancel(order.client_order_id)
    coordinator.apply_fill(
        order.client_order_id,
        fill_id="race-fill",
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0.10"),
    )
    assert coordinator.orders[order.client_order_id].status == "FILLED"
    return _result(
        "cancel_reject_fill_race",
        coordinator,
        initial_state={"position": "0", "wallet": "10000"},
        input_events=[
            "decision",
            "submit",
            "accept",
            "cancel_request",
            "cancel_reject",
            "cancel_request",
            "fill_before_cancel_confirmation",
        ],
        expected_orders=[order.client_order_id],
        expected_fills=["race-fill"],
    )


def _reversal_before_old_close(root: Path) -> ScenarioResult:
    coordinator = _coordinator(root, "reversal_before_old_close")
    _open_long(coordinator, quantity=Decimal("2"))
    close = coordinator.request_target("reverse", BTC, Decimal("-1"))
    assert close is not None and close.reduce_only
    _submit_accept(coordinator, close.client_order_id, "venue-close")
    coordinator.apply_fill(
        close.client_order_id,
        fill_id="close-partial",
        quantity=Decimal("1"),
        price=Decimal("99"),
        fee=Decimal("0.099"),
    )
    assert len(coordinator.active_orders(BTC)) == 1
    coordinator.apply_fill(
        close.client_order_id,
        fill_id="close-final",
        quantity=Decimal("1"),
        price=Decimal("98"),
        fee=Decimal("0.098"),
    )
    opening = coordinator.active_orders(BTC)
    assert len(opening) == 1 and opening[0].role == "REVERSAL_OPEN"
    return _result(
        "reversal_before_old_close",
        coordinator,
        initial_state={"position": "2", "wallet": "9999.90"},
        input_events=["reverse_decision", "partial_close", "final_close"],
        expected_orders=[close.client_order_id, opening[0].client_order_id],
        expected_fills=["close-partial", "close-final"],
    )


def _protection_fill_cancels_sibling(root: Path) -> ScenarioResult:
    coordinator = _coordinator(root, "protection_fill_cancels_sibling")
    _open_long(coordinator)
    stop, take = coordinator.create_protection_group(
        group_id="protect-1",
        instrument_id=BTC,
        quantity=Decimal("1"),
    )
    _submit_accept(coordinator, stop.client_order_id, "venue-stop")
    _submit_accept(coordinator, take.client_order_id, "venue-take")
    coordinator.apply_fill(
        stop.client_order_id,
        fill_id="stop-fill",
        quantity=Decimal("1"),
        price=Decimal("95"),
        fee=Decimal("0.095"),
    )
    assert coordinator.orders[take.client_order_id].status == "CANCEL_PENDING"
    coordinator.mark_canceled(take.client_order_id)
    return _result(
        "protection_fill_cancels_sibling",
        coordinator,
        initial_state={"position": "1", "wallet": "9999.90"},
        input_events=["create_protection", "stop_fill", "sibling_cancel"],
        expected_orders=[stop.client_order_id, take.client_order_id],
        expected_fills=["stop-fill"],
    )


def _main_close_cancels_protection(root: Path) -> ScenarioResult:
    coordinator = _coordinator(root, "main_close_cancels_protection")
    _open_long(coordinator)
    stop, take = coordinator.create_protection_group(
        group_id="protect-1",
        instrument_id=BTC,
        quantity=Decimal("1"),
    )
    _submit_accept(coordinator, stop.client_order_id, "venue-stop")
    _submit_accept(coordinator, take.client_order_id, "venue-take")
    close = coordinator.request_target("close", BTC, Decimal("0"))
    assert close is not None
    _submit_accept(coordinator, close.client_order_id, "venue-close")
    coordinator.apply_fill(
        close.client_order_id,
        fill_id="close-fill",
        quantity=Decimal("1"),
        price=Decimal("102"),
        fee=Decimal("0.102"),
    )
    assert coordinator.orders[stop.client_order_id].status == "CANCEL_PENDING"
    assert coordinator.orders[take.client_order_id].status == "CANCEL_PENDING"
    coordinator.mark_canceled(stop.client_order_id)
    coordinator.mark_canceled(take.client_order_id)
    return _result(
        "main_close_cancels_protection",
        coordinator,
        initial_state={"position": "1", "wallet": "9999.90"},
        input_events=["create_protection", "main_close", "cancel_protection"],
        expected_orders=[
            stop.client_order_id,
            take.client_order_id,
            close.client_order_id,
        ],
        expected_fills=["close-fill"],
    )


def _duplicate_events(root: Path) -> ScenarioResult:
    coordinator = _coordinator(root, "duplicate_events")
    order_id = _open_long(coordinator)
    assert (
        coordinator.apply_fill(
            order_id,
            fill_id="fill-open",
            quantity=Decimal("1"),
            price=Decimal("100"),
            fee=Decimal("0.10"),
        )
        is False
    )
    before = coordinator.business_hash()
    for event in coordinator.ledger.read_all():
        coordinator._reduce(event)
    assert coordinator.business_hash() == before
    return _result(
        "duplicate_events",
        coordinator,
        initial_state={"position": "0", "wallet": "10000"},
        input_events=["duplicate_decision", "duplicate_fill", "duplicate_replay"],
        expected_orders=[order_id],
        expected_fills=["fill-open"],
    )


def _out_of_order_events(root: Path) -> ScenarioResult:
    coordinator = _coordinator(root, "out_of_order_events")
    order = coordinator.request_target("d1", BTC, Decimal("1"))
    assert order is not None
    coordinator.mark_submitted(order.client_order_id)
    coordinator.apply_fill(
        order.client_order_id,
        fill_id="fill-before-ack",
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0.10"),
    )
    coordinator.mark_accepted(order.client_order_id, "late-venue-id")
    assert coordinator.orders[order.client_order_id].status == "FILLED"
    return _result(
        "out_of_order_events",
        coordinator,
        initial_state={"position": "0", "wallet": "10000"},
        input_events=["submit", "fill", "late_accept"],
        expected_orders=[order.client_order_id],
        expected_fills=["fill-before-ack"],
    )


def _unknown_external_event(root: Path) -> ScenarioResult:
    coordinator = _coordinator(root, "unknown_external_event")
    try:
        coordinator.apply_fill(
            "unknown-order",
            fill_id="unknown-fill",
            quantity=Decimal("1"),
            price=Decimal("100"),
            fee=Decimal("0"),
        )
    except UnexplainedEventError:
        pass
    else:
        raise AssertionError("unknown fill did not fail closed")
    assert coordinator.fail_closed
    return _result(
        "unknown_external_event",
        coordinator,
        initial_state={"position": "0", "wallet": "10000"},
        input_events=["unknown_external_fill"],
        expected_orders=[],
        expected_fills=[],
        expected_failure="UnexplainedEventError",
    )


def _snapshot_replay_mismatch(root: Path) -> ScenarioResult:
    coordinator = _coordinator(root, "snapshot_replay_mismatch")
    order_id = _open_long(coordinator)
    path = root / "snapshot_replay_mismatch" / "checkpoint.json"
    store = CheckpointStore(path)
    store.save(coordinator)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["business_snapshot"]["wallet_balance"] = "999999"
    path.write_text(json.dumps(raw), encoding="utf-8")
    try:
        store.validate_against(coordinator)
    except CheckpointIntegrityError:
        pass
    else:
        raise AssertionError("snapshot mismatch was not detected")
    return _result(
        "snapshot_replay_mismatch",
        coordinator,
        initial_state={"position": "0", "wallet": "10000"},
        input_events=["write_checkpoint", "corrupt_checkpoint", "validate"],
        expected_orders=[order_id],
        expected_fills=["fill-open"],
        expected_failure="CheckpointIntegrityError",
    )


SCENARIO_RUNNERS: dict[str, Callable[[Path], ScenarioResult]] = {
    "new_order_rejected": _new_order_rejected,
    "submitted_unacknowledged": _submitted_unacknowledged,
    "partial_then_complete": _partial_then_complete,
    "partial_then_cancel": _partial_then_cancel,
    "cancel_reject_fill_race": _cancel_reject_fill_race,
    "reversal_before_old_close": _reversal_before_old_close,
    "protection_fill_cancels_sibling": _protection_fill_cancels_sibling,
    "main_close_cancels_protection": _main_close_cancels_protection,
    "duplicate_events": _duplicate_events,
    "out_of_order_events": _out_of_order_events,
    "unknown_external_event": _unknown_external_event,
    "snapshot_replay_mismatch": _snapshot_replay_mismatch,
}


def run_all_scenarios(root: Path | str) -> list[ScenarioResult]:
    destination = Path(root)
    results: list[ScenarioResult] = []
    for name in REQUIRED_SCENARIOS:
        try:
            result = SCENARIO_RUNNERS[name](destination)
        except Exception as exc:
            results.append(
                ScenarioResult(
                    name=name,
                    status="STOP",
                    initial_state={"declared": True},
                    input_events=["scenario_failed"],
                    expected_orders=[],
                    expected_fills=[],
                    final_positions={},
                    final_wallet="UNKNOWN",
                    protection_state={},
                    exit_code=1,
                    fail_closed=True,
                    expected_failure=None,
                    observed_events=[],
                    ledger_hash="",
                    business_hash="",
                    error=f"{type(exc).__name__}: {exc}",
                ),
            )
        else:
            results.append(result)
    return results
