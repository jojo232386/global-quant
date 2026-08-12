"""Independent Gate 1B review artifacts and fail-closed acceptance binding.

There are three deliberately separate trust domains:

* the historical v1.6 runtime verifier, which retains its tag-bound schema;
* the historical v1.9 acceptance verifier, which retains its exact schema;
* the version-aware acceptance verifier used from v1.10 onward.

For the version-aware path, a reviewer supplies an exact candidate SHA and an
expected protocol version.  A tracked declaration and protocol document are
then read from that candidate's Git tree to construct
``TrustedExpectedContext``.  The artifact is only compared with that context;
none of its fields can select the expected version, candidate, verdict, or
protocol identity.

Real artifacts remain the responsibility of a fresh independent reviewer.
Tests use explicitly synthetic fixtures.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROTOCOL_VERSION = re.compile(r"^[1-9][0-9]*\.[0-9]+$")
_PROTOCOL_PATH = re.compile(r"^protocols/NT_GATE_1B_V[1-9][0-9]*_[0-9]+\.md$")
_VERDICT = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")

ACTIVE_ACCEPTANCE_DECLARATION_PATH = "protocols/NT_GATE_1B_ACTIVE_ACCEPTANCE.json"
ACTIVE_ACCEPTANCE_DECLARATION_SCHEMA_VERSION = 1
ARTIFACT_SCHEMA_VERSION = "1"
LEGACY_V1_6_PROTOCOL_VERSION = "1.6"
V1_9_PROTOCOL_VERSION = "1.9"
V1_10_PROTOCOL_VERSION = "1.10"
V1_6_PASS_VERDICT = "PASS_GATE1B_V1_6_DEMO_RUNTIME"
V1_9_PASS_VERDICT = "PASS_GATE1B_V1_9_READ_ONLY_PREFLIGHT"
V1_10_PASS_VERDICT = "PASS_GATE1B_V1_10_READ_ONLY_DIAGNOSTICS"


class ReviewArtifactError(RuntimeError):
    """Raised when a review artifact or expected context fails closed."""


def _canonical_artifact_digest(payload: dict[str, object]) -> str:
    """Return SHA-256 over every canonical artifact field except itself."""

    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _load_json_object(raw: str, *, reason: str) -> dict[str, object]:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (TypeError, ValueError) as exc:
        raise ReviewArtifactError(reason) from exc
    if type(value) is not dict:
        raise ReviewArtifactError(reason)
    return value


def _require_string(data: dict[str, object], key: str, *, reason: str) -> str:
    value = data.get(key)
    if type(value) is not str:
        raise ReviewArtifactError(reason)
    return value


@dataclass(frozen=True, slots=True)
class ActiveAcceptanceDeclaration:
    """One candidate tree's sole active machine-acceptance declaration."""

    protocol_version: str
    artifact_schema_version: str
    protocol_path: str
    pass_verdict: str


