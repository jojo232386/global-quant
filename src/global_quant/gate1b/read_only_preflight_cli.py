"""Hidden-prompt CLI for the isolated v1.11 authenticated read-only preflight."""

from __future__ import annotations

import getpass
import json
import os
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from types import ModuleType

from global_quant.gate1b.credential_session import (
    _disable_core_dumps,
    _read_hidden_credentials,
)
from global_quant.gate1b.read_only_preflight import (
    MAX_NUM_ORDERS_STATUS,
    POSITION_RISK_CONTROL_STATUS,
    PROTOCOL_VERSION,
    AuthenticatedReadOnlyPreflightTransport,
    DiagnosticCategory,
    DiagnosticStage,
    ReadOnlyPreflightError,
    SafeReadOnlyDiagnostic,
    build_authenticated_read_only_transport,
)
from global_quant.gate1b.safety import DemoCredentials

_STOP_PAYLOAD = {
    "protocol_version": PROTOCOL_VERSION,
    "status": "STOP",
    "stage": DiagnosticStage.CREDENTIAL_INPUT.value,
    "category": DiagnosticCategory.LOCAL_INPUT_FAILURE.value,
}


class _PromptDeadline:
    def __init__(self) -> None:
        self._deadline_ns = time.monotonic_ns() + 300_000_000_000

    def assert_intact(self) -> None:
        if time.monotonic_ns() >= self._deadline_ns:
            raise RuntimeError("PROMPT_DEADLINE_EXHAUSTED")


class _PromptBootstrap:
    def __init__(self) -> None:
        self.hard_deadline = _PromptDeadline()


def _encoded_secret_free(payload: Mapping[str, object], secrets: tuple[str, ...]) -> str:
    encoded = json.dumps(dict(payload), ensure_ascii=True, sort_keys=True)
    if any(secret and secret in encoded for secret in secrets):
        raise RuntimeError("CREDENTIAL_OUTPUT_GUARD_FAILED")
    return encoded


def run_prompted_read_only_preflight(
    *,
    prompt_secret: Callable[[str], str] = getpass.getpass,
    environ: Mapping[str, str] | None = None,
    input_is_tty: bool | None = None,
    core_dump_guard: Callable[[], None] = _disable_core_dumps,
    credential_importer: Callable[[str], ModuleType] | None = None,
    transport_builder: Callable[
        [DemoCredentials], AuthenticatedReadOnlyPreflightTransport
    ] = build_authenticated_read_only_transport,
) -> int:
    """Prompt locally, execute fixed GETs, and print only sanitized state."""

    api_key = ""
    api_secret = ""
    forbidden_values: tuple[str, ...] = ()
    transport: AuthenticatedReadOnlyPreflightTransport | None = None
    try:
        parent_environment = dict(os.environ) if environ is None else environ
        tty = os.isatty(0) if input_is_tty is None else input_is_tty
        core_dump_guard()
        prompt_kwargs: dict[str, object] = {
            "prompt_secret": prompt_secret,
            "environ": parent_environment,
            "input_is_tty": tty,
        }
        if credential_importer is not None:
            prompt_kwargs["importer"] = credential_importer
        api_key, api_secret, forbidden_values = _read_hidden_credentials(
            _PromptBootstrap(),
            **prompt_kwargs,  # type: ignore[arg-type]
        )
        transport = transport_builder(DemoCredentials(api_key=api_key, api_secret=api_secret))
        results = transport.run_fixed_preflight()
        payload = {
            "max_num_orders": MAX_NUM_ORDERS_STATUS,
            "order_authorization_ready": False,
            "position_risk_control": POSITION_RISK_CONTROL_STATUS,
            "protocol_version": PROTOCOL_VERSION,
            "results": [result.to_mapping() for result in results],
            "status": "AUTHENTICATED_READ_ONLY_PREFLIGHT_COMPLETE",
        }
        print(_encoded_secret_free(payload, forbidden_values))
        return 0
    except BaseException as exc:
        diagnostic = (
            exc.diagnostic
            if isinstance(exc, ReadOnlyPreflightError) and exc.diagnostic is not None
            else SafeReadOnlyDiagnostic(
                stage=DiagnosticStage.CREDENTIAL_INPUT,
                category=DiagnosticCategory.LOCAL_INPUT_FAILURE,
            )
        )
        print(_encoded_secret_free(diagnostic.to_stop_payload(), forbidden_values))
        return 1
    finally:
        if transport is not None:
            with suppress(BaseException):
                transport.close()
        api_key = ""
        api_secret = ""
        forbidden_values = ()


def main(argv: list[str] | None = None) -> int:
    """Require an explicit Demo-only arming token; accept no other input."""

    arguments = list(argv or [])
    if arguments != ["--confirm-demo-only"]:
        print(json.dumps(_STOP_PAYLOAD, sort_keys=True))
        return 1
    return run_prompted_read_only_preflight()
