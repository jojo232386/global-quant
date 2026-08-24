"""Minimal, exception-only PIT lifecycle sidecar for EXPL-017-IMPL-016.

Price V1 remains immutable.  This module validates an independently versioned
sidecar that contains only early-ending series.  It deliberately never infers
termination from a later missing price bar.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


DAY_MS = 86_400_000
PRICE_DATASET_SHA = "a7d65a9223d5b66baa93826c1706a6eeb718718211a0d7fe94371d03ded4ec9b"
PRICE_MANIFEST_SHA = "cd2ae988fac8bca1b4c67d5985d93d3dcc145c7b7c598a9e5a0377c7c49bf166"
PRICE_PIT_SHA = "b006eae7dde9514e656156749d9891edf5fe70c2e12811b0395e15e1b4ef643e"
SIDECAR_PATH = Path(__file__).with_name("expl-017-lifecycle-v1.json")
COMPOSITE_PATH = Path(__file__).with_name("expl-017-price-lifecycle-composite.json")

TERMINATED_CONFIRMED = "TERMINATED_CONFIRMED"
TERMINATED_UNCONFIRMED = "TERMINATED_UNCONFIRMED"
DATA_CORRUPTION = "DATA_CORRUPTION"


class LifecycleError(RuntimeError):
    """A sidecar identity or lifecycle semantic invariant failed."""


class LifecycleDataUnavailable(LifecycleError):
    """A required price has no PIT-available lifecycle interpretation."""


@dataclass(frozen=True)
class LifecycleEvent:
    symbol: str
    classification: str
    effective_at_ms: int
    published_at_ms: int
    last_valid_bar_ms: int


@dataclass(frozen=True)
class LifecycleView:
    active: bool
    terminal_timestamp: int | None = None


def _parse_utc(value: str, *, field: str) -> int:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise LifecycleError(f"invalid {field}") from error
    if parsed.tzinfo is None:
        raise LifecycleError(f"timezone absent for {field}")
    return int(parsed.timestamp() * 1000)


def _day_start(value: str, *, field: str) -> int:
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as error:
        raise LifecycleError(f"invalid {field}") from error
    return int(dt.datetime.combine(parsed, dt.time(), tzinfo=dt.UTC).timestamp() * 1000)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_sidecar(path: Path = SIDECAR_PATH) -> tuple[dict[str, LifecycleEvent], str]:
    """Load the exception-only sidecar and enforce its frozen Price V1 binding."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LifecycleError("lifecycle sidecar unreadable") from error
    if payload.get("artifact_class") != "EXPL_017_LIFECYCLE_V1":
        raise LifecycleError("wrong lifecycle artifact class")
    price = payload.get("price_v1")
    if price != {
        "snapshot_id": PRICE_DATASET_SHA,
        "manifest_sha256": PRICE_MANIFEST_SHA,
        "pit_sha256": PRICE_PIT_SHA,
    }:
        raise LifecycleError("Price V1 identity mismatch")
    scope = payload.get("scope_audit")
    if scope != {
        "continuous_symbols": 196,
        "early_end_symbols": 12,
        "internal_gap_symbols": 0,
        "confirmed_terminals": 11,
        "unresolved_terminals": 1,
        "data_corruption": 0,
    }:
        raise LifecycleError("lifecycle scope audit mismatch")
    raw_events = payload.get("exceptions")
    if not isinstance(raw_events, list) or len(raw_events) != 12:
        raise LifecycleError("exception-only event count mismatch")

    events: dict[str, LifecycleEvent] = {}
    for raw in raw_events:
        if not isinstance(raw, Mapping):
            raise LifecycleError("malformed lifecycle exception")
        symbol = raw.get("symbol")
        classification = raw.get("classification")
        if not isinstance(symbol, str) or not symbol.isalnum() or symbol != symbol.upper():
            raise LifecycleError("malformed lifecycle symbol")
        if classification not in {TERMINATED_CONFIRMED, TERMINATED_UNCONFIRMED}:
            raise LifecycleError("invalid lifecycle classification")
        if symbol in events:
            raise LifecycleError("duplicate lifecycle symbol")
        effective = _parse_utc(str(raw.get("terminal_effective_at_utc")), field="effective")
        published = _parse_utc(str(raw.get("evidence_published_at_utc")), field="published")
        last_bar = _day_start(str(raw.get("last_valid_daily_bar_utc")), field="last valid bar")
        if published > effective:
            raise LifecycleError("announcement published after terminal effective time")
        if classification == TERMINATED_CONFIRMED:
            source = raw.get("evidence_source")
            identity = raw.get("evidence_identity")
            if not isinstance(source, Mapping) or not isinstance(identity, Mapping):
                raise LifecycleError("confirmed terminal lacks primary evidence")
            if not all(isinstance(source.get(key), str) and source[key] for key in ("announcement_url", "trade_archive_url")):
                raise LifecycleError("confirmed terminal source incomplete")
            if not all(isinstance(identity.get(key), str) and len(identity[key]) == 64 for key in ("announcement_response_sha256", "trade_archive_sha256")):
                raise LifecycleError("confirmed terminal identity incomplete")
        events[symbol] = LifecycleEvent(symbol, classification, effective, published, last_bar)

    confirmed = sum(item.classification == TERMINATED_CONFIRMED for item in events.values())
    unresolved = sum(item.classification == TERMINATED_UNCONFIRMED for item in events.values())
    if (confirmed, unresolved) != (11, 1) or "AKROUSDT" not in events:
        raise LifecycleError("lifecycle exception classification mismatch")
    if events["AKROUSDT"].classification != TERMINATED_UNCONFIRMED:
        raise LifecycleError("AKROUSDT must remain unresolved")
    return events, sha256_file(path)