def load_active_acceptance_declaration(
    content: bytes,
    *,
    expected_protocol_version: str,
) -> ActiveAcceptanceDeclaration:
    """Parse the tracked active declaration without consulting an artifact.

    The declaration intentionally describes exactly one active protocol.  It is
    not a registry from which an artifact can select an older or future entry.
    """

    if type(expected_protocol_version) is not str or not _PROTOCOL_VERSION.fullmatch(
        expected_protocol_version
    ):
        raise ReviewArtifactError("EXPECTED_PROTOCOL_VERSION_INVALID")
    try:
        raw = content.decode("utf-8")
    except UnicodeError as exc:
        raise ReviewArtifactError("EXPECTED_ACCEPTANCE_DECLARATION_INVALID") from exc
    data = _load_json_object(raw, reason="EXPECTED_ACCEPTANCE_DECLARATION_INVALID")
    required = {
        "manifest_schema_version",
        "protocol_version",
        "artifact_schema_version",
        "protocol_path",
        "pass_verdict",
    }
    if set(data) != required:
        raise ReviewArtifactError("EXPECTED_ACCEPTANCE_DECLARATION_FIELDS_INVALID")
    if (
        type(data["manifest_schema_version"]) is not int
        or data["manifest_schema_version"] != ACTIVE_ACCEPTANCE_DECLARATION_SCHEMA_VERSION
    ):
        raise ReviewArtifactError("EXPECTED_ACCEPTANCE_DECLARATION_SCHEMA_UNSUPPORTED")
    protocol_version = _require_string(
        data,
        "protocol_version",
        reason="EXPECTED_ACCEPTANCE_DECLARATION_INVALID",
    )
    artifact_schema_version = _require_string(
        data,
        "artifact_schema_version",
        reason="EXPECTED_ACCEPTANCE_DECLARATION_INVALID",
    )
    protocol_path = _require_string(
        data,
        "protocol_path",
        reason="EXPECTED_ACCEPTANCE_DECLARATION_INVALID",
    )
    pass_verdict = _require_string(
        data,
        "pass_verdict",
        reason="EXPECTED_ACCEPTANCE_DECLARATION_INVALID",
    )
    if protocol_version != expected_protocol_version:
        raise ReviewArtifactError("EXPECTED_PROTOCOL_VERSION_UNDECLARED")
    if artifact_schema_version != ARTIFACT_SCHEMA_VERSION:
        raise ReviewArtifactError("EXPECTED_ARTIFACT_SCHEMA_UNSUPPORTED")
    expected_protocol_path = f"protocols/NT_GATE_1B_V{protocol_version.replace('.', '_')}.md"
    if not _PROTOCOL_PATH.fullmatch(protocol_path) or protocol_path != expected_protocol_path:
        raise ReviewArtifactError("EXPECTED_PROTOCOL_PATH_INVALID")
    versioned_pass_prefix = f"PASS_GATE1B_V{protocol_version.replace('.', '_')}_"
    if not _VERDICT.fullmatch(pass_verdict) or not pass_verdict.startswith(versioned_pass_prefix):
        raise ReviewArtifactError("EXPECTED_PASS_VERDICT_INVALID")
    return ActiveAcceptanceDeclaration(
        protocol_version=protocol_version,
        artifact_schema_version=artifact_schema_version,
        protocol_path=protocol_path,
        pass_verdict=pass_verdict,
    )


@dataclass(frozen=True, slots=True)
class TrustedExpectedContext:
    """Trusted acceptance identity built independently of the artifact."""

    artifact_schema_version: str | None
    protocol_version: str
    reviewed_head: str
    protocol_identity: str
    pass_verdict: str

    def __post_init__(self) -> None:
        if self.artifact_schema_version not in (None, ARTIFACT_SCHEMA_VERSION):
            raise ReviewArtifactError("EXPECTED_ARTIFACT_SCHEMA_UNSUPPORTED")
        if self.artifact_schema_version is None and self.protocol_version != V1_9_PROTOCOL_VERSION:
            raise ReviewArtifactError("EXPECTED_LEGACY_ACCEPTANCE_CONTEXT_INVALID")
        if not _PROTOCOL_VERSION.fullmatch(self.protocol_version):
            raise ReviewArtifactError("EXPECTED_PROTOCOL_VERSION_INVALID")
        if not _GIT_COMMIT.fullmatch(self.reviewed_head):
            raise ReviewArtifactError("EXPECTED_CANDIDATE_INVALID")
        if not _SHA256.fullmatch(self.protocol_identity):
            raise ReviewArtifactError("EXPECTED_PROTOCOL_IDENTITY_INVALID")
        if not _VERDICT.fullmatch(self.pass_verdict):
            raise ReviewArtifactError("EXPECTED_PASS_VERDICT_INVALID")


def build_trusted_expected_context(
    declaration: ActiveAcceptanceDeclaration,
    *,
    reviewed_head: str,
    protocol_content: bytes,
) -> TrustedExpectedContext:
    """Bind a tracked declaration and document to one exact candidate SHA."""

    if type(protocol_content) is not bytes:
        raise ReviewArtifactError("EXPECTED_PROTOCOL_CONTENT_INVALID")
    return TrustedExpectedContext(
        artifact_schema_version=declaration.artifact_schema_version,
        protocol_version=declaration.protocol_version,
        reviewed_head=reviewed_head,
        protocol_identity=hashlib.sha256(protocol_content).hexdigest(),
        pass_verdict=declaration.pass_verdict,
    )


