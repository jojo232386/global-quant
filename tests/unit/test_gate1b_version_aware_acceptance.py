"""Security attacks for version-aware Gate 1B machine acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from global_quant.gate1b.review_artifact import (
    ARTIFACT_SCHEMA_VERSION,
    V1_9_PASS_VERDICT,
    V1_9_PROTOCOL_VERSION,
    V1_10_PASS_VERDICT,
    V1_10_PROTOCOL_VERSION,
    ReviewArtifact,
    ReviewArtifactError,
    TrustedExpectedContext,
    build_trusted_expected_context,
    load_active_acceptance_declaration,
    load_review_artifact,
    validate_review_for_acceptance,
    validate_review_for_v1_9_acceptance,
    write_synthetic_acceptance_artifact,
    write_synthetic_v1_9_artifact,
)

_V1_9_HEAD = "9" * 40
_V1_10_HEAD = "a" * 40
_MALICIOUS_HEAD = "b" * 40
_V1_9_PROTOCOL_IDENTITY = "1" * 64
_V1_10_PROTOCOL = b"tracked v1.10 protocol content\n"
_V1_10_PROTOCOL_IDENTITY = hashlib.sha256(_V1_10_PROTOCOL).hexdigest()
_MALICIOUS_PROTOCOL_IDENTITY = "f" * 64


def _declaration(*, protocol_version: str = V1_10_PROTOCOL_VERSION) -> bytes:
    return (
        json.dumps(
            {
                "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
                "manifest_schema_version": 1,
                "pass_verdict": V1_10_PASS_VERDICT,
                "protocol_path": "protocols/NT_GATE_1B_V1_10.md",
                "protocol_version": protocol_version,
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _v1_10_context() -> TrustedExpectedContext:
    declaration = load_active_acceptance_declaration(
        _declaration(),
        expected_protocol_version=V1_10_PROTOCOL_VERSION,
    )
    return build_trusted_expected_context(
        declaration,
        reviewed_head=_V1_10_HEAD,
        protocol_content=_V1_10_PROTOCOL,
    )


def _legacy_v1_9_context() -> TrustedExpectedContext:
    return TrustedExpectedContext(
        artifact_schema_version=None,
        protocol_version=V1_9_PROTOCOL_VERSION,
        reviewed_head=_V1_9_HEAD,
        protocol_identity=_V1_9_PROTOCOL_IDENTITY,
        pass_verdict=V1_9_PASS_VERDICT,
    )


def _write_artifact(path: Path, artifact: ReviewArtifact) -> Path:
    path.write_text(artifact.to_json(), encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def _modern_artifact(path: Path, **overrides: object) -> Path:
    expected = _v1_10_context()
    values: dict[str, object] = {
        "reviewer_identity": "synthetic-independent-reviewer",
        "reviewed_head": expected.reviewed_head,
        "protocol_commit": expected.reviewed_head,
        "protocol_tag_object": None,
        "protocol_sha256": expected.protocol_identity,
        "p0": 0,
        "p1": 0,
        "p2": 0,
        "p3": 0,
        "verdict": expected.pass_verdict,
        "reviewed_at": "2026-08-12T00:00:00+00:00",
        "notes": ("synthetic attack fixture",),
        "protocol_version": expected.protocol_version,
        "artifact_schema_version": expected.artifact_schema_version,
    }
    values.update(overrides)
    return _write_artifact(path, ReviewArtifact(**values))  # type: ignore[arg-type]


def _validate_v1_10(path: Path) -> None:
    validate_review_for_acceptance(load_review_artifact(path), expected=_v1_10_context())


class TestTrustedExpectedContext:
    def test_context_is_built_from_explicit_version_and_tracked_content(self) -> None:
        expected = _v1_10_context()

        assert expected.artifact_schema_version == "1"
        assert expected.protocol_version == "1.10"
        assert expected.reviewed_head == _V1_10_HEAD
        assert expected.protocol_identity == _V1_10_PROTOCOL_IDENTITY
        assert expected.pass_verdict == V1_10_PASS_VERDICT

    def test_unknown_future_version_without_matching_declaration_fails_closed(self) -> None:
        with pytest.raises(ReviewArtifactError, match="EXPECTED_PROTOCOL_VERSION_UNDECLARED"):
            load_active_acceptance_declaration(
                _declaration(),
                expected_protocol_version="1.11",
            )

    def test_declaration_is_single_active_identity_not_an_extensible_payload(self) -> None:
        payload = json.loads(_declaration())
        payload["legacy_protocols"] = {"1.9": "select-me"}

        with pytest.raises(ReviewArtifactError, match="DECLARATION_FIELDS_INVALID"):
            load_active_acceptance_declaration(
                json.dumps(payload).encode(),
                expected_protocol_version=V1_10_PROTOCOL_VERSION,
            )

    def test_duplicate_declaration_key_is_rejected(self) -> None:
        duplicate = (
            _declaration()
            .decode()
            .replace(
                '"protocol_version": "1.10"',
                '"protocol_version": "1.10", "protocol_version": "1.10"',
            )
        )

        with pytest.raises(ReviewArtifactError, match="DECLARATION_INVALID"):
            load_active_acceptance_declaration(
                duplicate.encode(),
                expected_protocol_version=V1_10_PROTOCOL_VERSION,
            )

    @pytest.mark.parametrize("schema", [True, "1"])
    def test_declaration_schema_requires_exact_integer(self, schema: object) -> None:
        payload = json.loads(_declaration())
        payload["manifest_schema_version"] = schema

        with pytest.raises(ReviewArtifactError, match="DECLARATION_SCHEMA_UNSUPPORTED"):
            load_active_acceptance_declaration(
                json.dumps(payload).encode(),
                expected_protocol_version=V1_10_PROTOCOL_VERSION,
            )

    def test_non_pass_or_cross_version_verdict_declaration_is_rejected(self) -> None:
        payload = json.loads(_declaration())
        payload["pass_verdict"] = "BLOCKED"

        with pytest.raises(ReviewArtifactError, match="EXPECTED_PASS_VERDICT_INVALID"):
            load_active_acceptance_declaration(
                json.dumps(payload).encode(),
                expected_protocol_version=V1_10_PROTOCOL_VERSION,
            )

    def test_protocol_path_must_match_the_declared_version(self) -> None:
        payload = json.loads(_declaration())
        payload["protocol_path"] = "protocols/NT_GATE_1B_V1_9.md"

        with pytest.raises(ReviewArtifactError, match="EXPECTED_PROTOCOL_PATH_INVALID"):
            load_active_acceptance_declaration(
                json.dumps(payload).encode(),
                expected_protocol_version=V1_10_PROTOCOL_VERSION,
            )


class TestVersionAwareAcceptanceAttacks:
    def test_valid_v1_10_artifact_passes_without_a_final_tag(self, tmp_path: Path) -> None:
        path = write_synthetic_acceptance_artifact(
            tmp_path / "v1.10.json",
            expected=_v1_10_context(),
        )

        _validate_v1_10(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["artifact_schema_version"] == "1"
        assert "protocol_tag_object" not in payload

    def test_v1_9_artifact_bound_to_v1_10_head_is_rejected(self, tmp_path: Path) -> None:
        path = write_synthetic_v1_9_artifact(
            tmp_path / "v1.9.json",
            candidate_commit=_V1_10_HEAD,
            protocol_sha256=_V1_10_PROTOCOL_IDENTITY,
        )

        with pytest.raises(ReviewArtifactError, match="SCHEMA_VERSION_MISMATCH"):
            _validate_v1_10(path)

    def test_v1_10_artifact_bound_to_v1_9_head_is_rejected_by_legacy_context(
        self,
        tmp_path: Path,
    ) -> None:
        path = _modern_artifact(
            tmp_path / "v1.10.json",
            reviewed_head=_V1_9_HEAD,
            protocol_commit=_V1_9_HEAD,
            protocol_sha256=_V1_9_PROTOCOL_IDENTITY,
        )

        with pytest.raises(ReviewArtifactError, match="SCHEMA_VERSION_MISMATCH"):
            validate_review_for_acceptance(
                load_review_artifact(path),
                expected=_legacy_v1_9_context(),
            )

    def test_missing_version_is_rejected(self, tmp_path: Path) -> None:
        path = _modern_artifact(tmp_path / "missing-version.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("protocol_version")
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ReviewArtifactError, match="MISSING_FIELDS|INVALID_PROTOCOL_VERSION"):
            _validate_v1_10(path)

    def test_wrong_version_with_self_consistent_digest_is_rejected(self, tmp_path: Path) -> None:
        path = _modern_artifact(
            tmp_path / "wrong-version.json",
            protocol_version="1.11",
        )

        with pytest.raises(ReviewArtifactError, match="PROTOCOL_VERSION_MISMATCH"):
            _validate_v1_10(path)

    def test_correct_version_with_wrong_head_is_rejected(self, tmp_path: Path) -> None:
        path = _modern_artifact(
            tmp_path / "wrong-head.json",
            reviewed_head=_MALICIOUS_HEAD,
            protocol_commit=_MALICIOUS_HEAD,
        )

        with pytest.raises(ReviewArtifactError, match="HEAD_MISMATCH"):
            _validate_v1_10(path)

    def test_correct_version_and_head_with_wrong_protocol_identity_is_rejected(
        self,
        tmp_path: Path,
    ) -> None:
        path = _modern_artifact(
            tmp_path / "wrong-protocol.json",
            protocol_sha256=_MALICIOUS_PROTOCOL_IDENTITY,
        )

        with pytest.raises(ReviewArtifactError, match="PROTOCOL_HASH_MISMATCH"):
            _validate_v1_10(path)

    def test_tampered_artifact_digest_is_rejected(self, tmp_path: Path) -> None:
        path = _modern_artifact(tmp_path / "tampered.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["notes"].append("post-review tamper")
        path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ReviewArtifactError, match="DIGEST_MISMATCH"):
            _validate_v1_10(path)

    def test_duplicate_artifact_identity_field_is_rejected(self, tmp_path: Path) -> None:
        path = _modern_artifact(tmp_path / "duplicate.json")
        duplicate = path.read_text(encoding="utf-8").replace(
            '"protocol_version": "1.10"',
            '"protocol_version": "1.10", "protocol_version": "1.10"',
        )
        path.write_text(duplicate, encoding="utf-8")

        with pytest.raises(ReviewArtifactError, match="MALFORMED_JSON"):
            _validate_v1_10(path)

    @pytest.mark.parametrize(("p0", "p1"), [(1, 0), (0, 1)])
    def test_p0_or_p1_prevents_final_acceptance(
        self,
        tmp_path: Path,
        p0: int,
        p1: int,
    ) -> None:
        path = write_synthetic_acceptance_artifact(
            tmp_path / f"findings-{p0}-{p1}.json",
            expected=_v1_10_context(),
            p0=p0,
            p1=p1,
        )

        with pytest.raises(ReviewArtifactError, match="NONZERO_FINDINGS"):
            _validate_v1_10(path)

    def test_self_consistent_malicious_artifact_cannot_define_trust(self, tmp_path: Path) -> None:
        malicious_context = TrustedExpectedContext(
            artifact_schema_version=ARTIFACT_SCHEMA_VERSION,
            protocol_version=V1_10_PROTOCOL_VERSION,
            reviewed_head=_MALICIOUS_HEAD,
            protocol_identity=_MALICIOUS_PROTOCOL_IDENTITY,
            pass_verdict=V1_10_PASS_VERDICT,
        )
        path = write_synthetic_acceptance_artifact(
            tmp_path / "self-consistent-malicious.json",
            expected=malicious_context,
        )
        malicious = load_review_artifact(path)
        validate_review_for_acceptance(malicious, expected=malicious_context)

        with pytest.raises(ReviewArtifactError, match="HEAD_MISMATCH"):
            validate_review_for_acceptance(malicious, expected=_v1_10_context())

    def test_self_consistent_unknown_future_artifact_cannot_select_context(
        self,
        tmp_path: Path,
    ) -> None:
        path = _modern_artifact(
            tmp_path / "future.json",
            protocol_version="1.11",
            verdict="PASS_GATE1B_V1_11_FUTURE",
        )

        with pytest.raises(ReviewArtifactError, match="PROTOCOL_VERSION_MISMATCH"):
            _validate_v1_10(path)


class TestLegacyV19Preservation:
    def test_legacy_v1_9_artifact_still_verifies_in_explicit_v1_9_context(
        self,
        tmp_path: Path,
    ) -> None:
        path = write_synthetic_v1_9_artifact(
            tmp_path / "legacy-v1.9.json",
            candidate_commit=_V1_9_HEAD,
            protocol_sha256=_V1_9_PROTOCOL_IDENTITY,
        )

        validate_review_for_v1_9_acceptance(
            load_review_artifact(path),
            candidate_commit=_V1_9_HEAD,
            protocol_sha256=_V1_9_PROTOCOL_IDENTITY,
        )

    def test_legacy_v1_9_artifact_is_not_a_wildcard_for_v1_10(self, tmp_path: Path) -> None:
        path = write_synthetic_v1_9_artifact(
            tmp_path / "legacy-v1.9.json",
            candidate_commit=_V1_10_HEAD,
            protocol_sha256=_V1_10_PROTOCOL_IDENTITY,
        )

        with pytest.raises(ReviewArtifactError, match="SCHEMA_VERSION_MISMATCH"):
            _validate_v1_10(path)
