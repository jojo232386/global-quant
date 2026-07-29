from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import fields
from decimal import Decimal
from decimal import InvalidOperation
from pathlib import Path
from typing import Any


class LedgerIntegrityError(RuntimeError):
    """Raised when the durable event chain cannot be trusted."""


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


_SCHEMA_VERSION = "1.0"
_SETTLEMENT_CURRENCY = "USDT"
_QUANTITY_PRECISION = 8
_PRICE_PRECISION = 2

_REQUIRED_EVENT_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "DECISION": (
        ("decision_id",),
        ("instrument_id",),
        ("order_intent", "target_quantity"),
    ),
    "ORDER_INTENT": (
        ("decision_id",),
        ("instrument_id",),
        ("client_order_id",),
        ("order_intent", "side"),
        ("order_intent", "quantity"),
        ("order_intent", "role"),
        ("order_intent", "reduce_only"),
        ("order_intent", "trigger_price"),
    ),
    "STALE_ORDER_EVENT": (
        ("instrument_id",),
        ("client_order_id",),
        ("order_transition", "from"),
        ("order_transition", "attempted"),
    ),
    "ORDER_TRANSITION": (
        ("instrument_id",),
        ("client_order_id",),
        ("order_transition", "from"),
        ("order_transition", "to"),
    ),
    "FILL": (
        ("instrument_id",),
        ("client_order_id",),
        ("fill", "fill_id"),
        ("fill", "side"),
        ("fill", "quantity"),
        ("fill", "signed_quantity"),
        ("fill", "price"),
        ("fee",),
        ("position_transition", "quantity_before"),
        ("position_transition", "signed_fill_quantity"),
        ("balance_transition", "fee"),
    ),
    "PROTECTION_RESIZE": (
        ("instrument_id",),
        ("client_order_id",),
        ("order_transition", "from_quantity"),
        ("order_transition", "to_quantity"),
        ("order_transition", "status"),
        ("protection_group_id",),
    ),
    "MARK_PRICE": (
        ("instrument_id",),
        ("fill", "price"),
    ),
    "RECONCILIATION": (
        ("balance_transition", "wallet_balance"),
        ("balance_transition", "positions"),
    ),
    "ANOMALY": (("fill", "message"),),
}

_NULLABLE_REQUIRED_PATHS = {
    ("ORDER_INTENT", "order_intent", "trigger_price"),
}

_DECIMAL_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "DECISION": (
        ("order_intent", "target_quantity"),
    ),
    "ORDER_INTENT": (
        ("order_intent", "quantity"),
        ("order_intent", "trigger_price"),
    ),
    "FILL": (
        ("fill", "quantity"),
        ("fill", "signed_quantity"),
        ("fill", "price"),
        ("fee",),
        ("position_transition", "quantity_before"),
        ("position_transition", "signed_fill_quantity"),
        ("balance_transition", "fee"),
    ),
    "PROTECTION_RESIZE": (
        ("order_transition", "from_quantity"),
        ("order_transition", "to_quantity"),
    ),
    "MARK_PRICE": (
        ("fill", "price"),
    ),
    "RECONCILIATION": (
        ("balance_transition", "wallet_balance"),
    ),
}