@dataclass(frozen=True)
class ReviewArtifact:
    reviewer_identity: str
    reviewed_head: str
    protocol_commit: str
    protocol_tag_object: str | None
    protocol_sha256: str
    p0: int
    p1: int
    p2: int
    p3: int
    verdict: str
    reviewed_at: str
    notes: tuple[str, ...]
    protocol_version: str | None = None
    artifact_sha256: str | None = None
    artifact_schema_version: str | None = None

    @property
    def protocol_identity(self) -> str:
        """The existing protocol content digest, exposed by trust-model name."""

        return self.protocol_sha256

    def _uses_canonical_digest(self) -> bool:
        return (
            self.protocol_version == V1_9_PROTOCOL_VERSION and self.artifact_schema_version is None
        ) or self.artifact_schema_version == ARTIFACT_SCHEMA_VERSION

    def _payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "reviewer_identity": self.reviewer_identity,
            "reviewed_head": self.reviewed_head,
            "protocol_commit": self.protocol_commit,
            "protocol_sha256": self.protocol_sha256,
            "findings": {"p0": self.p0, "p1": self.p1, "p2": self.p2, "p3": self.p3},
            "verdict": self.verdict,
            "reviewed_at": self.reviewed_at,
            "notes": list(self.notes),
        }
        if self.protocol_tag_object is not None:
            payload["protocol_tag_object"] = self.protocol_tag_object
        if self.protocol_version is not None:
            payload["protocol_version"] = self.protocol_version
        if self.artifact_schema_version is not None:
            payload["artifact_schema_version"] = self.artifact_schema_version
        return payload

    def to_json(self) -> str:
        payload = self._payload()
        if self._uses_canonical_digest():
            if self.protocol_tag_object is not None:
                raise ReviewArtifactError("REVIEW_ARTIFACT_ACCEPTANCE_TAG_OBJECT_FORBIDDEN")
            if self.protocol_version is None:
                raise ReviewArtifactError("REVIEW_ARTIFACT_MISSING_PROTOCOL_VERSION")
            payload["artifact_sha256"] = _canonical_artifact_digest(payload)
        elif self.artifact_schema_version is not None:
            raise ReviewArtifactError("REVIEW_ARTIFACT_INVALID_SCHEMA_VERSION")
        elif self.protocol_version not in (None, LEGACY_V1_6_PROTOCOL_VERSION):
            raise ReviewArtifactError("REVIEW_ARTIFACT_MISSING_SCHEMA_VERSION")
        elif self.artifact_sha256 is not None:
            raise ReviewArtifactError("REVIEW_ARTIFACT_LEGACY_DIGEST_FORBIDDEN")
        return json.dumps(payload, sort_keys=True, indent=2) + "\n"

    @classmethod
    def from_mapping(cls, data: dict[str, object]) -> ReviewArtifact:
        protocol_version_value = data.get("protocol_version")
        if protocol_version_value is not None and type(protocol_version_value) is not str:
            raise ReviewArtifactError("REVIEW_ARTIFACT_INVALID_PROTOCOL_VERSION")
        protocol_version = protocol_version_value
        schema_value = data.get("artifact_schema_version")
        if schema_value is not None and type(schema_value) is not str:
            raise ReviewArtifactError("REVIEW_ARTIFACT_INVALID_SCHEMA_VERSION")
        artifact_schema_version = schema_value

        modern = artifact_schema_version is not None
        legacy_v1_9 = artifact_schema_version is None and protocol_version == V1_9_PROTOCOL_VERSION
        legacy_v1_6 = artifact_schema_version is None and protocol_version in (
            None,
            LEGACY_V1_6_PROTOCOL_VERSION,
        )
        if modern:
            if artifact_schema_version != ARTIFACT_SCHEMA_VERSION:
                raise ReviewArtifactError("REVIEW_ARTIFACT_INVALID_SCHEMA_VERSION")
            if protocol_version is None or not _PROTOCOL_VERSION.fullmatch(protocol_version):
                raise ReviewArtifactError("REVIEW_ARTIFACT_INVALID_PROTOCOL_VERSION")
        elif not legacy_v1_9 and not legacy_v1_6:
            raise ReviewArtifactError("REVIEW_ARTIFACT_INVALID_PROTOCOL_VERSION")

        required = {
            "reviewer_identity",
            "reviewed_head",
            "protocol_commit",
            "protocol_sha256",
            "findings",
            "verdict",
            "reviewed_at",
        }
        if modern or legacy_v1_9:
            required.update({"protocol_version", "artifact_sha256"})
            if modern:
                required.add("artifact_schema_version")
            allowed = required | {"notes"}
            unexpected = sorted(set(data).difference(allowed))
            if unexpected:
                raise ReviewArtifactError(f"REVIEW_ARTIFACT_UNEXPECTED_FIELDS:{unexpected}")
        else:
            required.add("protocol_tag_object")
            if "artifact_sha256" in data:
                raise ReviewArtifactError("REVIEW_ARTIFACT_LEGACY_DIGEST_FORBIDDEN")
        missing = sorted(required.difference(data))
        if missing:
            raise ReviewArtifactError(f"REVIEW_ARTIFACT_MISSING_FIELDS:{missing}")

        string_fields = (
            "reviewer_identity",
            "reviewed_head",
            "protocol_commit",
            "protocol_sha256",
            "verdict",
            "reviewed_at",
        )
        for key in string_fields:
            if type(data[key]) is not str:
                raise ReviewArtifactError(f"REVIEW_ARTIFACT_INVALID_FIELD:{key}")
        if legacy_v1_6 and type(data["protocol_tag_object"]) is not str:
            raise ReviewArtifactError("REVIEW_ARTIFACT_INVALID_FIELD:protocol_tag_object")

        findings = data["findings"]
        if type(findings) is not dict:
            raise ReviewArtifactError("REVIEW_ARTIFACT_MALFORMED_FINDINGS")
        if (modern or legacy_v1_9) and set(findings) != {"p0", "p1", "p2", "p3"}:
            raise ReviewArtifactError("REVIEW_ARTIFACT_MALFORMED_FINDINGS")
        for key in ("p0", "p1", "p2", "p3"):
            value = findings.get(key)
            if type(value) is not int or value < 0:
                raise ReviewArtifactError(f"REVIEW_ARTIFACT_INVALID_FINDING:{key}")
        notes = data.get("notes", [])
        if type(notes) is not list or any(type(note) is not str for note in notes):
            raise ReviewArtifactError("REVIEW_ARTIFACT_MALFORMED_NOTES")
        return cls(
            reviewer_identity=data["reviewer_identity"],
            reviewed_head=data["reviewed_head"],
            protocol_commit=data["protocol_commit"],
            protocol_tag_object=(data["protocol_tag_object"] if legacy_v1_6 else None),
            protocol_sha256=data["protocol_sha256"],
            p0=findings["p0"],
            p1=findings["p1"],
            p2=findings["p2"],
            p3=findings["p3"],
            verdict=data["verdict"],
            reviewed_at=data["reviewed_at"],
            notes=tuple(notes),
            protocol_version=protocol_version,
            artifact_sha256=(data["artifact_sha256"] if modern or legacy_v1_9 else None),
            artifact_schema_version=artifact_schema_version,
        )


