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
    claim_authorization,
    create_authorization,
    mark_consumed,
    mark_recovery,
    read_manifest,
    validate_authorization_for_runtime,
    validate_recovery_authorization_for_runtime,
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


class TestRecoveryAuthorization:
    def test_only_consumed_record_can_transition_to_recovery(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "auth.json", _record())

        with pytest.raises(AuthorizationError, match="RECOVERY_REQUIRES_CONSUMED"):
            mark_recovery(path, _record())

        consumed = mark_consumed(path, _record())
        recovery = mark_recovery(path, consumed)

        assert recovery.status == "RECOVERY"
        assert read_manifest(path) == recovery

    def test_recovery_validation_preserves_exact_binding_and_identity(self) -> None:
        recovery = _record(status="RECOVERY")

        validated = validate_recovery_authorization_for_runtime(
            recovery,
            authorization_id=_AUTH_ID,
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
            runtime_commit=_RUNTIME,
        )

        assert validated is recovery
        assert validated.status == "RECOVERY"

    @pytest.mark.parametrize("status", ["ACTIVE", "CONSUMED"])
    def test_non_recovery_status_cannot_authorize_recovery(self, status: str) -> None:
        with pytest.raises(AuthorizationError, match="AUTHORIZATION_NOT_RECOVERY"):
            validate_recovery_authorization_for_runtime(
                _record(status=status),
                authorization_id=_AUTH_ID,
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
                runtime_commit=_RUNTIME,
            )

    def test_recovery_binding_mismatch_fails_closed(self) -> None:
        with pytest.raises(AuthorizationError, match="AUTHORIZATION_RUNTIME_REPLAY"):
            validate_recovery_authorization_for_runtime(
                _record(status="RECOVERY"),
                authorization_id=_AUTH_ID,
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
                runtime_commit="0" * 40,
            )

    def test_recovery_remains_recovery_only_across_repeated_sessions(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "auth.json", _record(status="CONSUMED"))
        recovery = mark_recovery(path, read_manifest(path))

        first = validate_recovery_authorization_for_runtime(
            recovery,
            authorization_id=_AUTH_ID,
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
            runtime_commit=_RUNTIME,
        )
        second = validate_recovery_authorization_for_runtime(
            read_manifest(path),
            authorization_id=_AUTH_ID,
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
            runtime_commit=_RUNTIME,
        )

        assert first.status == second.status == "RECOVERY"
        with pytest.raises(AuthorizationError, match="AUTHORIZATION_RECOVERY_ONLY"):
            validate_authorization_for_runtime(
                second,
                authorization_id=_AUTH_ID,
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
                runtime_commit=_RUNTIME,
            )


