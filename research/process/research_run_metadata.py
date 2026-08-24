"""Small, fail-closed provenance record for a completed research artifact.

This module is deliberately independent from historical runners.  It records
identity and hashes; it neither approves nor creates a formal run.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
from typing import Final


REQUIRED_KEYS: Final = (
    "HYPOTHESIS_ID",
    "IMPLEMENTATION_ID",
    "FORMAL_RUN_ID",
    "DATASET_SHA",
    "CODE_SHA",
    "CONFIG_SHA",
    "RESULT_SHA",
)
_GIT_SHA = re.compile(r"[0-9a-f]{40}")


class MetadataError(ValueError):
    """The provenance record cannot be made from unambiguous inputs."""


def _identifier(name: str, value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise MetadataError(f"invalid {name}")
    return value


def sha256_file(path: str | pathlib.Path) -> str:
    """Hash one explicit, regular input file; directories and links are rejected."""
    candidate = pathlib.Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise MetadataError(f"required regular file missing: {candidate}")
    digest = hashlib.sha256()
    try:
        with candidate.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise MetadataError(f"cannot hash required file: {candidate}") from error
    return digest.hexdigest()


def current_git_head(repo_root: str | pathlib.Path | None = None) -> str:
    """Resolve a clean checked-out commit, rather than trusting caller input."""
    cwd = pathlib.Path(repo_root) if repo_root is not None else pathlib.Path.cwd()
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise MetadataError("git HEAD cannot be resolved") from error
    value = completed.stdout.strip()
    if completed.returncode != 0 or not _GIT_SHA.fullmatch(value):
        raise MetadataError("git HEAD cannot be resolved")
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0 or status.stdout:
        raise MetadataError("git worktree must be clean to bind CODE_SHA")
    return value


def build_run_metadata(
    *,
    hypothesis_id: str,
    implementation_id: str,
    formal_run_id: str,
    dataset_path: str | pathlib.Path,
    config_path: str | pathlib.Path,
    result_path: str | pathlib.Path,
    repo_root: str | pathlib.Path | None = None,
) -> dict[str, str]:
    """Return the canonical record for explicit, already-existing artifacts.

    ``CODE_SHA`` is the content-addressed current Git commit.  The other SHA
    values are SHA-256 hashes of the three explicit regular files.  A caller
    must obtain formal admission separately; this function has no gate or
    side effects.
    """
    record = {
        "HYPOTHESIS_ID": _identifier("HYPOTHESIS_ID", hypothesis_id),
        "IMPLEMENTATION_ID": _identifier("IMPLEMENTATION_ID", implementation_id),
        "FORMAL_RUN_ID": _identifier("FORMAL_RUN_ID", formal_run_id),
        "DATASET_SHA": sha256_file(dataset_path),
        "CODE_SHA": current_git_head(repo_root),
        "CONFIG_SHA": sha256_file(config_path),
        "RESULT_SHA": sha256_file(result_path),
    }
    if tuple(record) != REQUIRED_KEYS:
        raise MetadataError("metadata schema drift")
    return record


def emit_run_metadata(destination: str | pathlib.Path, **kwargs: object) -> dict[str, str]:
    """Exclusively create one JSON record, refusing every replacement race."""
    output = pathlib.Path(destination)
    if output.exists() or output.is_symlink() or output.parent.is_symlink() or not output.parent.is_dir():
        raise MetadataError(f"metadata destination is unsafe: {output}")
    record = build_run_metadata(**kwargs)  # type: ignore[arg-type]
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise MetadataError(f"metadata destination is unsafe: {output}") from error
    except OSError as error:
        raise MetadataError(f"metadata destination is unwritable: {output}") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        raise MetadataError(f"metadata destination is unwritable: {output}") from error
    return record