def load_review_artifact(path: Path) -> ReviewArtifact:
    """Load an owner-only regular artifact, refusing links and tampering."""

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
    data = _load_json_object(raw, reason="REVIEW_ARTIFACT_MALFORMED_JSON")
    artifact = ReviewArtifact.from_mapping(data)
    if not artifact.reviewer_identity.strip():
        raise ReviewArtifactError("REVIEW_ARTIFACT_EMPTY_REVIEWER")
    if not _GIT_COMMIT.fullmatch(artifact.reviewed_head):
        raise ReviewArtifactError("REVIEW_ARTIFACT_INVALID_HEAD")
    if not _GIT_COMMIT.fullmatch(artifact.protocol_commit):
        raise ReviewArtifactError("REVIEW_ARTIFACT_INVALID_PROTOCOL_COMMIT")
    if artifact.protocol_tag_object is not None and not _GIT_COMMIT.fullmatch(
        artifact.protocol_tag_object
    ):
        raise ReviewArtifactError("REVIEW_ARTIFACT_INVALID_TAG_OBJECT")
    if not _SHA256.fullmatch(artifact.protocol_sha256):
        raise ReviewArtifactError("REVIEW_ARTIFACT_INVALID_PROTOCOL_SHA256")
    if artifact._uses_canonical_digest():
        if artifact.protocol_tag_object is not None:
            raise ReviewArtifactError("REVIEW_ARTIFACT_ACCEPTANCE_TAG_OBJECT_FORBIDDEN")
        if type(artifact.artifact_sha256) is not str or not _SHA256.fullmatch(
            artifact.artifact_sha256
        ):
            raise ReviewArtifactError("REVIEW_ARTIFACT_INVALID_ARTIFACT_SHA256")
        if artifact.artifact_sha256 != _canonical_artifact_digest(artifact._payload()):
            raise ReviewArtifactError("REVIEW_ARTIFACT_DIGEST_MISMATCH")
    if not _VERDICT.fullmatch(artifact.verdict):
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
    """Preserve the historical v1.6 runtime/tag verification contract."""

    if artifact.protocol_version not in (None, LEGACY_V1_6_PROTOCOL_VERSION):
        raise ReviewArtifactError("REVIEW_ARTIFACT_PROTOCOL_VERSION_MISMATCH")
    if artifact.artifact_schema_version is not None:
        raise ReviewArtifactError("REVIEW_ARTIFACT_SCHEMA_VERSION_MISMATCH")
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
    if artifact.verdict != V1_6_PASS_VERDICT:
        raise ReviewArtifactError(f"REVIEW_ARTIFACT_NON_PASS_VERDICT:{artifact.verdict}")
    return artifact


