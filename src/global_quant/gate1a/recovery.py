from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from global_quant.gate1a.coordinator import EventSourcedCoordinator


class CheckpointIntegrityError(RuntimeError):
    """Raised when a checkpoint is malformed or inconsistent with the ledger."""


class RecoveryBlockedError(RuntimeError):
    """Raised when durable state requires human or venue reconciliation."""


@dataclass(frozen=True)
class RecoveryAction:
    kind: str
    client_order_id: str
    reason: str


@dataclass(frozen=True)
class RecoveryResult:
    coordinator: EventSourcedCoordinator
    actions: tuple[RecoveryAction, ...]
    inbox_events_applied: int


class DurableInbox:
    """Raw execution-event inbox persisted before projection processing."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def append(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        records = self.read_all()
        record = {
            "schema_version": "1.0",
            "inbox_sequence": len(records) + 1,
            **payload,
        }
        encoded = (
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        descriptor = os.open(
            self.path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            written = 0
            while written < len(encoded):
                written += os.write(descriptor, encoded[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RecoveryBlockedError(
                    f"invalid inbox JSON at line {line_number}",
                ) from exc
            if (
                record.get("schema_version") != "1.0"
                or record.get("inbox_sequence") != line_number
            ):
                raise RecoveryBlockedError(
                    f"invalid inbox schema or sequence at line {line_number}",
                )
            records.append(record)
        return records


class CheckpointStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @staticmethod
    def _checksum(payload: dict[str, Any]) -> str:
        unsigned = dict(payload)
        unsigned.pop("checkpoint_hash", None)
        canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def save(self, coordinator: EventSourcedCoordinator) -> None:
        payload = {
            "schema_version": "1.0",
            "last_ledger_sequence": len(coordinator.ledger.read_all()),
            "last_event_hash": coordinator.ledger.last_event_hash,
            "business_hash": coordinator.business_hash(),
            "business_snapshot": coordinator.business_snapshot(),
        }
        payload["checkpoint_hash"] = self._checksum(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
            0o600,
        )
        try:
            encoded = (
                json.dumps(payload, sort_keys=True, indent=2) + "\n"
            ).encode()
            written = 0
            while written < len(encoded):
                written += os.write(descriptor, encoded[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        temporary.replace(self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CheckpointIntegrityError("checkpoint is unreadable") from exc
        required = {
            "schema_version",
            "last_ledger_sequence",
            "last_event_hash",
            "business_hash",
            "business_snapshot",
            "checkpoint_hash",
        }
        if set(payload) != required:
            raise CheckpointIntegrityError("checkpoint schema mismatch")
        if payload["schema_version"] != "1.0":
            raise CheckpointIntegrityError("unknown checkpoint schema")
        if payload["checkpoint_hash"] != self._checksum(payload):
            raise CheckpointIntegrityError("checkpoint hash mismatch")
        return payload

    def validate_against(
        self,
        coordinator: EventSourcedCoordinator,
    ) -> dict[str, Any]:
        payload = self.load()
        if payload["last_ledger_sequence"] != len(coordinator.ledger.read_all()):
            raise CheckpointIntegrityError("checkpoint ledger sequence mismatch")
        if payload["last_event_hash"] != coordinator.ledger.last_event_hash:
            raise CheckpointIntegrityError("checkpoint event hash mismatch")
        if payload["business_hash"] != coordinator.business_hash():
            raise CheckpointIntegrityError("checkpoint business hash mismatch")
        if payload["business_snapshot"] != coordinator.business_snapshot():
            raise CheckpointIntegrityError("checkpoint snapshot mismatch")
        return payload


class RecoverySupervisor:
    """Production recovery entrypoint shared by backtest and future clients."""

    def __init__(
        self,
        *,
        ledger,
        initial_wallet: Decimal,
        checkpoint_path: Path | str,
        inbox_path: Path | str,
    ) -> None:
        self.ledger = ledger
        self.initial_wallet = Decimal(initial_wallet)
        self.checkpoint_path = Path(checkpoint_path)
        self.inbox = DurableInbox(inbox_path)

    def recover(self) -> RecoveryResult:
        from global_quant.gate1a.coordinator import EventSourcedCoordinator

        coordinator = EventSourcedCoordinator.replay(
            ledger=self.ledger,
            initial_wallet=self.initial_wallet,
        )
        if self.checkpoint_path.exists():
            CheckpointStore(self.checkpoint_path).validate_against(coordinator)
        if coordinator.fail_closed:
            raise RecoveryBlockedError("durable coordinator state is fail-closed")

        applied = 0
        for record in self.inbox.read_all():
            if record.get("event_type") != "FILL":
                raise RecoveryBlockedError(
                    f"unsupported durable inbox event {record.get('event_type')}",
                )
            did_apply = coordinator.apply_fill(
                record["client_order_id"],
                fill_id=record["source_event_id"],
                quantity=Decimal(record["quantity"]),
                price=Decimal(record["price"]),
                fee=Decimal(record["fee"]),
            )
            applied += int(did_apply)

        coordinator.reconcile_protection_quantities()
        actions: list[RecoveryAction] = []
        for order in sorted(
            coordinator.active_orders(),
            key=lambda value: value.client_order_id,
        ):
            if order.status == "INTENT":
                kind = "SUBMIT_ORDER"
            elif order.status == "CANCEL_PENDING":
                kind = "RECONCILE_CANCEL"
            else:
                kind = "RECONCILE_ORDER"
            actions.append(
                RecoveryAction(
                    kind=kind,
                    client_order_id=order.client_order_id,
                    reason=f"durable order state is {order.status}",
                ),
            )
        return RecoveryResult(
            coordinator=coordinator,
            actions=tuple(actions),
            inbox_events_applied=applied,
        )