def _path_value(raw: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = raw
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise LedgerIntegrityError(
                f"required economic field missing: {'.'.join(path)}",
            )
        value = value[key]
    return value


def _validate_decimal(
    value: Any,
    *,
    path: tuple[str, ...],
) -> None:
    label = ".".join(path)
    if not isinstance(value, str):
        raise LedgerIntegrityError(f"{label} must be a canonical Decimal string")
    try:
        decimal_value = Decimal(value)
    except InvalidOperation as exc:
        raise LedgerIntegrityError(
            f"{label} must be a canonical Decimal string",
        ) from exc
    if (
        not decimal_value.is_finite()
        or str(decimal_value) != value
        or (decimal_value.is_zero() and decimal_value.is_signed())
    ):
        raise LedgerIntegrityError(f"{label} must be a canonical Decimal string")


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
        if not isinstance(raw, dict):
            raise LedgerIntegrityError("ledger event must be a JSON object")
        expected = set(cls.required_fields())
        actual = set(raw)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise LedgerIntegrityError(f"ledger schema mismatch missing={missing} extra={extra}")

        if raw["schema_version"] != _SCHEMA_VERSION:
            raise LedgerIntegrityError(
                f"unknown schema_version {raw['schema_version']!r}",
            )
        if raw["settlement_currency"] != _SETTLEMENT_CURRENCY:
            raise LedgerIntegrityError(
                f"settlement_currency must be {_SETTLEMENT_CURRENCY}",
            )
        if (
            type(raw["quantity_precision"]) is not int
            or raw["quantity_precision"] != _QUANTITY_PRECISION
        ):
            raise LedgerIntegrityError(
                f"quantity_precision must be {_QUANTITY_PRECISION}",
            )
        if (
            type(raw["price_precision"]) is not int
            or raw["price_precision"] != _PRICE_PRECISION
        ):
            raise LedgerIntegrityError(
                f"price_precision must be {_PRICE_PRECISION}",
            )

        event_type = raw["event_type"]
        if not isinstance(event_type, str) or event_type not in _REQUIRED_EVENT_PATHS:
            raise LedgerIntegrityError(f"unknown event_type {event_type!r}")

        for identity_field in (
            "source_hash",
            "config_hash",
            "strategy_id",
            "account_id",
        ):
            value = raw[identity_field]
            if not isinstance(value, str) or not value:
                raise LedgerIntegrityError(f"{identity_field} must be a non-empty string")

        for path in _REQUIRED_EVENT_PATHS[event_type]:
            value = _path_value(raw, path)
            nullable_path = (event_type, *path) in _NULLABLE_REQUIRED_PATHS
            if value is None and not nullable_path:
                raise LedgerIntegrityError(
                    f"required economic field missing: {'.'.join(path)}",
                )

        for path in _DECIMAL_PATHS.get(event_type, ()):
            value = _path_value(raw, path)
            if value is None and (event_type, *path) in _NULLABLE_REQUIRED_PATHS:
                continue
            _validate_decimal(
                value,
                path=path,
            )

        if event_type == "RECONCILIATION":
            positions = _path_value(raw, ("balance_transition", "positions"))
            if not isinstance(positions, dict):
                raise LedgerIntegrityError(
                    "required economic field missing: balance_transition.positions",
                )
            for instrument_id, quantity in positions.items():
                if not isinstance(instrument_id, str) or not instrument_id:
                    raise LedgerIntegrityError(
                        "reconciliation position instrument must be a non-empty string",
                    )
                _validate_decimal(
                    quantity,
                    path=("balance_transition", "positions", instrument_id),
                )

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
    _IDENTITY_FIELDS = (
        "source_hash",
        "config_hash",
        "strategy_id",
        "account_id",
    )

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._events: list[LedgerEvent] = []
        self._by_event_id: dict[str, LedgerEvent] = {}
        self._identity: tuple[str, ...] | None = None
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
            self._validate_identity(event)
            self._events.append(event)
            self._by_event_id[event.event_id] = event
            self._identity = self._event_identity(event)
            previous_hash = event.event_hash

    @classmethod
    def _event_identity(cls, event: LedgerEvent) -> tuple[str, ...]:
        return tuple(getattr(event, field) for field in cls._IDENTITY_FIELDS)

    def _validate_identity(self, event: LedgerEvent) -> None:
        if self._identity is None:
            return
        candidate = self._event_identity(event)
        if candidate == self._identity:
            return
        mismatches = [
            field
            for field, expected, actual in zip(
                self._IDENTITY_FIELDS,
                self._identity,
                candidate,
                strict=True,
            )
            if actual != expected
        ]
        raise LedgerIntegrityError(
            f"ledger identity mismatch fields={mismatches}",
        )

    @staticmethod
    def _calculate_event_hash(event: LedgerEvent) -> str:
        raw = event.to_dict()
        raw["event_hash"] = ""
        return hashlib.sha256(_canonical_json(raw).encode()).hexdigest()

    def append(self, event: LedgerEvent) -> bool:
        LedgerEvent.from_dict(event.to_dict())
        existing = self._by_event_id.get(event.event_id)
        if existing is not None:
            if existing.semantic_fingerprint() != event.semantic_fingerprint():
                raise LedgerIntegrityError(
                    f"conflicting duplicate event id {event.event_id}",
                )
            return False

        self._validate_identity(event)
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
        self._identity = self._event_identity(stored)
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