def validate_review_for_acceptance(
    artifact: ReviewArtifact,
    *,
    expected: TrustedExpectedContext,
) -> ReviewArtifact:
    """Compare an artifact to context that was built without reading it."""

    if artifact.artifact_schema_version != expected.artifact_schema_version:
        raise ReviewArtifactError("REVIEW_ARTIFACT_SCHEMA_VERSION_MISMATCH")
    if artifact.protocol_version != expected.protocol_version:
        raise ReviewArtifactError("REVIEW_ARTIFACT_PROTOCOL_VERSION_MISMATCH")
    if artifact.protocol_tag_object is not None:
        raise ReviewArtifactError("REVIEW_ARTIFACT_ACCEPTANCE_TAG_OBJECT_FORBIDDEN")
    if artifact.reviewed_head != expected.reviewed_head:
        raise ReviewArtifactError("REVIEW_ARTIFACT_HEAD_MISMATCH")
    if artifact.protocol_commit != expected.reviewed_head:
        raise ReviewArtifactError("REVIEW_ARTIFACT_PROTOCOL_COMMIT_MISMATCH")
    if artifact.protocol_identity != expected.protocol_identity:
        raise ReviewArtifactError("REVIEW_ARTIFACT_PROTOCOL_HASH_MISMATCH")
    if type(artifact.artifact_sha256) is not str:
        raise ReviewArtifactError("REVIEW_ARTIFACT_INVALID_ARTIFACT_SHA256")
    if artifact.artifact_sha256 != _canonical_artifact_digest(artifact._payload()):
        raise ReviewArtifactError("REVIEW_ARTIFACT_DIGEST_MISMATCH")
    if artifact.p0 != 0 or artifact.p1 != 0:
        raise ReviewArtifactError(
            f"REVIEW_ARTIFACT_NONZERO_FINDINGS:p0={artifact.p0}:p1={artifact.p1}"
        )
    if artifact.verdict != expected.pass_verdict:
        raise ReviewArtifactError(f"REVIEW_ARTIFACT_NON_PASS_VERDICT:{artifact.verdict}")
    return artifact


def validate_review_for_v1_9_acceptance(
    artifact: ReviewArtifact,
    *,
    candidate_commit: str,
    protocol_sha256: str,
) -> ReviewArtifact:
    """Preserve explicit historical v1.9 artifact verification."""

    expected = TrustedExpectedContext(
        artifact_schema_version=None,
        protocol_version=V1_9_PROTOCOL_VERSION,
        reviewed_head=candidate_commit,
        protocol_identity=protocol_sha256,
        pass_verdict=V1_9_PASS_VERDICT,
    )
    return validate_review_for_acceptance(artifact, expected=expected)


def is_reviewed_for_runtime(
    path: Path,
    *,
    runtime_commit: str,
    protocol_commit: str,
    protocol_tag_object: str,
    protocol_sha256: str,
) -> bool:
    """Return False for absent v1.6 review; reject any invalid present file."""

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


def is_reviewed_for_acceptance(
    path: Path,
    *,
    expected: TrustedExpectedContext,
) -> bool:
    """Return False for absence; fail closed for invalid/replayed artifacts."""

    path = Path(path)
    if not path.exists():
        return False
    artifact = load_review_artifact(path)
    validate_review_for_acceptance(artifact, expected=expected)
    return True


def is_reviewed_for_v1_9_acceptance(
    path: Path,
    *,
    candidate_commit: str,
    protocol_sha256: str,
) -> bool:
    """Historical explicit v1.9 predicate retained for legacy evidence."""

    path = Path(path)
    if not path.exists():
        return False
    artifact = load_review_artifact(path)
    validate_review_for_v1_9_acceptance(
        artifact,
        candidate_commit=candidate_commit,
        protocol_sha256=protocol_sha256,
    )
    return True


