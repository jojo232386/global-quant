"""Focused fail-closed tests for the v1.9 independent-acceptance artifact."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from global_quant.gate1b.review_artifact import (
    ReviewArtifactError,
    is_reviewed_for_runtime,
    load_review_artifact,
    validate_review_for_v1_9_acceptance,
    write_synthetic_artifact,
    write_synthetic_v1_9_artifact,
)

_CANDIDATE = "a" * 40
_PROTOCOL_SHA = "b" * 64
_LEGACY_PROTOCOL = "c" * 40
_LEGACY_TAG_OBJECT = "d" * 40


def _v1_9_artifact(path: Path, **overrides: object) -> Path:
    values: dict[str, object] = {
        "candidate_commit": _CANDIDATE,
        "protocol_sha256": _PROTOCOL_SHA,
    }
    values.update(overrides)
    return write_synthetic_v1_9_artifact(path, **values)  # type: ignore[arg-type]


def _validate(path: Path) -> None:
    validate_review_for_v1_9_acceptance(
        load_review_artifact(path),
        candidate_commit=_CANDIDATE,
        protocol_sha256=_PROTOCOL_SHA,
    )


def _rewrite(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


class TestV19ReviewArtifact:
    def test_correct_synthetic_v1_9_artifact_passes(self, tmp_path: Path) -> None:
        path = _v1_9_artifact(tmp_path / "review.json")

        _validate(path)

    def test_v1_6_artifact_cannot_satisfy_v1_9_acceptance(self, tmp_path: Path) -> None:
        path = write_synthetic_artifact(
            tmp_path / "legacy.json",
            runtime_commit=_CANDIDATE,
            protocol_commit=_LEGACY_PROTOCOL,
            protocol_tag_object=_LEGACY_TAG_OBJECT,
            protocol_sha256=_PROTOCOL_SHA,
        )

        with pytest.raises(ReviewArtifactError, match="PROTOCOL_VERSION_MISMATCH"):
            _validate(path)

    def test_missing_protocol_version_is_rejected(self, tmp_path: Path) -> None:
        path = _v1_9_artifact(tmp_path / "review.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("protocol_version")
        payload.pop("artifact_sha256")
        payload["protocol_tag_object"] = _LEGACY_TAG_OBJECT
        _rewrite(path, payload)

        with pytest.raises(ReviewArtifactError, match="PROTOCOL_VERSION_MISMATCH"):
            _validate(path)

    def test_wrong_protocol_version_is_rejected(self, tmp_path: Path) -> None:
        path = _v1_9_artifact(tmp_path / "review.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["protocol_version"] = "1.8"
        _rewrite(path, payload)

        with pytest.raises(ReviewArtifactError, match="INVALID_PROTOCOL_VERSION"):
            _validate(path)

    def test_wrong_reviewed_head_is_rejected(self, tmp_path: Path) -> None:
        path = _v1_9_artifact(tmp_path / "review.json")

        with pytest.raises(ReviewArtifactError, match="HEAD_MISMATCH"):
            validate_review_for_v1_9_acceptance(
                load_review_artifact(path),
                candidate_commit="e" * 40,
                protocol_sha256=_PROTOCOL_SHA,
            )

    def test_protocol_commit_must_match_the_candidate(self, tmp_path: Path) -> None:
        path = _v1_9_artifact(
            tmp_path / "review.json",
            protocol_commit="e" * 40,
        )

        with pytest.raises(ReviewArtifactError, match="PROTOCOL_COMMIT_MISMATCH"):
            _validate(path)

    def test_wrong_protocol_content_hash_is_rejected(self, tmp_path: Path) -> None:
        path = _v1_9_artifact(tmp_path / "review.json", protocol_sha256="e" * 64)

        with pytest.raises(ReviewArtifactError, match="PROTOCOL_HASH_MISMATCH"):
            _validate(path)

    def test_tampered_artifact_is_rejected(self, tmp_path: Path) -> None:
        path = _v1_9_artifact(tmp_path / "review.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["notes"].append("tampered after review")
        _rewrite(path, payload)

        with pytest.raises(ReviewArtifactError, match="DIGEST_MISMATCH"):
            _validate(path)

    def test_invalid_verdict_is_rejected(self, tmp_path: Path) -> None:
        path = _v1_9_artifact(tmp_path / "review.json", verdict="BLOCKED")

        with pytest.raises(ReviewArtifactError, match="NON_PASS_VERDICT"):
            _validate(path)

    def test_p0_finding_invalidates_acceptance(self, tmp_path: Path) -> None:
        path = _v1_9_artifact(tmp_path / "review.json", p0=1)

        with pytest.raises(ReviewArtifactError, match="NONZERO_FINDINGS"):
            _validate(path)

    def test_p1_finding_invalidates_acceptance(self, tmp_path: Path) -> None:
        path = _v1_9_artifact(tmp_path / "review.json", p1=1)

        with pytest.raises(ReviewArtifactError, match="NONZERO_FINDINGS"):
            _validate(path)

    def test_legacy_v1_6_path_remains_explicitly_compatible(self, tmp_path: Path) -> None:
        path = write_synthetic_artifact(
            tmp_path / "legacy.json",
            runtime_commit=_CANDIDATE,
            protocol_commit=_LEGACY_PROTOCOL,
            protocol_tag_object=_LEGACY_TAG_OBJECT,
            protocol_sha256=_PROTOCOL_SHA,
        )

        assert (
            is_reviewed_for_runtime(
                path,
                runtime_commit=_CANDIDATE,
                protocol_commit=_LEGACY_PROTOCOL,
                protocol_tag_object=_LEGACY_TAG_OBJECT,
                protocol_sha256=_PROTOCOL_SHA,
            )
            is True
        )
