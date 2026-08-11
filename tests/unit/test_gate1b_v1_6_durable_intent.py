from __future__ import annotations

import json
import os
import stat
from decimal import Decimal
from pathlib import Path

import pytest

from global_quant.gate1b.durable_intent import (
    DurableIntentError,
    PersistedIntent,
    load_persisted_intent,
    persist_intent,
)
from global_quant.gate1b.mutation_protocol import (
    DurableIntent,
    LimitOrderFilters,
    OrderDerivationProof,
)


def _intent(*, persisted: bool = False) -> DurableIntent:
    filters = LimitOrderFilters(
        min_price=Decimal("1000.00"),
        max_price=Decimal("5000.00"),
        tick_size=Decimal("0.01"),
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("100.000"),
        step_size=Decimal("0.001"),
        min_notional=Decimal("5"),
        percent_price_multiplier_down=Decimal("0.85"),
        percent_price_multiplier_up=Decimal("1.05"),
    )
    derivation = OrderDerivationProof(
        best_bid=Decimal("2000.00"),
        best_ask=Decimal("2000.01"),
        mark_price=Decimal("2000.00"),
        filters=filters,
        filter_snapshot_sha256="6" * 64,
        filter_contract_sha256=filters.canonical_sha256,
        book_age_ms=Decimal("100"),
        mark_age_ms=Decimal("100"),
        observed_elapsed_seconds=Decimal("1"),
    )
    return DurableIntent(
        authorization_id="g1b16-0123456789abcdef",
        protocol_commit="1" * 40,
        protocol_tag_object="2" * 40,
        protocol_sha256="3" * 64,
        runtime_commit="4" * 40,
        session_nonce="5" * 16,
        order_derivation=derivation,
        persisted=persisted,
    )


def _owner_root(tmp_path: Path) -> Path:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    return root


def test_persist_then_replay_reconstructs_exact_intent_and_proves_file_hash(
    tmp_path: Path,
) -> None:
    path = _owner_root(tmp_path) / "intent.json"

    receipt = persist_intent(path, _intent())
    replayed = load_persisted_intent(path)

    assert receipt == replayed
    assert replayed.intent.persisted is True
    assert replayed.intent.intent_sha256 == _intent().intent_sha256
    assert replayed.path == path
    assert len(replayed.file_sha256) == 64
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="ascii"))["intent_sha256"] == (
        replayed.intent.intent_sha256
    )


def test_persist_rejects_caller_claim_that_intent_is_already_durable(
    tmp_path: Path,
) -> None:
    path = _owner_root(tmp_path) / "intent.json"

    with pytest.raises(DurableIntentError, match="INTENT_MUST_START_UNPERSISTED"):
        persist_intent(path, _intent(persisted=True))

    assert not path.exists()


