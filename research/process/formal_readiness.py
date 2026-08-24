"""Fail-closed precondition checker used before a formal-run ID is created."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Mapping
from typing import Final


REQUIRED_READINESS: Final = (
    "DATASET_READY",
    "PIT_READY",
    "LIFECYCLE_READY",
    "GOLD_SAMPLE_READY",
    "CONSUMER_READY",
    "HORIZON_READY",
    "ACCOUNTING_READY",
    "REPORT_READY",
)


class FormalReadinessError(ValueError):
    """A formal run must not proceed from this readiness artifact."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise FormalReadinessError(f"duplicate readiness key: {key}")
        payload[key] = value
    return payload


def require_formal_readiness(checks: Mapping[str, object]) -> dict[str, bool]:
    """Validate every required readiness assertion before minting a formal ID.

    This checker does not create IDs, run research, or approve a formal run.
    It only returns the validated, exact readiness map; missing, non-boolean,
    false, or unrecognised assertions fail closed.
    """
    if not isinstance(checks, Mapping):
        raise FormalReadinessError("readiness must be a mapping")
    received = set(checks)
    expected = set(REQUIRED_READINESS)
    if received != expected:
        missing = sorted(expected - received)
        extra = sorted(received - expected)
        raise FormalReadinessError(f"readiness keys differ; missing={missing}, extra={extra}")
    validated: dict[str, bool] = {}
    for name in REQUIRED_READINESS:
        value = checks[name]
        if type(value) is not bool:
            raise FormalReadinessError(f"{name} must be a boolean")
        if value is not True:
            raise FormalReadinessError(f"{name} is not ready")
        validated[name] = value
    return validated


def load_and_require_formal_readiness(path: str | pathlib.Path) -> dict[str, bool]:
    """Load a JSON readiness artifact and validate it without any side effects."""
    candidate = pathlib.Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise FormalReadinessError("readiness artifact missing")
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, json.JSONDecodeError) as error:
        raise FormalReadinessError("readiness artifact unreadable") from error
    return require_formal_readiness(payload)