class TestAtomicConcurrentClaim:
    """Prove that ``claim_authorization`` is atomic: two concurrent claims on the
    same authorization ID must yield at most one success."""

    def test_single_claim_succeeds(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "auth.json", _record())
        claimed = claim_authorization(
            path,
            authorization_id=_AUTH_ID,
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
            runtime_commit=_RUNTIME,
        )
        assert claimed.status == "CONSUMED"
        # Verify on-disk state is CONSUMED
        reloaded = read_manifest(path)
        assert reloaded.status == "CONSUMED"

    def test_second_claim_after_consumed_fails(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "auth.json", _record())
        claim_authorization(
            path,
            authorization_id=_AUTH_ID,
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
            runtime_commit=_RUNTIME,
        )
        with pytest.raises(AuthorizationError) as exc:
            claim_authorization(
                path,
                authorization_id=_AUTH_ID,
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
                runtime_commit=_RUNTIME,
            )
        assert "CONSUMED" in str(exc.value) or "NOT_ACTIVE" in str(exc.value)

    def test_concurrent_claims_only_one_succeeds(self, tmp_path: Path) -> None:
        """Simulate two concurrent processes racing on the same manifest.

        Uses ``os.fork()`` to create a real second process.  Both children
        attempt ``claim_authorization``; the parent collects results and
        asserts exactly one succeeded.
        """
        import os as _os

        path = _write(tmp_path / "auth.json", _record())

        read_fd, write_fd = _os.pipe()
        pid_a = _os.fork()
        if pid_a == 0:
            # Child A
            _os.close(read_fd)
            result_a = {"pid": _os.getpid(), "success": False, "error": None}
            try:
                claim_authorization(
                    path,
                    authorization_id=_AUTH_ID,
                    protocol_commit=_PROTOCOL,
                    protocol_tag_object=_TAG_OBJECT,
                    protocol_sha256=_SHA,
                    runtime_commit=_RUNTIME,
                )
                result_a["success"] = True
            except AuthorizationError as e:
                result_a["error"] = str(e)
            payload = json.dumps(result_a).encode()
            _os.write(write_fd, payload)
            _os.close(write_fd)
            _os._exit(0)

        pid_b = _os.fork()
        if pid_b == 0:
            # Child B
            _os.close(read_fd)
            result_b = {"pid": _os.getpid(), "success": False, "error": None}
            try:
                claim_authorization(
                    path,
                    authorization_id=_AUTH_ID,
                    protocol_commit=_PROTOCOL,
                    protocol_tag_object=_TAG_OBJECT,
                    protocol_sha256=_SHA,
                    runtime_commit=_RUNTIME,
                )
                result_b["success"] = True
            except AuthorizationError as e:
                result_b["error"] = str(e)
            payload = json.dumps(result_b).encode()
            _os.write(write_fd, payload)
            _os.close(write_fd)
            _os._exit(0)

        _os.close(write_fd)
        results: list[dict] = []
        for _ in range(2):
            data = _os.read(read_fd, 4096).decode()
            results.append(json.loads(data))
        _os.close(read_fd)

        # Reap children
        _os.waitpid(pid_a, 0)
        _os.waitpid(pid_b, 0)

        successes = [r for r in results if r["success"]]
        failures = [r for r in results if not r["success"]]

        assert len(successes) == 1, f"Expected exactly 1 success, got {successes}"
        assert len(failures) == 1, f"Expected exactly 1 failure, got {failures}"
        assert any(
            reason in failures[0]["error"] for reason in ("CONCURRENT", "CONSUMED", "NOT_ACTIVE")
        )

        # Verify on-disk state is CONSUMED (only one claim persisted)
        reloaded = read_manifest(path)
        assert reloaded.status == "CONSUMED"

    def test_lock_file_cleaned_up_after_claim(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "auth.json", _record())
        lock_path = Path(str(path) + ".lock")
        claim_authorization(
            path,
            authorization_id=_AUTH_ID,
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
            runtime_commit=_RUNTIME,
        )
        assert not lock_path.exists(), "Lock file must be cleaned up after claim"

    def test_lock_file_cleaned_up_after_failure(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "auth.json", _record())
        claim_authorization(
            path,
            authorization_id=_AUTH_ID,
            protocol_commit=_PROTOCOL,
            protocol_tag_object=_TAG_OBJECT,
            protocol_sha256=_SHA,
            runtime_commit=_RUNTIME,
        )
        lock_path = Path(str(path) + ".lock")
        # Second claim fails, but lock must still be cleaned up
        with pytest.raises(AuthorizationError):
            claim_authorization(
                path,
                authorization_id=_AUTH_ID,
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
                runtime_commit=_RUNTIME,
            )
        assert not lock_path.exists(), "Lock file must be cleaned up even after failure"

    def test_wrong_runtime_rejected(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "auth.json", _record())
        with pytest.raises(AuthorizationError):
            claim_authorization(
                path,
                authorization_id=_AUTH_ID,
                protocol_commit=_PROTOCOL,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
                runtime_commit="0" * 40,
            )
        # Manifest must still be ACTIVE (not consumed on failed claim)
        reloaded = read_manifest(path)
        assert reloaded.status == "ACTIVE"

    def test_wrong_protocol_commit_rejected_and_not_consumed(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "auth.json", _record())
        with pytest.raises(AuthorizationError):
            claim_authorization(
                path,
                authorization_id=_AUTH_ID,
                protocol_commit="0" * 40,
                protocol_tag_object=_TAG_OBJECT,
                protocol_sha256=_SHA,
                runtime_commit=_RUNTIME,
            )
        reloaded = read_manifest(path)
        assert reloaded.status == "ACTIVE"
