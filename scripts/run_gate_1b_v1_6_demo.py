#!/usr/bin/env python3
"""Official NT-GATE-1B v1.6 credential-bearing Demo CLI entry point.

This is the thin, real entry point for the frozen v1.6 mutation lifecycle. It
replaces the silent ``python -m global_quant.gate1b.mutation_runner`` path
(which is an offline-only fail-closed guard and never executes a mutation).

Flow (all credential-free until the very last step):

1. Parse and validate the frozen binding (runtime HEAD, protocol commit, tag
   object, protocol SHA-256) and the one-time authorization.
2. Mechanically verify the committed-runtime binding (protocol section 17).
3. Prove the authorization manifest is ACTIVE and bound to this exact run.
4. Check the independent review artifact:
   - absent  -> STOP at PASS_READY_FOR_INDEPENDENT_CREDENTIAL_RUNTIME_REVIEW
                (correct end state after implementation + self-review)
   - present but invalid -> hard STOP
   - present and valid -> proceed
5. Only when the review is valid, launch the credential-bearing child session
   via the credential-free supervisor. The child reads hidden credentials in its
   own TTY; this CLI and the supervisor never see a key or secret.
6. Emit a structured verdict + evidence and a structured exit code.

This CLI never pushes, merges, tags, releases, or contacts production. The
``--build-only`` / ``--preflight`` offline modes remain available via the
existing ``run_gate_1b_demo`` / ``run_gate_1b_prompted`` scripts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# Ensure ``src`` is importable when run as a plain script (matches pyproject
# ``pythonpath = ["src"]`` without requiring an installed package).
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from global_quant.gate1b.authorization import (  # noqa: E402
    AuthorizationError,
    read_manifest,
    validate_authorization_for_runtime,
)
from global_quant.gate1b.mutation_runner import (  # noqa: E402
    PROJECT_ROOT,
    MutationRunnerError,
    _verify_runtime_binding,
)
from global_quant.gate1b.review_artifact import (  # noqa: E402
    ReviewArtifactError,
    is_reviewed_for_runtime,
)
from global_quant.gate1b.supervisor import run_supervised_session  # noqa: E402

READY_FOR_REVIEW = "PASS_READY_FOR_INDEPENDENT_CREDENTIAL_RUNTIME_REVIEW"


def _write_evidence(evidence_dir: Path, payload: dict[str, Any]) -> Path:
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = None
    try:
        descriptor, tmp_name = tempfile.mkstemp(
            prefix=".cli-verdict-", suffix=".json", dir=str(evidence_dir)
        )
        tmp = Path(tmp_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, evidence_dir / "cli-verdict.json")
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()
    return evidence_dir / "cli-verdict.json"


def _base_payload(
    *,
    status: str,
    reason_codes: list[str],
    binding: dict[str, str],
) -> dict[str, Any]:
    return {
        "gate": "NT-GATE-1B",
        "protocol_version": "1.6",
        "mode": "CREDENTIAL_BEARING_DEMO_CLI",
        "status": status,
        "reason_codes": reason_codes,
        "credential_environment_empty": True,
        "credentials_read": False,
        "network_accessed": False,
        "authenticated_request_sent": False,
        "order_summary": {"canceled": 0, "filled": 0, "submitted": 0},
        "economic_event_summary": {"fees": 0, "funding": 0},
        "position_changes": 0,
        "agent_credential_access_allowed": False,
        "next_action": status,
        **binding,
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the NT-GATE-1B v1.6 credential-bearing Demo lifecycle CLI."
    )
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--runtime-commit", required=True)
    parser.add_argument("--session-nonce", required=True)
    parser.add_argument("--authorization-id", required=True)
    parser.add_argument("--protocol-commit", required=True)
    parser.add_argument("--protocol-tag-object", required=True)
    parser.add_argument("--protocol-sha256", required=True)
    parser.add_argument(
        "--authorization-manifest", type=Path, required=True,
        help="Owner-only local authorization manifest path (git-ignored).",
    )
    parser.add_argument(
        "--review-artifact", type=Path, default=None,
        help="Independent review artifact path. If absent, the CLI stops at "
        "PASS_READY_FOR_INDEPENDENT_CREDENTIAL_RUNTIME_REVIEW.",
    )
    parser.add_argument("--key-type", choices=("hmac", "ed25519"), default="hmac")
    parser.add_argument("--private-key-file", type=Path, default=None)
    parser.add_argument(
        "--child-python", default=sys.executable,
        help="Python interpreter for the credential-bearing child (default: this "
        "interpreter). Tests inject a fake child driver instead.",
    )
    parser.add_argument(
        "--dry-run-plumbing", action="store_true",
        help="Validate binding + authorization + review gating only, without "
        "launching the child. Used by self-review to prove the plumbing is sound.",
    )
    return parser.parse_args(argv)


def _child_argv(args: argparse.Namespace, evidence_dir: Path) -> list[str]:
    argv = [
        args.child_python,
        "-m",
        "global_quant.gate1b.credential_session",
        "--evidence-dir", str(evidence_dir),
        "--runtime-commit", args.runtime_commit,
        "--session-nonce", args.session_nonce,
        "--authorization-id", args.authorization_id,
        "--protocol-commit", args.protocol_commit,
        "--protocol-tag-object", args.protocol_tag_object,
        "--protocol-sha256", args.protocol_sha256,
        "--authorization-manifest", str(args.authorization_manifest),
        "--key-type", args.key_type,
    ]
    if args.private_key_file:
        argv.extend(["--private-key-file", str(args.private_key_file)])
    return argv


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    evidence_dir = Path(args.evidence_dir).resolve()
    binding = {
        "runtime_commit": args.runtime_commit,
        "session_nonce": args.session_nonce,
        "authorization_id": args.authorization_id,
        "protocol_commit": args.protocol_commit,
        "protocol_tag_object": args.protocol_tag_object,
        "protocol_sha256": args.protocol_sha256,
    }

    # Step 2: committed-runtime binding (protocol section 17).
    try:
        verified = _verify_runtime_binding(
            PROJECT_ROOT,
            runtime_commit=args.runtime_commit,
            protocol_commit=args.protocol_commit,
            protocol_tag_object=args.protocol_tag_object,
            protocol_sha256=args.protocol_sha256,
        )
    except MutationRunnerError as exc:
        path = _write_evidence(
            evidence_dir,
            _base_payload(status="STOP", reason_codes=[exc.reason], binding=binding),
        )
        print(json.dumps({"exit_code": 1, "evidence": str(path), "reason": exc.reason}))
        return 1
    binding.update(verified)

    # Step 3: one-time authorization manifest (protocol section 9).
    try:
        record = read_manifest(Path(args.authorization_manifest))
        validate_authorization_for_runtime(
            record,
            authorization_id=args.authorization_id,
            protocol_commit=args.protocol_commit,
            protocol_tag_object=args.protocol_tag_object,
            protocol_sha256=args.protocol_sha256,
            runtime_commit=args.runtime_commit,
        )
    except AuthorizationError as exc:
        path = _write_evidence(
            evidence_dir,
            _base_payload(status="STOP", reason_codes=[str(exc)], binding=binding),
        )
        print(json.dumps({"exit_code": 1, "evidence": str(path), "reason": str(exc)}))
        return 1

    # Step 4: independent review artifact (protocol section 19).
    review_path = Path(args.review_artifact) if args.review_artifact else None
    try:
        if review_path is None or not review_path.exists():
            reviewed = False
        else:
            reviewed = is_reviewed_for_runtime(
                review_path,
                runtime_commit=args.runtime_commit,
                protocol_commit=args.protocol_commit,
                protocol_tag_object=args.protocol_tag_object,
                protocol_sha256=args.protocol_sha256,
            )
    except ReviewArtifactError as exc:
        path = _write_evidence(
            evidence_dir,
            _base_payload(
                status="STOP", reason_codes=[f"REVIEW_ARTIFACT_INVALID:{exc}"], binding=binding
            ),
        )
        print(json.dumps({"exit_code": 1, "evidence": str(path), "reason": str(exc)}))
        return 1

    if not reviewed:
        # Correct end state after implementation + self-review: plumbing is
        # sound, but an independent reviewer has not yet produced a PASS
        # artifact. Stop here; do not read credentials.
        path = _write_evidence(
            evidence_dir,
            _base_payload(
                status=READY_FOR_REVIEW,
                reason_codes=["INDEPENDENT_REVIEW_ARTIFACT_ABSENT"],
                binding=binding,
            ),
        )
        print(
            json.dumps(
                {"exit_code": 0, "evidence": str(path), "status": READY_FOR_REVIEW}
            )
        )
        return 0

    if args.dry_run_plumbing:
        path = _write_evidence(
            evidence_dir,
            _base_payload(
                status="PASS_PLUMBING_VERIFIED_REVIEW_PRESENT",
                reason_codes=[],
                binding=binding,
            ),
        )
        print(json.dumps({"exit_code": 0, "evidence": str(path)}))
        return 0

    # Step 5: launch the credential-bearing child via the credential-free
    # supervisor. No credential value is ever visible to this CLI.
    child_argv = _child_argv(args, evidence_dir)
    verdict_code, verdict_path = run_supervised_session(
        evidence_dir=evidence_dir,
        binding=binding,
        child_argv=child_argv,
    )
    print(
        json.dumps(
            {"exit_code": verdict_code, "evidence": str(verdict_path)},
            sort_keys=True,
        )
    )
    return verdict_code


if __name__ == "__main__":
    raise SystemExit(main())