def verify_composite_identity(sidecar_sha256: str, path: Path = COMPOSITE_PATH) -> str:
    """Verify the thin, non-data-bearing Price V1 + Lifecycle V1 binding."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LifecycleError("composite identity unreadable") from error
    expected = {
        "price_dataset_sha": PRICE_DATASET_SHA,
        "price_manifest_sha256": PRICE_MANIFEST_SHA,
        "price_pit_sha256": PRICE_PIT_SHA,
        "lifecycle_dataset_sha256": sidecar_sha256,
    }
    if payload.get("artifact_class") != "EXPL_017_PRICE_LIFECYCLE_COMPOSITE" or payload.get("identity") != expected:
        raise LifecycleError("composite identity mismatch")
    if payload.get("contains_price_data") is not False:
        raise LifecycleError("composite must not copy Price V1 data")
    return sha256_file(path)


class LifecycleResolver:
    """PIT resolver for a completed daily bar whose timestamp is its open."""

    def __init__(self, events: Mapping[str, LifecycleEvent]):
        self._events = dict(events)

    @staticmethod
    def _completed_close(timestamp: int) -> int:
        if type(timestamp) is not int or timestamp % DAY_MS:
            raise LifecycleError("completed bar timestamp must be a UTC day start")
        return timestamp + DAY_MS - 1

    def as_of(self, symbol: str, completed_bar_timestamp: int) -> LifecycleView:
        """Return TERMINATED only if the primary event was knowable by this close."""
        close = self._completed_close(completed_bar_timestamp)
        event = self._events.get(symbol)
        if event is None or event.classification != TERMINATED_CONFIRMED:
            return LifecycleView(True)
        if event.published_at_ms <= close and event.effective_at_ms <= close:
            return LifecycleView(False, event.last_valid_bar_ms)
        return LifecycleView(True)

    def terminal_event_as_of(
        self, symbol: str, completed_bar_timestamp: int
    ) -> LifecycleEvent | None:
        view = self.as_of(symbol, completed_bar_timestamp)
        return self._events.get(symbol) if not view.active else None

    def require_missing_bar_semantics(self, symbol: str, timestamp: int) -> None:
        """Raise a distinct error unless a confirmed event already explains absence."""
        event = self.terminal_event_as_of(symbol, timestamp)
        if event is None:
            raise LifecycleDataUnavailable(
                f"DATA_UNAVAILABLE: {symbol} missing bar has no PIT confirmed terminal"
            )
