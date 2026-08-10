"""Independent review artifact schema, loader, and verifier for the frozen
NT-GATE-1B v1.6 credential-bearing mutation runtime.

Protocol section 18 (acceptance) and section 19 (verdict) require an independent
read-only review before a credential-bearing mutation runtime PASS. The
implementer must not fabricate reviewer approval. This module only:

* defines the review artifact schema,
* loads and validates a review artifact from disk,
* mechanically proves the artifact binds to the exact current runtime HEAD and
  frozen protocol identity, with ``P0 == 0`` and ``P1 == 0``.

A real review artifact is produced by a separate independent reviewer; tests use
synthetic fixtures. This module never grants PASS itself — it only verifies a
reviewer-produced artifact is structurally valid and bound to this run.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VALID_VERDICTS = frozenset(
    {
        "PASS_GATE1B_V1_6_DEMO_RUNTIME",
        "PASS_READY_FOR_INDEPENDENT_CREDENTIAL_RUNTIME_REVIEW",
        "BLOCKED",
        "STOP",
    }
)


class ReviewArtifactError(RuntimeError):
    """Raised when a review artifact is missing, malformed, or unbound."""


@dataclass(frozen=True)
class ReviewArtifact:
    reviewer_identity: str
    reviewed_head: str
    protocol_commit: str
    protocol_tag_object: str
    protocol_sha256: str
    p0: int
    p1: int
    p2: int
    p3: int
    verdict: str
    reviewed_at: str
    notes: tuple[str, ...]

    def to_json(self) -> str:
        return (
            json.dumps(
                {
                    "reviewer_identity": self.reviewer_identity,
                    "reviewed_head": self.reviewed_head,
                    "protocol_commit": self.protocol_commit,
                    "protocol_tag_object": self.protocol_tag_object,
                    "protocol_sha256": self.protocol_sha256,
                    "findings": {"p0": self.p0, "p1": self.p1, "p2": self.p2, "p3": self.p3},
                    "verdict": self.verdict,
                    "reviewed_at": self.reviewed_at,
                    "notes": list(self.notes),
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )

    @classmethod
    def from_mapping(cls, data: dict[str, object]) -> ReviewArtifact:
        required = (
            "reviewer_identity",
            "reviewed_head",
            "protocol_commit",
            "protocol_tag_object",
            "protocol_sha256",
            "findings",
            "verdict",
            "reviewed_at",
        )
        missing = [k for k in required if k not in data]
        if missing:
            raise ReviewArtifactError(f"REVIEW_ARTIFACT_MISSING_FIELDS:{missing}")
        findings = data["findings"]
        if not isinstance(findings, dict):
            raise ReviewArtifactError("REVIEW_ARTIFACT_MALFORMED_FINDINGS")
        for key in ("p0", "p1", "p2", "p3"):
            value = findings.get(key)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ReviewArtifactError(f"REVIEW_ARTIFACT_INVALID_FINDING:{key}")
        notes = data.get("notes", [])
        if not isinstance(notes, list):
            raise ReviewArtifactError("REVIEW_ARTIFACT_MALFORMED_NOTES")
        return cls(
            reviewer_identity=str(data["reviewer_identity"]),
            reviewed_head=str(data["reviewed_head"]),
            protocol_commit=str(data["protocol_commit"]),
            protocol_tag_object=str(data["protocol_tag_object"]),
            protocol_sha256=str(data["protocol_sha256"]),
            p0=int(findings["p0"]),
            p1=int(findings["p1"]),
            p2=int(findings["p2"]),
            p3=int(findings["p3"]),
            verdict=str(data["verdict"]),
            reviewed_at=str(data["reviewed_at"]),
            notes=tuple(str(n) for n in notes),
        )


def load_review_artifact(path: Path) -> ReviewArtifact:
    """Load and structurally validate a review artifact from disk.

    Refuses symlinks and insecure permissions. A missing, malformed, or
    tampered artifact fails closed.
    """

    path = Path(path)
    if not path.exists():
        raise ReviewArtifactError("REVIEW_ARTIFACT_MISSING")
    if path.is_symlink():
        raise ReviewArtifactError("REVIEW_ARTIFACT_IS_SYMLINK")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ReviewArtifactError("REVIEW_ARTIFACT_NOT_REGULAR")
        if metadata.st_uid != os.getuid():
            raise ReviewArtifactError("REVIEW_ARTIFACT_OWNER_MISMATCH")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ReviewArtifactError("REVIEW_ARTIFACT_INSECURE_PERMISSIONS")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
            raw = handle.read()
    finally:
        os.close(descriptor)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ReviewArtifactError("REVIEW_ARTIFACT_MALFORMED_JSON") from exc
    if not isinstance(data, dict):
        raise ReviewArtifactError("REVIEW_ARTIFACT_MALFORMED_JSON")
    artifact = ReviewArtifact.from_mapping(data)
    if not artifact.reviewer_identity.strip():
        raise ReviewArtifactError("REVIEW_ARTIFACT_EMPTY_REVIEWER")
    if not _GIT_COMMIT.match(artifact.reviewed_head):
        raise ReviewArtifactError("REVIEW_ARTIFACT_INVALID_HEAD")
    if not _GIT_COMMIT.match(artifact.protocol_commit):
        raise ReviewArtifactError("REVIEW_ARTIFACT_INVALID_PROTOCOL_COMMIT")
    if not _GIT_COMMIT.match(artifact.protocol_tag_object):
        raise ReviewArtifactError("REVIEW_ARTIFACT_INVALID_TAG_OBJECT")
    if not _SHA256.match(artifact.protocol_sha256):
        raise ReviewArtifactError("REVIEW_ARTIFACT_INVALID_PROTOCOL_SHA256")
    if artifact.verdict not in _VALID_VERDICTS:
        raise ReviewArtifactError(f"REVIEW_ARTIFACT_INVALID_VERDICT:{artifact.verdict}")
    return artifact


def validate_review_for_runtime(
    artifact: ReviewArtifact,
    *,
    runtime_commit: str,
    protocol_commit: str,
    protocol_tag_object: str,
    protocol_sha256: str,
) -> ReviewArtifact:
    """Prove the review artifact binds to the exact current runtime + protocol.

    A reviewer cannot PASS a different HEAD, a different protocol commit, tag
    object, or protocol hash. Any mismatch, a non-zero P0/P1, or a non-PASS
    verdict fails closed.
    """

    if artifact.reviewed_head != runtime_commit:
        raise ReviewArtifactError("REVIEW_ARTIFACT_HEAD_MISMATCH")
    if artifact.protocol_commit != protocol_commit:
        raise ReviewArtifactError("REVIEW_ARTIFACT_PROTOCOL_COMMIT_MISMATCH")
    if artifact.protocol_tag_object != protocol_tag_object:
        raise ReviewArtifactError("REVIEW_ARTIFACT_TAG_OBJECT_MISMATCH")
    if artifact.protocol_sha256 != protocol_sha256:
        raise ReviewArtifactError("REVIEW_ARTIFACT_PROTOCOL_HASH_MISMATCH")
    if artifact.p0 != 0 or artifact.p1 != 0:
        raise ReviewArtifactError(
            f"REVIEW_ARTIFACT_NONZERO_FINDINGS:p0={artifact.p0}:p1={artifact.p1}"
        )
    if artifact.verdict != "PASS_GATE1B_V1_6_DEMO_RUNTIME":
        raise ReviewArtifactError(f"REVIEW_ARTIFACT_NON_PASS_VERDICT:{artifact.verdict}")
    return artifact


def is_reviewed_for_runtime(
    path: Path,
    *,
    runtime_commit: str,
    protocol_commit: str,
    protocol_tag_object: str,
    protocol_sha256: str,
) -> bool:
    """Convenience predicate used by the CLI to gate the credential phase.

    Returns ``False`` (not a STOP) when the artifact is simply absent; the CLI
    then stops at ``PASS_READY_FOR_INDEPENDENT_CREDENTIAL_RUNTIME_REVIEW``. A
    present-but-invalid artifact raises and forces a hard STOP.
    """

    path = Path(path)
    if not path.exists():
        return False
    artifact = load_review_artifact(path)
    validate_review_for_runtime(
        artifact,
        runtime_commit=runtime_commit,
        protocol_commit=protocol_commit,
        protocol_tag_object=protocol_tag_object,
        protocol_sha256=protocol_sha256,
    )
    return True


def write_synthetic_artifact(path: Path, *, runtime_commit: str, protocol_commit: str,
                             protocol_tag_object: str, protocol_sha256: str,
                             reviewer: str = "synthetic-test-reviewer") -> Path:
    """Write a synthetic PASS artifact for tests only.

    Production code must never call this; it exists so the verifier can be
    exercised against a known-good fixture. Real artifacts are produced by an
    independent reviewer.
    """

    artifact = ReviewArtifact(
        reviewer_identity=reviewer,
        reviewed_head=runtime_commit,
        protocol_commit=protocol_commit,
        protocol_tag_object=protocol_tag_object,
        protocol_sha256=protocol_sha256,
        p0=0,
        p1=0,
        p2=0,
        p3=0,
        verdict="PASS_GATE1B_V1_6_DEMO_RUNTIME",
        reviewed_at=datetime.now(UTC).isoformat(),
        notes=("synthetic fixture for verifier tests",),
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(artifact.to_json(), encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


# Kept for datetime import symmetry (reviewed_at is an ISO string).
__all__ = (
    "ReviewArtifact",
    "ReviewArtifactError",
    "is_reviewed_for_runtime",
    "load_review_artifact",
    "validate_review_for_runtime",
    "write_synthetic_artifact",
)
