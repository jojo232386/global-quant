#!/usr/bin/env python3
"""Verify one version-aware Gate 1B acceptance artifact offline."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_GIT_EXECUTABLE = "/usr/bin/git"
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from global_quant.gate1b.review_artifact import (  # noqa: E402
    ACTIVE_ACCEPTANCE_DECLARATION_PATH,
    ReviewArtifactError,
    TrustedExpectedContext,
    build_trusted_expected_context,
    load_active_acceptance_declaration,
    load_review_artifact,
    validate_review_for_acceptance,
)


class AcceptanceVerificationError(RuntimeError):
    """The requested Git object cannot provide a trusted expected context."""


def _git(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    environment = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    try:
        return subprocess.run(
            [_GIT_EXECUTABLE, *arguments],
            cwd=_PROJECT_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=check,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AcceptanceVerificationError("ACCEPTANCE_GIT_STATE_INVALID") from exc


def _trusted_expected_context(
    *,
    candidate: str,
    expected_protocol_version: str,
) -> TrustedExpectedContext:
    if not _GIT_COMMIT.fullmatch(candidate):
        raise AcceptanceVerificationError("EXPECTED_CANDIDATE_INVALID")
    resolved = _git("rev-parse", "--verify", f"{candidate}^{{commit}}").stdout
    try:
        if resolved.decode("ascii").strip() != candidate:
            raise AcceptanceVerificationError("EXPECTED_CANDIDATE_INVALID")
        declaration_content = _git(
            "show",
            f"{candidate}:{ACTIVE_ACCEPTANCE_DECLARATION_PATH}",
        ).stdout
        declaration = load_active_acceptance_declaration(
            declaration_content,
            expected_protocol_version=expected_protocol_version,
        )
        protocol_content = _git("show", f"{candidate}:{declaration.protocol_path}").stdout
    except UnicodeError as exc:
        raise AcceptanceVerificationError("ACCEPTANCE_GIT_STATE_INVALID") from exc
    return build_trusted_expected_context(
        declaration,
        reviewed_head=candidate,
        protocol_content=protocol_content,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--expected-protocol-version", required=True)
    args = parser.parse_args(argv)
    try:
        expected = _trusted_expected_context(
            candidate=args.candidate,
            expected_protocol_version=args.expected_protocol_version,
        )
        artifact = load_review_artifact(args.artifact)
        validate_review_for_acceptance(artifact, expected=expected)
    except (AcceptanceVerificationError, ReviewArtifactError) as exc:
        print(json.dumps({"result": "REJECT", "reason": str(exc)}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "artifact_schema_version": expected.artifact_schema_version,
                "protocol_identity": expected.protocol_identity,
                "protocol_version": expected.protocol_version,
                "result": "PASS",
                "reviewed_head": expected.reviewed_head,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
