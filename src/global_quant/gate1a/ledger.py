from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import fields
from pathlib import Path
from typing import Any


class LedgerIntegrityError(RuntimeError):
    """Raised when the durable event chain cannot be trusted."""


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


@dataclass
class LedgerEvent:
    decision_id: str | None
    strategy_id: str
    run_id: str
    instrument_id: str | None
    client_order_id: str | None
    venue_order_id: str | None
    position_id: str | None
    correlation_id: str | None
    causation_id: str | None
    event_id: str
    event_sequence: int
    event_timestamp: str
    receive_timestamp: str
    event_type: str
    order_intent: dict[str, Any] | None
    order_transition: dict[str, Any] | None
    fill: dict[str, Any] | None
    fee: str | None
    position_transition: dict[str, Any] | None
    balance_transition: dict[str, Any] | None
    protection_group_id: str | None
    persistence_checkpoint: dict[str, Any] | None
    process_start_id: str
    source_hash: str
    config_hash: str
    schema_version: str = "1.0"
    ledger_sequence: int = 0
    account_id: str = "GATE1A-USDT"
    source_event_id: str | None = None
    dedupe_key: str | None = None
    persisted_at: str | None = None
    previous_event_hash: str = "GENESIS"
    event_hash: str = ""
    settlement_currency: str = "USDT"
    quantity_precision: int = 8
    price_precision: int = 2

    @classmethod
    def required_fields(cls) -> tuple[str, ...]:
        return tuple(field.name for field in fields(cls))

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LedgerEvent:
        expected = set(cls.required_fields())
        actual = set(raw)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise LedgerIntegrityError(f"ledger schema mismatch missing={missing} extra={extra}")
        return cls(**raw)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def semantic_fingerprint(self) -> str:
        raw = self.to_dict()
        for key in (
            "ledger_sequence",
            "event_hash",
            "previous_event_hash",
            "persisted_at",
        ):
            raw.pop(key)
        return hashlib.sha256(_canonical_json(raw).encode()).hexdigest()


class AppendOnlyLedger:
    """Durable JSONL ledger with sequence validation and a SHA-256 hash chain."""

    _BUSINESS_VOLATILE_FIELDS = {
        "run_id",
        "process_start_id",
        "receive_timestamp",
        "persisted_at",
        "ledger_sequence",
        "previous_event_hash",
        "event_hash",
    }

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._events: list[LedgerEvent] = []
        self._by_event_id: dict[str, LedgerEvent] = {}
        if self.path.exists():
            self._load()

    @property
    def next_sequence(self) -> int:
        return len(self._events) + 1

    @property
    def last_event_hash(self) -> str:
        if not self._events:
            return "GENESIS"
        return self._events[-1].event_hash

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise LedgerIntegrityError("ledger is not valid UTF-8") from exc

        previous_hash = "GENESIS"
        for line_number, line in enumerate(lines, start=1):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerIntegrityError(
                    f"invalid JSON at ledger line {line_number}",
                ) from exc
            event = LedgerEvent.from_dict(raw)
            if event.ledger_sequence != line_number or event.event_sequence != line_number:
                raise LedgerIntegrityError(f"invalid sequence at ledger line {line_number}")
            if event.previous_event_hash != previous_hash:
                raise LedgerIntegrityError(f"hash chain break at ledger line {line_number}")
            expected_hash = self._calculate_event_hash(event)
            if event.event_hash != expected_hash:
                raise LedgerIntegrityError(f"event hash mismatch at ledger line {line_number}")
            if event.event_id in self._by_event_id:
                raise LedgerIntegrityError(f"duplicate event id at ledger line {line_number}")
            self._events.append(event)
            self._by_event_id[event.event_id] = event
            previous_hash = event.event_hash

    @staticmethod
    def _calculate_event_hash(event: LedgerEvent) -> str:
        raw = event.to_dict()
        raw["event_hash"] = ""
        return hashlib.sha256(_canonical_json(raw).encode()).hexdigest()

    def append(self, event: LedgerEvent) -> bool:
        existing = self._by_event_id.get(event.event_id)
        if existing is not None:
            if existing.semantic_fingerprint() != event.semantic_fingerprint():
                raise LedgerIntegrityError(
                    f"conflicting duplicate event id {event.event_id}",
                )
            return False

        expected_sequence = self.next_sequence
        if event.event_sequence != expected_sequence:
            raise LedgerIntegrityError(
                f"event sequence {event.event_sequence} != expected {expected_sequence}",
            )

        event.ledger_sequence = expected_sequence
        event.previous_event_hash = self.last_event_hash
        event.persisted_at = event.persisted_at or event.receive_timestamp
        event.event_hash = self._calculate_event_hash(event)
        payload = _canonical_json(event.to_dict()) + "\n"

        descriptor = os.open(
            self.path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            encoded = payload.encode("utf-8")
            written = 0
            while written < len(encoded):
                written += os.write(descriptor, encoded[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

        stored = LedgerEvent.from_dict(json.loads(payload))
        self._events.append(stored)
        self._by_event_id[stored.event_id] = stored
        return True

    def read_all(self) -> list[LedgerEvent]:
        return copy.deepcopy(self._events)

    def business_hash(self) -> str:
        canonical_events: list[dict[str, Any]] = []
        for event in self._events:
            raw = event.to_dict()
            for key in self._BUSINESS_VOLATILE_FIELDS:
                raw.pop(key)
            canonical_events.append(raw)
        payload = json.dumps(
            canonical_events,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()
