"""Credential-bearing child session for the frozen NT-GATE-1B v1.6 protocol.

This module is the child half of the protocol section 4 supervisor/child
boundary. It is launched by a credential-free supervisor (``supervisor.py``)
in a real Terminal the agent does not control. The child:

* refuses to start if the parent environment already holds any Binance
  credential name (protocol section 4);
* disables core dumps before any credential is read;
* reads the Demo API key and secret through the existing non-echoing
  ``getpass`` prompt (or a guarded owner-only Ed25519 private key file), so no
  credential value or identifier ever enters chat, agent context, argv, Git,
  evidence, or ordinary logs;
* builds a ``DemoLifecycleTransport`` bound to the frozen Demo origin via the
  existing ``safety.build_demo_http_apis`` signing client;
* runs ``run_mutation_lifecycle`` exactly once, with mutation retry = 0;
* writes a sanitized ``child-pre-exit.json`` (no credential, no signed URL, no
  raw response) and terminates so process exit completes credential cleanup.

No credential value is retained after the child process exits. The supervisor
attests the child exit separately.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from global_quant.gate1b.authorization import (
    AuthorizationError,
    claim_authorization,
    is_valid_authorization_id,
)
from global_quant.gate1b.credential_prompt import (
    disable_core_dumps,
    read_ed25519_private_key,
)
from global_quant.gate1b.demo_transport import DemoLifecycleTransport
from global_quant.gate1b.mutation_runner import run_mutation_lifecycle
from global_quant.gate1b.safety import (
    CONFLICTING_CREDENTIAL_NAMES,
    DEMO_KEY_NAME,
    DEMO_SECRET_NAME,
    DemoCredentials,
    assert_secret_free,
)

_ALL_BINANCE_CREDENTIAL_NAMES = (
    DEMO_KEY_NAME,
    DEMO_SECRET_NAME,
    *CONFLICTING_CREDENTIAL_NAMES,
)


class CredentialSessionError(RuntimeError):
    """Raised before any credential is read when the session boundary is unsafe."""


def _validate_parent_environment(environ: Mapping[str, str]) -> None:
    if any(name in environ for name in _ALL_BINANCE_CREDENTIAL_NAMES):
        raise CredentialSessionError("CREDENTIAL_ENVIRONMENT_MUST_BE_EMPTY")


def _write_child_pre_exit(
    evidence_dir: Path,
    payload: Mapping[str, Any],
    credentials: DemoCredentials,
) -> Path:
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    assert_secret_free(encoded, credentials)
    tmp: Path | None = None
    try:
        descriptor, tmp_name = tempfile.mkstemp(
            prefix=".child-pre-exit-", suffix=".json", dir=str(evidence_dir)
        )
        tmp = Path(tmp_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, evidence_dir / "child-pre-exit.json")
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()
    # Final secret scan over the whole evidence directory.
    for candidate in evidence_dir.rglob("*"):
        if candidate.is_file():
            assert_secret_free(candidate.read_text(errors="ignore"), credentials)
    return evidence_dir / "child-pre-exit.json"


def _stop_payload(
    *,
    reason: str,
    binding: Mapping[str, str],
    credential_environment_empty: bool,
) -> dict[str, Any]:
    return {
        "gate": "NT-GATE-1B",
        "protocol_version": "1.6",
        "mode": "CREDENTIAL_BEARING_CHILD",
        "status": "STOP",
        "reason_codes": [reason],
        "credential_environment_empty": credential_environment_empty,
        "credentials_read": False,
        "network_accessed": False,
        "authenticated_request_sent": False,
        "order_summary": {"canceled": 0, "filled": 0, "submitted": 0},
        "economic_event_summary": {"fees": 0, "funding": 0},
        "position_changes": 0,
        "agent_credential_access_allowed": False,
        "next_action": "STOP_CREDENTIAL_SESSION",
        **dict(binding),
    }


def run_credential_session(
    *,
    evidence_dir: Path,
    binding: Mapping[str, str],
    authorization_manifest: Path,
    prompt_secret: Callable[[str], str] = getpass.getpass,
    transport_factory: Callable[[DemoCredentials], DemoLifecycleTransport] | None = None,
    environ: Mapping[str, str] | None = None,
    key_type: str = "hmac",
    private_key_file: Path | None = None,
    input_is_tty: bool = True,
) -> tuple[int, Path]:
    """Run the credential-bearing child lifecycle once.

    ``transport_factory`` defaults to the real Demo HTTP adapter built from the
    prompted credentials; tests inject a fake factory returning a fixture-driven
    transport without any network or real credential.
    """

    evidence_dir = Path(evidence_dir)
    parent_environ = environ if environ is not None else dict(os.environ)
    _validate_parent_environment(parent_environ)
    if not input_is_tty:
        raise CredentialSessionError("INTERACTIVE_TERMINAL_REQUIRED")
    disable_core_dumps()

    # Authorization must be atomically claimed (ACTIVE → CONSUMED) and bound
    # to this exact runtime before any credential is read.  Two concurrent
    # processes with the same authorization ID cannot both succeed — at most
    # one enters the credential-bearing lifecycle.  The other fails closed
    # before credential input or network/mutation.
    try:
        record = claim_authorization(
            Path(authorization_manifest),
            authorization_id=str(binding["authorization_id"]),
            protocol_commit=str(binding["protocol_commit"]),
            protocol_tag_object=str(binding["protocol_tag_object"]),
            protocol_sha256=str(binding["protocol_sha256"]),
            runtime_commit=str(binding["runtime_commit"]),
        )
    except AuthorizationError as exc:
        payload = _stop_payload(
            reason=str(exc),
            binding=binding,
            credential_environment_empty=True,
        )
        path = _write_child_pre_exit(
            evidence_dir, payload, DemoCredentials(api_key="<none>", api_secret="<none>")
        )
        return 1, path

    # Hidden credential input. Values never enter argv, Git, evidence, logs, or
    # the returned payload. They exist only in this process's ephemeral locals.
    if key_type == "ed25519":
        if private_key_file is None:
            raise CredentialSessionError("ED25519_PRIVATE_KEY_FILE_REQUIRED")
        api_secret = read_ed25519_private_key(private_key_file)
    elif key_type == "hmac":
        if private_key_file is not None:
            raise CredentialSessionError("PRIVATE_KEY_FILE_FORBIDDEN_FOR_HMAC")
        api_secret = ""
    else:
        raise CredentialSessionError("UNSUPPORTED_DEMO_KEY_TYPE")

    api_key = prompt_secret("Demo API key (hidden): ")
    if not api_key:
        raise CredentialSessionError("EMPTY_DEMO_CREDENTIAL")
    if key_type == "hmac":
        api_secret = prompt_secret("Demo API secret (hidden): ")
        if not api_secret:
            raise CredentialSessionError("EMPTY_DEMO_CREDENTIAL")

    credentials = DemoCredentials(api_key=api_key, api_secret=api_secret)

    try:
        if transport_factory is None:
            transport_factory = _build_real_transport
        transport = transport_factory(credentials)
        try:
            exit_code, lifecycle_path = run_mutation_lifecycle(
                transport,
                project_root=Path(__file__).resolve().parents[3],
                evidence_dir=evidence_dir,
                # Protocol section 4: the child environment itself holds no
                # credential value (the hidden input lives in memory only), so
                # the lifecycle sees an empty credential environment.
                environ={},
                runtime_commit=str(binding["runtime_commit"]),
                session_nonce=str(binding["session_nonce"]),
                authorization_id=str(binding["authorization_id"]),
                protocol_commit=str(binding["protocol_commit"]),
                protocol_tag_object=str(binding["protocol_tag_object"]),
                protocol_sha256=str(binding["protocol_sha256"]),
            )
        finally:
            with contextlib.suppress(Exception):
                transport.close()
        # Protocol section 10 step 11: the child atomically writes a sanitized
        # pre-exit bundle before terminating, in every outcome. The supervisor
        # uses it to attest the child exit; it never contains a PASS claim.
        _write_child_pre_exit(
            evidence_dir,
            {
                "status": "child_complete",
                "child_exit_code": exit_code,
                "lifecycle_evidence": str(lifecycle_path),
                "credential_redaction": "PASS",
            },
            credentials,
        )
        return exit_code, lifecycle_path
    except Exception as exc:
        # Never retain raw response/exception text; record only the type.
        payload = _stop_payload(
            reason=f"CREDENTIAL_SESSION_FAILURE_{type(exc).__name__.upper()}",
            binding=binding,
            credential_environment_empty=True,
        )
        path = _write_child_pre_exit(evidence_dir, payload, credentials)
        return 1, path
    finally:
        api_key = ""
        api_secret = ""


def _build_real_transport(credentials: DemoCredentials) -> DemoLifecycleTransport:
    """Build the real Demo HTTP adapter via the existing signing client."""

    from global_quant.gate1b.demo_preflight import build_demo_http_apis

    apis = build_demo_http_apis(credentials)
    return DemoLifecycleTransport(http_client=apis.client)


def _binding_from_args(args: argparse.Namespace) -> dict[str, str]:
    return {
        "runtime_commit": args.runtime_commit,
        "session_nonce": args.session_nonce,
        "authorization_id": args.authorization_id,
        "protocol_commit": args.protocol_commit,
        "protocol_tag_object": args.protocol_tag_object,
        "protocol_sha256": args.protocol_sha256,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the NT-GATE-1B v1.6 credential-bearing child session."
    )
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--runtime-commit", required=True)
    parser.add_argument("--session-nonce", required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--protocol-commit", required=True)
    parser.add_argument("--protocol-tag-object", required=True)
    parser.add_argument("--protocol-sha256", required=True)
    parser.add_argument("--authorization-manifest", type=Path, required=True)
    parser.add_argument("--key-type", choices=("hmac", "ed25519"), default="hmac")
    parser.add_argument("--private-key-file", type=Path, default=None)
    args = parser.parse_args(argv)

    if not is_valid_authorization_id(args.authorization_id):
        print(json.dumps({"exit_code": 1, "reason": "INVALID_AUTHORIZATION_ID_FORMAT"}))
        return 1

    binding = _binding_from_args(args)
    try:
        exit_code, path = run_credential_session(
            evidence_dir=args.evidence_dir,
            binding=binding,
            authorization_manifest=args.authorization_manifest,
            key_type=args.key_type,
            private_key_file=args.private_key_file,
            input_is_tty=sys.stdin.isatty(),
        )
    except CredentialSessionError as exc:
        print(json.dumps({"exit_code": 1, "reason": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"exit_code": exit_code, "evidence": str(path)}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