def write_acceptance_artifact(
    path: Path,
    *,
    expected: TrustedExpectedContext,
    reviewer: str,
    verdict: str,
    p0: int,
    p1: int,
    p2: int,
    p3: int,
    reviewed_at: str,
    notes: tuple[str, ...] = (),
) -> Path:
    """Serialize reviewer-supplied findings against an independent context.

    This function does not decide a verdict or findings and does not construct
    trust from artifact input.  The independent reviewer owns those inputs.
    """

    artifact = ReviewArtifact(
        reviewer_identity=reviewer,
        reviewed_head=expected.reviewed_head,
        protocol_commit=expected.reviewed_head,
        protocol_tag_object=None,
        protocol_sha256=expected.protocol_identity,
        p0=p0,
        p1=p1,
        p2=p2,
        p3=p3,
        verdict=verdict,
        reviewed_at=reviewed_at,
        notes=notes,
        protocol_version=expected.protocol_version,
        artifact_schema_version=expected.artifact_schema_version,
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    except FileExistsError as exc:
        raise ReviewArtifactError("REVIEW_ARTIFACT_OUTPUT_ALREADY_EXISTS") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(artifact.to_json())
    finally:
        os.close(descriptor)
    return path


def write_synthetic_artifact(
    path: Path,
    *,
    runtime_commit: str,
    protocol_commit: str,
    protocol_tag_object: str,
    protocol_sha256: str,
    reviewer: str = "synthetic-test-reviewer",
) -> Path:
    """Write a synthetic historical v1.6 fixture for tests only."""

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
        verdict=V1_6_PASS_VERDICT,
        reviewed_at=datetime.now(UTC).isoformat(),
        notes=("synthetic fixture for verifier tests",),
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(artifact.to_json(), encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def write_synthetic_acceptance_artifact(
    path: Path,
    *,
    expected: TrustedExpectedContext,
    reviewer: str = "synthetic-test-reviewer",
    verdict: str | None = None,
    p0: int = 0,
    p1: int = 0,
    p2: int = 0,
    p3: int = 0,
) -> Path:
    """Write a visibly synthetic version-aware fixture for tests only."""

    return write_acceptance_artifact(
        path,
        expected=expected,
        reviewer=reviewer,
        verdict=expected.pass_verdict if verdict is None else verdict,
        p0=p0,
        p1=p1,
        p2=p2,
        p3=p3,
        reviewed_at=datetime.now(UTC).isoformat(),
        notes=("synthetic fixture for version-aware verifier tests",),
    )


def write_synthetic_v1_9_artifact(
    path: Path,
    *,
    candidate_commit: str,
    protocol_sha256: str,
    protocol_commit: str | None = None,
    reviewer: str = "synthetic-test-reviewer",
    verdict: str = V1_9_PASS_VERDICT,
    p0: int = 0,
    p1: int = 0,
    p2: int = 0,
    p3: int = 0,
) -> Path:
    """Write the exact historical v1.9 synthetic schema for tests only."""

    artifact = ReviewArtifact(
        reviewer_identity=reviewer,
        reviewed_head=candidate_commit,
        protocol_commit=protocol_commit if protocol_commit is not None else candidate_commit,
        protocol_tag_object=None,
        protocol_sha256=protocol_sha256,
        p0=p0,
        p1=p1,
        p2=p2,
        p3=p3,
        verdict=verdict,
        reviewed_at=datetime.now(UTC).isoformat(),
        notes=("synthetic fixture for v1.9 verifier tests",),
        protocol_version=V1_9_PROTOCOL_VERSION,
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(artifact.to_json(), encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


__all__ = (
    "ACTIVE_ACCEPTANCE_DECLARATION_PATH",
    "ARTIFACT_SCHEMA_VERSION",
    "V1_9_PASS_VERDICT",
    "V1_9_PROTOCOL_VERSION",
    "V1_10_PASS_VERDICT",
    "V1_10_PROTOCOL_VERSION",
    "ActiveAcceptanceDeclaration",
    "ReviewArtifact",
    "ReviewArtifactError",
    "TrustedExpectedContext",
    "build_trusted_expected_context",
    "is_reviewed_for_acceptance",
    "is_reviewed_for_runtime",
    "is_reviewed_for_v1_9_acceptance",
    "load_active_acceptance_declaration",
    "load_review_artifact",
    "validate_review_for_acceptance",
    "validate_review_for_runtime",
    "validate_review_for_v1_9_acceptance",
    "write_acceptance_artifact",
    "write_synthetic_acceptance_artifact",
    "write_synthetic_artifact",
    "write_synthetic_v1_9_artifact",
)
