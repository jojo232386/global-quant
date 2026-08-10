"""Tests for the owner-only authorization manifest (``authorization.py``).

Coverage per task section F:

* owner-only 0600 permissions enforced;
* missing / stale / unknown / malformed authorization fails closed;
* replay across a different protocol commit, tag object, protocol hash, or
  runtime commit is rejected;
* a CONSUMED or RECOVERY authorization can never be reused;
* no secret value is ever written to the manifest.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from global_quant.gate1b.authorization import (
    AuthorizationError,
    AuthorizationRecord,
    create_authorization,
    mark_consumed,
    read_manifest,
    validate_authorization_for_runtime,
    write_manifest,
)

_RUNTIME = "a" * 40
_PROTOCOL = "b" * 40
_TAG_OBJECT = "c" * 40
_SHA = "d" * 64
_AUTH_ID = "g1b16-0123456789abcdef"


def _record(**overrides: str) -> AuthorizationRecord:
    values = {
        "authorization_id": _AUTH_ID,
        "protocol_commit": _PROTOCOL,
        "protocol_tag_object": _TAG_OBJECT,
        "protocol_sha256": _SHA,
        "runtime_commit": _RUNTIME,
        "created_at": "2026-08-10T00:00:00+00:00",
        "status": "ACTIVE",
    }
    values.update(overrides)
    return AuthorizationRecord(**values)  # type: ignore[arg-type]


def _write(path: Path, record: AuthorizationRecord) -> Path:
    return write_manifest(path, record)


class TestManifestPermissions:
    def test_owner_only_0600(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "auth.json", _record())
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_insecure_permissions_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "auth.json"
        _write(path, _record())
        os.chmod(path, 0o644)
        with pytest.raises(AuthorizationError) as exc:
            read_manifest(path)
        assert "INSECURE_PERMISSIONS" in str(exc.value)

    def test_missing_manifest_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(AuthorizationError):
            read_manifest(tmp_path / "absent.json")

    def test_symlink_rejected(self, tmp_path: Path) -> None:
        target = _write(tmp_path / "real.json", _record())
        link = tmp_path / "link.json"
        link.symlink_to(target)
        with pytest.raises(AuthorizationError):
            read_manifest(link)

    def test_malformed_json_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "auth.json"
        path.write_text("{not-json", encoding="utf-8")
        os.chmod(path, 0o600)
        with pytest.raises(AuthorizationError):
            read_manifest(path)

    def test_missing_fields_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "auth.json"
        path.write_text(json.dumps({"authorization_id": _AUTH_ID}), encoding="utf-8")
        os.chmod(path, 0o600)
        with pytest.raises(AuthorizationError):
            read_manifest(path)


class TestValidation:
    def test_valid_active_authorization_passes(self) -> None:
        record = validate_authorization_for_runtime(
            _record(),
            authorization_id=_AUTH_ID,
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
            runtime_commit=_RUNTIME,
        )
        assert record.status == "ACTIVE"

    def test_id_mismatch_rejected(self) -> None:
        with pytest.raises(AuthorizationError):
            validate_authorization_for_runtime(
                _record(),
                authorization_id="g1b16-ffffffffffffffff",
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
                runtime_commit=_RUNTIME,
            )

    def test_protocol_commit_replay_rejected(self) -> None:
        with pytest.raises(AuthorizationError):
            validate_authorization_for_runtime(
                _record(),
                authorization_id=_AUTH_ID,
                protocol_commit="f" * 40,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
                runtime_commit=_RUNTIME,
            )

    def test_tag_object_replay_rejected(self) -> None:
        with pytest.raises(AuthorizationError):
            validate_authorization_for_runtime(
                _record(),
                authorization_id=_AUTH_ID,
                protocol_commit=_PROTOCOL,
                protocol_tag_object="f" * 40,
                protocol_sha256=_SHA,
                runtime_commit=_RUNTIME,
            )

    def test_protocol_hash_replay_rejected(self) -> None:
        with pytest.raises(AuthorizationError):
            validate_authorization_for_runtime(
                _record(),
                authorization_id=_AUTH_ID,
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256="f" * 64,
                runtime_commit=_RUNTIME,
            )

    def test_runtime_replay_rejected(self) -> None:
        with pytest.raises(AuthorizationError):
            validate_authorization_for_runtime(
                _record(),
                authorization_id=_AUTH_ID,
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
                runtime_commit="f" * 40,
            )

    def test_consumed_rejected(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "auth.json", _record())
        consumed = mark_consumed(path, _record())
        assert consumed.status == "CONSUMED"
        with pytest.raises(AuthorizationError) as exc:
            validate_authorization_for_runtime(
                consumed,
                authorization_id=_AUTH_ID,
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
                runtime_commit=_RUNTIME,
            )
        assert "CONSUMED" in str(exc.value)

    def test_recovery_rejected(self) -> None:
        with pytest.raises(AuthorizationError):
            validate_authorization_for_runtime(
                _record(status="RECOVERY"),
                authorization_id=_AUTH_ID,
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
                runtime_commit=_RUNTIME,
            )


class TestCreation:
    def test_invalid_id_format_rejected(self) -> None:
        with pytest.raises(AuthorizationError):
            create_authorization(
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
                runtime_commit=_RUNTIME,
                authorization_id="not-an-id",
            )

    def test_valid_creation(self) -> None:
        record = create_authorization(
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
            runtime_commit=_RUNTIME,
            authorization_id=_AUTH_ID,
        )
        assert record.status == "ACTIVE"
        assert record.authorization_id == _AUTH_ID

    def test_manifest_never_contains_secret(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "auth.json", _record())
        text = path.read_text(encoding="utf-8")
        assert "api_key" not in text
        assert "secret" not in text
        assert "BINANCE" not in text
