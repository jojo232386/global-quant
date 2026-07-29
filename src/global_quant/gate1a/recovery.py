from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from global_quant.gate1a.coordinator import EventSourcedCoordinator


class CheckpointIntegrityError(RuntimeError):
    """Raised when a checkpoint is malformed or inconsistent with the ledger."""


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
            os.write(
                descriptor,
                (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode(),
            )
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