def test_persisted_receipt_has_no_public_nominal_constructor(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        PersistedIntent(  # type: ignore[call-arg]
            path=tmp_path / "missing.json",
            intent=_intent(persisted=True),
            file_sha256="0" * 64,
        )


def test_intent_publication_is_single_use_and_never_overwrites(
    tmp_path: Path,
) -> None:
    path = _owner_root(tmp_path) / "intent.json"
    first = persist_intent(path, _intent())

    with pytest.raises(DurableIntentError, match="INTENT_ALREADY_EXISTS"):
        persist_intent(path, _intent())

    assert load_persisted_intent(path) == first


def test_replay_rejects_unknown_field_instead_of_ignoring_credential_material(
    tmp_path: Path,
) -> None:
    path = _owner_root(tmp_path) / "intent.json"
    persist_intent(path, _intent())
    payload = json.loads(path.read_text(encoding="ascii"))
    payload["api_secret"] = "credential-canary"
    path.write_text(json.dumps(payload), encoding="ascii")
    os.chmod(path, 0o600)

    with pytest.raises(DurableIntentError, match="INTENT_FIELDS_INVALID"):
        load_persisted_intent(path)


def test_replay_rejects_tampered_derivation_even_if_attacker_keeps_old_hash(
    tmp_path: Path,
) -> None:
    path = _owner_root(tmp_path) / "intent.json"
    persist_intent(path, _intent())
    payload = json.loads(path.read_text(encoding="ascii"))
    payload["order_derivation"]["best_bid"] = "2100.00"
    path.write_text(json.dumps(payload), encoding="ascii")
    os.chmod(path, 0o600)

    with pytest.raises(DurableIntentError):
        load_persisted_intent(path)


def test_replay_rejects_boolean_disguised_as_integer_budget(tmp_path: Path) -> None:
    path = _owner_root(tmp_path) / "intent.json"
    persist_intent(path, _intent())
    payload = json.loads(path.read_text(encoding="ascii"))
    payload["budgets"]["max_read_retries"] = True
    path.write_text(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        encoding="ascii",
    )
    os.chmod(path, 0o600)

    with pytest.raises(DurableIntentError, match="INTENT_BUDGET_FIELDS_INVALID"):
        load_persisted_intent(path)


@pytest.mark.parametrize("mode", [0o644, 0o666, 0o400])
def test_replay_requires_exact_owner_read_write_mode(tmp_path: Path, mode: int) -> None:
    path = _owner_root(tmp_path) / "intent.json"
    persist_intent(path, _intent())
    os.chmod(path, mode)

    with pytest.raises(DurableIntentError, match="INTENT_FILE_NOT_OWNER_ONLY"):
        load_persisted_intent(path)


def test_replay_rejects_symlink(tmp_path: Path) -> None:
    root = _owner_root(tmp_path)
    real = root / "real.json"
    persist_intent(real, _intent())
    link = root / "intent.json"
    link.symlink_to(real)

    with pytest.raises(DurableIntentError, match="INTENT_FILE_SYMLINK"):
        load_persisted_intent(link)


def test_persist_requires_owner_only_parent_directory(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)

    with pytest.raises(DurableIntentError, match="INTENT_DIRECTORY_NOT_OWNER_ONLY"):
        persist_intent(root / "intent.json", _intent())


def test_replay_rechecks_owner_only_parent_directory(tmp_path: Path) -> None:
    root = _owner_root(tmp_path)
    path = root / "intent.json"
    persist_intent(path, _intent())
    os.chmod(root, 0o755)

    with pytest.raises(DurableIntentError, match="INTENT_DIRECTORY_NOT_OWNER_ONLY"):
        load_persisted_intent(path)


def test_publication_fsyncs_file_then_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _owner_root(tmp_path)
    events: list[str] = []
    original_fsync = os.fsync
    original_link = os.link

    def tracked_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        events.append("fsync-dir" if stat.S_ISDIR(mode) else "fsync-file")
        original_fsync(fd)

    def tracked_link(src: Path, dst: Path, **kwargs: object) -> None:
        events.append("publish")
        original_link(src, dst, **kwargs)

    monkeypatch.setattr(os, "fsync", tracked_fsync)
    monkeypatch.setattr(os, "link", tracked_link)

    persist_intent(root / "intent.json", _intent())

    assert events.index("fsync-file") < events.index("publish") < events.index("fsync-dir")


def test_temp_path_substitution_cannot_change_the_published_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _owner_root(tmp_path)
    alternate_path = root / "alternate.json"
    alternate = _intent()
    object.__setattr__(alternate, "session_nonce", "6" * 16)
    persist_intent(alternate_path, alternate)
    alternate_bytes = alternate_path.read_bytes()
    original_link = os.link

    def substitute_then_link(
        src: str,
        dst: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        os.unlink(src, dir_fd=src_dir_fd)
        descriptor = os.open(
            src,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=src_dir_fd,
        )
        try:
            os.write(descriptor, alternate_bytes)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        original_link(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", substitute_then_link)

    with pytest.raises(DurableIntentError, match="INTENT_TEMPORARY_INODE_CHANGED"):
        persist_intent(root / "intent.json", _intent())


def test_parent_path_swap_cannot_redirect_the_directory_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _owner_root(tmp_path)
    moved = tmp_path / "moved-evidence"
    original_fsync = os.fsync
    swapped = False

    def swap_before_directory_fsync(descriptor: int) -> None:
        nonlocal swapped
        if stat.S_ISDIR(os.fstat(descriptor).st_mode) and not swapped:
            swapped = True
            root.rename(moved)
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", swap_before_directory_fsync)

    with pytest.raises(DurableIntentError, match="INTENT_DIRECTORY_PATH_RACE"):
        persist_intent(root / "intent.json", _intent())

    assert swapped is True
    assert (moved / "intent.json").exists()
    assert not (root / "intent.json").exists()
