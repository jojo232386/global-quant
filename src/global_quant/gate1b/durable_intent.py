"""Durable, owner-only intent publication for Gate 1B v1.6.

The protocol objects in :mod:`mutation_protocol` are pure validators.  In
particular, their ``persisted`` flag is not itself evidence that bytes reached
stable storage.  This module is the only adapter which turns an unpersisted
intent into a replay-verified durable intent.  Publication is single-use,
file-fsynced, atomically linked into place, and followed by a parent-directory
fsync before the returned object can grant mutation preparation.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import stat
from contextlib import suppress
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from global_quant.gate1b.mutation_protocol import (
    CREATE_DEADLINE_SECONDS,
    MAX_HARD_MUTATION_REQUESTS,
    MAX_HTTP_REQUESTS,
    MAX_POST_CREATE_READ_REQUESTS,
    MAX_READ_RETRIES,
    NORMAL_MUTATION_REQUESTS,
    POST_CREATE_HTTP_RESERVE,
    PROTOCOL_STATUS,
    PROTOCOL_VERSION,
    TOTAL_RUNTIME_SECONDS,
    DurableIntent,
    FrozenLimitOrder,
    LimitOrderFilters,
    OrderDerivationProof,
)

_SCHEMA_VERSION = "gate1b.durable-intent.v1"
_MAX_INTENT_BYTES = 64 * 1024
_TOP_LEVEL_FIELDS = frozenset(
    {
        "authorization_id",
        "budgets",
        "derived_order",
        "filter_contract_sha256",
        "filter_snapshot_sha256",
        "intent_sha256",
        "order_derivation",
        "probe_payload",
        "protocol",
        "runtime_commit",
        "schema_version",
        "session_nonce",
    }
)
_PROTOCOL_FIELDS = frozenset({"commit", "sha256", "status", "tag", "tag_object", "version"})
_BUDGET_FIELDS = frozenset(
    {
        "create_deadline_seconds",
        "max_hard_mutation_requests",
        "max_http_requests",
        "max_post_create_read_requests",
        "max_read_retries",
        "normal_mutation_requests",
        "post_create_http_reserve",
        "total_runtime_seconds",
    }
)
_DERIVATION_FIELDS = frozenset(
    {
        "best_ask",
        "best_bid",
        "book_age_ms",
        "filter_contract_sha256",
        "filter_snapshot_sha256",
        "filters",
        "mark_age_ms",
        "mark_price",
        "observed_elapsed_seconds",
    }
)
_FILTER_FIELDS = frozenset(
    {
        "lot_size_filter_count",
        "max_price",
        "max_quantity",
        "min_notional",
        "min_notional_filter_count",
        "min_price",
        "min_quantity",
        "percent_price_filter_count",
        "percent_price_multiplier_down",
        "percent_price_multiplier_up",
        "price_filter_count",
        "step_size",
        "tick_size",
        "uninterpreted_applicable_filter_types",
    }
)
_ORDER_FIELDS = frozenset(
    {
        "notional",
        "order_type",
        "position_side",
        "price",
        "quantity",
        "reduce_only",
        "response_type",
        "side",
        "symbol",
        "time_in_force",
    }
)
_REPLAY_CONSTRUCTOR_TOKEN = object()


class DurableIntentError(RuntimeError):
    """The intent could not be published or replayed without ambiguity."""


@dataclass(frozen=True, slots=True, init=False)
class PersistedIntent:
    """Replayable proof returned only after reopening the published bytes."""

    path: Path
    intent: DurableIntent
    file_sha256: str

    @classmethod
    def _from_verified_replay(
        cls,
        *,
        path: Path,
        intent: DurableIntent,
        file_sha256: str,
        _token: object,
    ) -> PersistedIntent:
        if _token is not _REPLAY_CONSTRUCTOR_TOKEN:
            raise DurableIntentError("PERSISTED_INTENT_CONSTRUCTOR_FORBIDDEN")
        instance = object.__new__(cls)
        object.__setattr__(instance, "path", path)
        object.__setattr__(instance, "intent", intent)
        object.__setattr__(instance, "file_sha256", file_sha256)
        instance.__post_init__()
        return instance

    def __post_init__(self) -> None:
        if (
            not isinstance(self.path, Path)
            or type(self.intent) is not DurableIntent
            or self.intent.persisted is not True
            or type(self.file_sha256) is not str
            or len(self.file_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.file_sha256)
        ):
            raise DurableIntentError("PERSISTED_INTENT_RECEIPT_INVALID")


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise DurableIntentError("INTENT_VALUE_NOT_CANONICAL") from exc


def _decimal(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise DurableIntentError("INTENT_DECIMAL_INVALID")
    return format(value, "f")


def _parse_decimal(value: object) -> Decimal:
    if type(value) is not str:
        raise DurableIntentError("INTENT_DECIMAL_INVALID")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise DurableIntentError("INTENT_DECIMAL_INVALID") from exc
    if not parsed.is_finite() or format(parsed, "f") != value:
        raise DurableIntentError("INTENT_DECIMAL_INVALID")
    return parsed


def _exact_mapping(value: object, fields: frozenset[str], reason: str) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != fields:
        raise DurableIntentError(reason)
    return value


def _filter_payload(filters: LimitOrderFilters) -> dict[str, object]:
    return {
        "lot_size_filter_count": filters.lot_size_filter_count,
        "max_price": _decimal(filters.max_price),
        "max_quantity": _decimal(filters.max_quantity),
        "min_notional": _decimal(filters.min_notional),
        "min_notional_filter_count": filters.min_notional_filter_count,
        "min_price": _decimal(filters.min_price),
        "min_quantity": _decimal(filters.min_quantity),
        "percent_price_filter_count": filters.percent_price_filter_count,
        "percent_price_multiplier_down": _decimal(filters.percent_price_multiplier_down),
        "percent_price_multiplier_up": _decimal(filters.percent_price_multiplier_up),
        "price_filter_count": filters.price_filter_count,
        "step_size": _decimal(filters.step_size),
        "tick_size": _decimal(filters.tick_size),
        "uninterpreted_applicable_filter_types": list(
            filters.uninterpreted_applicable_filter_types
        ),
    }


def _derivation_payload(proof: OrderDerivationProof) -> dict[str, object]:
    return {
        "best_ask": _decimal(proof.best_ask),
        "best_bid": _decimal(proof.best_bid),
        "book_age_ms": _decimal(proof.book_age_ms),
        "filter_contract_sha256": proof.filter_contract_sha256,
        "filter_snapshot_sha256": proof.filter_snapshot_sha256,
        "filters": _filter_payload(proof.filters),
        "mark_age_ms": _decimal(proof.mark_age_ms),
        "mark_price": _decimal(proof.mark_price),
        "observed_elapsed_seconds": _decimal(proof.observed_elapsed_seconds),
    }


def _order_payload(order: FrozenLimitOrder) -> dict[str, object]:
    return {
        "notional": _decimal(order.notional),
        "order_type": order.order_type,
        "position_side": order.position_side,
        "price": _decimal(order.price),
        "quantity": _decimal(order.quantity),
        "reduce_only": order.reduce_only,
        "response_type": order.response_type,
        "side": order.side,
        "symbol": order.symbol,
        "time_in_force": order.time_in_force,
    }


def _record_payload(intent: DurableIntent) -> dict[str, object]:
    if type(intent) is not DurableIntent:
        raise DurableIntentError("DURABLE_INTENT_TYPE_REQUIRED")
    return {
        "authorization_id": intent.authorization_id,
        "budgets": _budget_payload(),
        "derived_order": _order_payload(intent.probe_order),
        "filter_contract_sha256": intent.order_derivation.filter_contract_sha256,
        "filter_snapshot_sha256": intent.filter_snapshot_sha256,
        "intent_sha256": intent.intent_sha256,
        "order_derivation": _derivation_payload(intent.order_derivation),
        "probe_payload": intent.probe_payload,
        "protocol": {
            "commit": intent.protocol_commit,
            "sha256": intent.protocol_sha256,
            "status": PROTOCOL_STATUS,
            "tag": "nt-gate-1b-v1.6-protocol",
            "tag_object": intent.protocol_tag_object,
            "version": PROTOCOL_VERSION,
        },
        "runtime_commit": intent.runtime_commit,
        "schema_version": _SCHEMA_VERSION,
        "session_nonce": intent.session_nonce,
    }


def _budget_payload() -> dict[str, int]:
    return {
        "create_deadline_seconds": CREATE_DEADLINE_SECONDS,
        "max_hard_mutation_requests": MAX_HARD_MUTATION_REQUESTS,
        "max_http_requests": MAX_HTTP_REQUESTS,
        "max_post_create_read_requests": MAX_POST_CREATE_READ_REQUESTS,
        "max_read_retries": MAX_READ_RETRIES,
        "normal_mutation_requests": NORMAL_MUTATION_REQUESTS,
        "post_create_http_reserve": POST_CREATE_HTTP_RESERVE,
        "total_runtime_seconds": TOTAL_RUNTIME_SECONDS,
    }


def _open_owner_directory(path: Path) -> tuple[int, os.stat_result]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        before = path.stat(follow_symlinks=False)
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DurableIntentError("INTENT_DIRECTORY_UNAVAILABLE") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise DurableIntentError("INTENT_DIRECTORY_NOT_OWNER_ONLY")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise DurableIntentError("INTENT_DIRECTORY_PATH_RACE")
        return descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def _assert_parent_path_identity(path: Path, expected: os.stat_result) -> None:
    try:
        observed = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DurableIntentError("INTENT_DIRECTORY_PATH_RACE") from exc
    if (observed.st_dev, observed.st_ino) != (expected.st_dev, expected.st_ino):
        raise DurableIntentError("INTENT_DIRECTORY_PATH_RACE")


def _create_temporary(parent_fd: int) -> tuple[int, str]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    for _attempt in range(16):
        name = f".intent-{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise DurableIntentError("INTENT_TEMPORARY_CREATE_FAILED") from exc
        return descriptor, name
    raise DurableIntentError("INTENT_TEMPORARY_CREATE_FAILED")


def persist_intent(path: Path, intent: DurableIntent) -> PersistedIntent:
    """Publish one immutable intent and replay it before returning authority."""

    if not isinstance(path, Path) or path.name in {"", ".", ".."}:
        raise DurableIntentError("INTENT_PATH_INVALID")
    if type(intent) is not DurableIntent:
        raise DurableIntentError("DURABLE_INTENT_TYPE_REQUIRED")
    if intent.persisted is not False:
        raise DurableIntentError("INTENT_MUST_START_UNPERSISTED")
    encoded = _canonical_json(_record_payload(intent))
    if len(encoded) > _MAX_INTENT_BYTES:
        raise DurableIntentError("INTENT_RECORD_OVERSIZED")

    parent_fd, parent_stat = _open_owner_directory(path.parent)
    temporary_name: str | None = None
    temporary_fd = -1
    try:
        try:
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise DurableIntentError("INTENT_DESTINATION_CHECK_FAILED") from exc
        else:
            raise DurableIntentError("INTENT_ALREADY_EXISTS")
        temporary_fd, temporary_name = _create_temporary(parent_fd)
        try:
            os.fchmod(temporary_fd, 0o600)
            view = memoryview(encoded)
            while view:
                written = os.write(temporary_fd, view)
                if written <= 0:
                    raise DurableIntentError("INTENT_WRITE_FAILED")
                view = view[written:]
            os.fsync(temporary_fd)
            temporary_stat = os.fstat(temporary_fd)
        except OSError as exc:
            raise DurableIntentError("INTENT_WRITE_FAILED") from exc
        try:
            os.link(
                temporary_name,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise DurableIntentError("INTENT_ALREADY_EXISTS") from None
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise DurableIntentError("INTENT_ALREADY_EXISTS") from None
            raise DurableIntentError("INTENT_PUBLICATION_FAILED") from exc
        try:
            published_stat = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise DurableIntentError("INTENT_PUBLICATION_FAILED") from exc
        if (temporary_stat.st_dev, temporary_stat.st_ino) != (
            published_stat.st_dev,
            published_stat.st_ino,
        ):
            raise DurableIntentError("INTENT_TEMPORARY_INODE_CHANGED")
        os.unlink(temporary_name, dir_fd=parent_fd)
        temporary_name = None
        try:
            os.fsync(parent_fd)
        except OSError as exc:
            raise DurableIntentError("INTENT_DIRECTORY_FSYNC_FAILED") from exc
        _assert_parent_path_identity(path.parent, parent_stat)
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_name is not None:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=parent_fd)
        os.close(parent_fd)
    replayed = load_persisted_intent(path)
    if replayed.intent != replace(intent, persisted=True):
        raise DurableIntentError("INTENT_INPUT_REPLAY_MISMATCH")
    return replayed


def _load_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    parent_fd, parent_stat = _open_owner_directory(path.parent)
    descriptor = -1
    try:
        before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode):
            raise DurableIntentError("INTENT_FILE_SYMLINK")
        descriptor = os.open(path.name, flags, dir_fd=parent_fd)
    except DurableIntentError:
        os.close(parent_fd)
        raise
    except OSError as exc:
        os.close(parent_fd)
        raise DurableIntentError("INTENT_FILE_OPEN_FAILED") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise DurableIntentError("INTENT_FILE_NOT_OWNER_ONLY")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise DurableIntentError("INTENT_FILE_PATH_RACE")
        if opened.st_size <= 0 or opened.st_size > _MAX_INTENT_BYTES:
            raise DurableIntentError("INTENT_RECORD_SIZE_INVALID")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 16 * 1024))
            if not chunk:
                raise DurableIntentError("INTENT_RECORD_TRUNCATED")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise DurableIntentError("INTENT_RECORD_SIZE_CHANGED")
        after = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise DurableIntentError("INTENT_FILE_PATH_RACE")
        _assert_parent_path_identity(path.parent, parent_stat)
        return b"".join(chunks)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _parse_filters(value: object) -> LimitOrderFilters:
    item = _exact_mapping(value, _FILTER_FIELDS, "INTENT_FILTER_FIELDS_INVALID")
    unknown = item["uninterpreted_applicable_filter_types"]
    if type(unknown) is not list or any(type(name) is not str for name in unknown):
        raise DurableIntentError("INTENT_FILTER_FIELDS_INVALID")
    integer_fields = (
        "lot_size_filter_count",
        "min_notional_filter_count",
        "percent_price_filter_count",
        "price_filter_count",
    )
    if any(type(item[name]) is not int for name in integer_fields):
        raise DurableIntentError("INTENT_FILTER_FIELDS_INVALID")
    try:
        return LimitOrderFilters(
            min_price=_parse_decimal(item["min_price"]),
            max_price=_parse_decimal(item["max_price"]),
            tick_size=_parse_decimal(item["tick_size"]),
            min_quantity=_parse_decimal(item["min_quantity"]),
            max_quantity=_parse_decimal(item["max_quantity"]),
            step_size=_parse_decimal(item["step_size"]),
            min_notional=_parse_decimal(item["min_notional"]),
            percent_price_multiplier_down=_parse_decimal(item["percent_price_multiplier_down"]),
            percent_price_multiplier_up=_parse_decimal(item["percent_price_multiplier_up"]),
            price_filter_count=item["price_filter_count"],
            lot_size_filter_count=item["lot_size_filter_count"],
            min_notional_filter_count=item["min_notional_filter_count"],
            percent_price_filter_count=item["percent_price_filter_count"],
            uninterpreted_applicable_filter_types=tuple(unknown),
        )
    except (TypeError, ValueError) as exc:
        raise DurableIntentError("INTENT_FILTER_CONTRACT_INVALID") from exc


def _parse_derivation(value: object) -> OrderDerivationProof:
    item = _exact_mapping(value, _DERIVATION_FIELDS, "INTENT_DERIVATION_FIELDS_INVALID")
    try:
        return OrderDerivationProof(
            best_bid=_parse_decimal(item["best_bid"]),
            best_ask=_parse_decimal(item["best_ask"]),
            mark_price=_parse_decimal(item["mark_price"]),
            filters=_parse_filters(item["filters"]),
            filter_snapshot_sha256=item["filter_snapshot_sha256"],
            filter_contract_sha256=item["filter_contract_sha256"],
            book_age_ms=_parse_decimal(item["book_age_ms"]),
            mark_age_ms=_parse_decimal(item["mark_age_ms"]),
            observed_elapsed_seconds=_parse_decimal(item["observed_elapsed_seconds"]),
        )
    except (TypeError, ValueError) as exc:
        raise DurableIntentError("INTENT_DERIVATION_INVALID") from exc


def _intent_from_payload(payload: object) -> DurableIntent:
    item = _exact_mapping(payload, _TOP_LEVEL_FIELDS, "INTENT_FIELDS_INVALID")
    protocol = _exact_mapping(item["protocol"], _PROTOCOL_FIELDS, "INTENT_PROTOCOL_FIELDS_INVALID")
    budgets = _exact_mapping(item["budgets"], _BUDGET_FIELDS, "INTENT_BUDGET_FIELDS_INVALID")
    if any(type(value) is not int for value in budgets.values()):
        raise DurableIntentError("INTENT_BUDGET_FIELDS_INVALID")
    if budgets != _budget_payload():
        raise DurableIntentError("INTENT_BUDGET_MISMATCH")
    if (
        item["schema_version"] != _SCHEMA_VERSION
        or protocol["version"] != PROTOCOL_VERSION
        or protocol["status"] != PROTOCOL_STATUS
        or protocol["tag"] != "nt-gate-1b-v1.6-protocol"
    ):
        raise DurableIntentError("INTENT_PROTOCOL_BINDING_INVALID")
    derivation = _parse_derivation(item["order_derivation"])
    try:
        intent = DurableIntent(
            authorization_id=item["authorization_id"],
            protocol_commit=protocol["commit"],
            protocol_tag_object=protocol["tag_object"],
            protocol_sha256=protocol["sha256"],
            runtime_commit=item["runtime_commit"],
            session_nonce=item["session_nonce"],
            order_derivation=derivation,
            persisted=True,
        )
    except (TypeError, ValueError) as exc:
        raise DurableIntentError("INTENT_CONTRACT_INVALID") from exc
    expected = _record_payload(intent)
    if item != expected:
        raise DurableIntentError("INTENT_RECOMPUTATION_MISMATCH")
    return intent


def load_persisted_intent(path: Path) -> PersistedIntent:
    """Reopen and recompute every authority-bearing intent field."""

    if not isinstance(path, Path) or not path.name:
        raise DurableIntentError("INTENT_PATH_INVALID")
    raw = _load_bytes(path)
    try:
        payload = json.loads(raw, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise DurableIntentError("INTENT_JSON_INVALID") from exc
    intent = _intent_from_payload(payload)
    if raw != _canonical_json(payload):
        raise DurableIntentError("INTENT_CANONICAL_ENCODING_INVALID")
    return PersistedIntent._from_verified_replay(
        path=path,
        intent=replace(intent, persisted=True),
        file_sha256=hashlib.sha256(raw).hexdigest(),
        _token=_REPLAY_CONSTRUCTOR_TOKEN,
    )
