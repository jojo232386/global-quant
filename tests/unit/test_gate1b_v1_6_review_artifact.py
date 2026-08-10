"""Tests for the independent review artifact mechanism (``review_artifact.py``).

Coverage per task section F:

* synthetic fixture loads and validates;
* missing artifact -> ``is_reviewed_for_runtime`` returns False (CLI stops at
  READY_FOR_REVIEW), never an invented PASS;
* wrong reviewed HEAD / wrong protocol identity / non-zero P0/P1 / non-PASS
  verdict are all rejected;
* owner-only permissions and symlink refusal.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from global_quant.gate1b.review_artifact import (
    ReviewArtifact,
    ReviewArtifactError,
    is_reviewed_for_runtime,
    load_review_artifact,
    validate_review_for_runtime,
    write_synthetic_artifact,
)

_RUNTIME = "a" * 40
_PROTOCOL = "b" * 40
_TAG_OBJECT = "c" * 40
_SHA = "d" * 64


def _artifact(**overrides: object) -> ReviewArtifact:
    values: dict[str, object] = {
        "reviewer_identity": "independent-reviewer-X",
        "reviewed_head": _RUNTIME,
        "protocol_commit": _PROTOCOL,
        "protocol_tag_object": _TAG_OBJECT,
        "protocol_sha256": _SHA,
        "p0": 0,
        "p1": 0,
        "p2": 0,
        "p3": 0,
        "verdict": "PASS_GATE1B_V1_6_DEMO_RUNTIME",
        "reviewed_at": "2026-08-10T00:00:00+00:00",
        "notes": (),
    }
    values.update(overrides)
    return ReviewArtifact(**values)  # type: ignore[arg-type]


class TestLoadAndValidate:
    def test_synthetic_fixture_valid(self, tmp_path: Path) -> None:
        path = write_synthetic_artifact(
            tmp_path / "review.json",
            runtime_commit=_RUNTIME,
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
        )
        artifact = load_review_artifact(path)
        validate_review_for_runtime(
            artifact,
            runtime_commit=_RUNTIME,
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
        )
        assert artifact.reviewer_identity == "synthetic-test-reviewer"

    def test_missing_artifact_is_not_reviewed(self, tmp_path: Path) -> None:
        assert (
            is_reviewed_for_runtime(
                tmp_path / "absent.json",
                runtime_commit=_RUNTIME,
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
            )
            is False
        )

    def test_wrong_head_rejected(self, tmp_path: Path) -> None:
        path = write_synthetic_artifact(
            tmp_path / "review.json",
            runtime_commit=_RUNTIME,
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
        )
        with pytest.raises(ReviewArtifactError) as exc:
            is_reviewed_for_runtime(
                path,
                runtime_commit="f" * 40,
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
            )
        assert "HEAD_MISMATCH" in str(exc.value)

    def test_wrong_protocol_commit_rejected(self) -> None:
        with pytest.raises(ReviewArtifactError):
            validate_review_for_runtime(
                _artifact(),
                runtime_commit=_RUNTIME,
                protocol_commit="f" * 40,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
            )

    def test_nonzero_p0_rejected(self) -> None:
        with pytest.raises(ReviewArtifactError) as exc:
            validate_review_for_runtime(
                _artifact(p0=1),
                runtime_commit=_RUNTIME,
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
            )
        assert "NONZERO_FINDINGS" in str(exc.value)

    def test_nonzero_p1_rejected(self) -> None:
        with pytest.raises(ReviewArtifactError):
            validate_review_for_runtime(
                _artifact(p1=2),
                runtime_commit=_RUNTIME,
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
            )

    def test_non_pass_verdict_rejected(self) -> None:
        with pytest.raises(ReviewArtifactError) as exc:
            validate_review_for_runtime(
                _artifact(verdict="BLOCKED"),
                runtime_commit=_RUNTIME,
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
            )
        assert "NON_PASS_VERDICT" in str(exc.value)

    def test_empty_reviewer_rejected(self, tmp_path: Path) -> None:
        path = _write_json(tmp_path, _artifact(reviewer_identity=" "))
        with pytest.raises(ReviewArtifactError):
            load_review_artifact(path)


class TestFileSecurity:
    def test_symlink_rejected(self, tmp_path: Path) -> None:
        target = write_synthetic_artifact(
            tmp_path / "real.json",
            runtime_commit=_RUNTIME,
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
        )
        link = tmp_path / "link.json"
        link.symlink_to(target)
        with pytest.raises(ReviewArtifactError):
            load_review_artifact(link)

    def test_malformed_json_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "review.json"
        path.write_text("{bad", encoding="utf-8")
        with pytest.raises(ReviewArtifactError):
            load_review_artifact(path)

    def test_insecure_permissions_rejected(self, tmp_path: Path) -> None:
        path = write_synthetic_artifact(
            tmp_path / "review.json",
            runtime_commit=_RUNTIME,
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
        )
        os.chmod(path, 0o644)
        with pytest.raises(ReviewArtifactError):
            load_review_artifact(path)

    def test_written_artifact_is_owner_only(self, tmp_path: Path) -> None:
        path = write_synthetic_artifact(
            tmp_path / "review.json",
            runtime_commit=_RUNTIME,
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
        )
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def _write_json(path: Path, artifact: ReviewArtifact) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    file = path / "review.json"
    file.write_text(artifact.to_json(), encoding="utf-8")
    os.chmod(file, 0o600)
    return file
