#!/usr/bin/env python3
"""Verify one independent Gate 1B v1.9 acceptance artifact offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PROTOCOL_PATH = "protocols/NT_GATE_1B_V1_9.md"
_GIT_EXECUTABLE = "/usr/bin/git"
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from global_quant.gate1b.review_artifact import (  # noqa: E402
    V1_9_PROTOCOL_VERSION,
    ReviewArtifactError,
    load_review_artifact,
    validate_review_for_v1_9_acceptance,
)


class V19AcceptanceVerificationError(RuntimeError):
    """The current checkout cannot provide a trustworthy v1.9 context."""


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
        raise V19AcceptanceVerificationError("V1_9_ACCEPTANCE_GIT_STATE_INVALID") from exc


def _candidate_identity() -> tuple[str, str]:
    dirty = _git("diff", "--quiet", "--no-ext-diff", "HEAD", "--", check=False)
    if dirty.returncode == 1:
        raise V19AcceptanceVerificationError("V1_9_ACCEPTANCE_CHECKOUT_DIRTY")
    if dirty.returncode != 0:
        raise V19AcceptanceVerificationError("V1_9_ACCEPTANCE_GIT_STATE_INVALID")
    if _git("ls-files", "--others", "--exclude-standard", "-z").stdout:
        raise V19AcceptanceVerificationError("V1_9_ACCEPTANCE_UNTRACKED_FILES_PRESENT")
    try:
        candidate = _git("rev-parse", "--verify", "HEAD^{commit}").stdout.decode("ascii").strip()
        protocol = _git("show", f"{candidate}:{_PROTOCOL_PATH}").stdout
    except UnicodeError as exc:
        raise V19AcceptanceVerificationError("V1_9_ACCEPTANCE_GIT_STATE_INVALID") from exc
    return candidate, hashlib.sha256(protocol).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        candidate, protocol_sha256 = _candidate_identity()
        artifact = load_review_artifact(args.artifact)
        validate_review_for_v1_9_acceptance(
            artifact,
            candidate_commit=candidate,
            protocol_sha256=protocol_sha256,
        )
    except (ReviewArtifactError, V19AcceptanceVerificationError) as exc:
        print(json.dumps({"result": "REJECT", "reason": str(exc)}, sort_keys=True))
        return 1
    print(
        json.dumps(
            {
                "protocol_version": V1_9_PROTOCOL_VERSION,
                "result": "PASS",
                "reviewed_head": candidate,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
