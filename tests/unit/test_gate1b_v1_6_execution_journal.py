from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

import global_quant.gate1b.execution_journal as journal_module
from global_quant.gate1b.execution_journal import (
    MAX_RECORD_BYTES,
    SCHEMA_VERSION,
    ZERO_DIGEST,
    BoundaryResult,
    ExecutionJournal,
    ExecutionJournalError,
    FrontierState,
    GenerationCapability,
    MutationAttempt,
    MutationKind,
    ReconciliationKey,
    ReconciliationKeyKind,
    RecoveryMode,
)
from global_quant.gate1b.mutation_protocol import (
    build_client_order_id,
    build_emergency_client_order_id,
)

AUTHORIZATION_ID = "g1b16-0123456789abcdef"
RUNTIME_COMMIT = "1" * 40
SESSION_NONCE = "2" * 16
INTENT_SHA256 = "3" * 64
DEADLINE_NS = 9_000_000_000


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _attempt(
    kind: MutationKind = MutationKind.CREATE,
    *,
    generation: int = 1,
    reservation: str = "reservation-1",
    recovery_of_attempt_id: str | None = None,
) -> MutationAttempt:
    return MutationAttempt.build(
        kind=kind,
        generation=generation,
        retry_index=0,
        deadline_ns=DEADLINE_NS + generation,
        reservation_sha256=_sha(reservation),
        authorization_id=AUTHORIZATION_ID,
        intent_sha256=INTENT_SHA256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
        fresh_open_proof_sha256=_sha(f"open-{reservation}")
        if kind is MutationKind.CANCEL
        else None,
        recovery_of_attempt_id=recovery_of_attempt_id,
    )


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _raw_records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()]


def _set_digest(record: dict[str, object]) -> None:
    body = {key: value for key, value in record.items() if key != "digest"}
    record["digest"] = hashlib.sha256(_canonical(body)).hexdigest()


def _write_records(path: Path, records: list[dict[str, object]]) -> None:
    path.write_bytes(b"".join(_canonical(record) + b"\n" for record in records))


def _append_forged_event(path: Path, event: dict[str, object]) -> None:
    records = _raw_records(path)
    record: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "sequence": len(records) + 1,
        "previous_digest": records[-1]["digest"],
        "event": event,
    }
    _set_digest(record)
    records.append(record)
    _write_records(path, records)


def _unknown_attempt(
    tmp_path: Path,
    kind: MutationKind,
) -> tuple[ExecutionJournal, MutationAttempt]:
    journal = ExecutionJournal(tmp_path / f"{kind.value.lower()}.jsonl")
    journal.admit_generation(1, GenerationCapability.PRIMARY)
    attempt = _attempt(kind)
    journal.prepare_attempt(attempt)
    journal.record_go(attempt.attempt_id)
    journal.reap_generation(1)
    assert (
        journal.resolve_after_reap(attempt.attempt_id, BoundaryResult.ABSENT)
        is FrontierState.UNKNOWN
    )
    return journal, attempt


def test_new_journal_is_owner_only_canonical_and_hash_chained(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"

    journal = ExecutionJournal(path)
    journal.admit_generation(1, GenerationCapability.PRIMARY)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw_lines = path.read_bytes().splitlines(keepends=True)
    assert all(
        line.endswith(b"\n") and line == _canonical(json.loads(line)) + b"\n"
        for line in raw_lines
    )
    records = _raw_records(path)
    assert [record["sequence"] for record in records] == [1, 2]
    assert records[0]["previous_digest"] == ZERO_DIGEST
    assert records[1]["previous_digest"] == records[0]["digest"]
    for record in records:
        body = {key: value for key, value in record.items() if key != "digest"}
        assert record["digest"] == hashlib.sha256(_canonical(body)).hexdigest()


def test_creation_fsyncs_file_then_parent_directory(tmp_path, monkeypatch) -> None:
    calls: list[str] = []
    real_fsync = os.fsync

    def tracking_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        calls.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(journal_module.os, "fsync", tracking_fsync)

    ExecutionJournal(tmp_path / "execution.jsonl")

    assert calls == ["file", "directory"]


def test_every_event_append_is_fsynced(tmp_path, monkeypatch) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    calls: list[str] = []
    real_fsync = os.fsync

    def tracking_fsync(fd: int) -> None:
        calls.append("fsync")
        real_fsync(fd)

    monkeypatch.setattr(journal_module.os, "fsync", tracking_fsync)

    journal.admit_generation(1, GenerationCapability.PRIMARY)

    assert calls == ["fsync"]


@pytest.mark.parametrize("special", ["symlink", "directory", "fifo"])
def test_symlink_and_non_regular_paths_fail_closed(tmp_path, special) -> None:
    path = tmp_path / "execution.jsonl"
    if special == "symlink":
        target = tmp_path / "target.jsonl"
        ExecutionJournal(target)
        path.symlink_to(target)
    elif special == "directory":
        path.mkdir()
    else:
        os.mkfifo(path, 0o600)

    with pytest.raises(ExecutionJournalError, match="JOURNAL_NOT_SAFE_REGULAR_FILE"):
        ExecutionJournal(path)


def test_insecure_mode_fails_closed(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    ExecutionJournal(path)
    path.chmod(0o640)

    with pytest.raises(ExecutionJournalError, match="JOURNAL_INSECURE_MODE"):
        ExecutionJournal(path)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda _path: b"not-json\n", "JOURNAL_MALFORMED"),
        (lambda path: path.read_bytes().rstrip(b"\n"), "JOURNAL_TRUNCATED"),
        (lambda _path: b"{" + b"x" * MAX_RECORD_BYTES + b"\n", "JOURNAL_RECORD_OVERSIZED"),
    ],
)
def test_malformed_truncated_and_oversized_records_fail_closed(
    tmp_path,
    mutation,
    error,
) -> None:
    path = tmp_path / "execution.jsonl"
    ExecutionJournal(path)
    path.write_bytes(mutation(path))

    with pytest.raises(ExecutionJournalError, match=error):
        ExecutionJournal(path)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("schema_version", "gate1b.execution-journal.v999", "JOURNAL_SCHEMA_VERSION"),
        ("sequence", 2, "JOURNAL_SEQUENCE"),
        ("previous_digest", "f" * 64, "JOURNAL_PREVIOUS_DIGEST"),
    ],
)
def test_wrong_version_sequence_and_previous_digest_fail_closed(
    tmp_path,
    field,
    value,
    error,
) -> None:
    path = tmp_path / "execution.jsonl"
    ExecutionJournal(path)
    records = _raw_records(path)
    records[0][field] = value
    _set_digest(records[0])
    _write_records(path, records)

    with pytest.raises(ExecutionJournalError, match=error):
        ExecutionJournal(path)


def test_wrong_digest_fails_closed(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    ExecutionJournal(path)
    records = _raw_records(path)
    records[0]["digest"] = "f" * 64
    _write_records(path, records)

    with pytest.raises(ExecutionJournalError, match="JOURNAL_DIGEST"):
        ExecutionJournal(path)


def test_noncanonical_json_fails_closed_even_with_valid_digest(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    ExecutionJournal(path)
    record = _raw_records(path)[0]
    path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="ascii")

    with pytest.raises(ExecutionJournalError, match="JOURNAL_NONCANONICAL"):
        ExecutionJournal(path)


def test_arbitrary_payload_and_credential_field_fail_closed(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    ExecutionJournal(path)
    records = _raw_records(path)
    event = records[0]["event"]
    assert isinstance(event, dict)
    event["payload"] = {"api_key": "must-not-be-journaled"}
    _set_digest(records[0])
    _write_records(path, records)

    with pytest.raises(ExecutionJournalError, match="JOURNAL_EVENT_FIELDS"):
        ExecutionJournal(path)


def test_attempt_is_immutable_and_identity_is_deterministic() -> None:
    first = _attempt()
    second = _attempt()

    assert first == second
    assert first.attempt_id == second.attempt_id
    with pytest.raises(FrozenInstanceError):
        first.retry_index = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("kind", "expected_client_id", "expected_key_kind"),
    [
        (
            MutationKind.CREATE,
            build_client_order_id(RUNTIME_COMMIT, SESSION_NONCE),
            ReconciliationKeyKind.PROBE_CLIENT_ID,
        ),
        (
            MutationKind.CANCEL,
            build_client_order_id(RUNTIME_COMMIT, SESSION_NONCE),
            ReconciliationKeyKind.PROBE_TERMINAL_STATE,
        ),
        (
            MutationKind.EMERGENCY_CLOSE,
            build_emergency_client_order_id(RUNTIME_COMMIT, SESSION_NONCE),
            ReconciliationKeyKind.EMERGENCY_CLOSE_CLIENT_ID,
        ),
    ],
)
def test_attempt_has_kind_correct_deterministic_reconciliation_key(
    kind,
    expected_client_id,
    expected_key_kind,
) -> None:
    attempt = _attempt(kind)

    assert attempt.client_id == expected_client_id
    assert attempt.reconciliation_key == ReconciliationKey(
        kind=expected_key_kind,
        client_id=expected_client_id,
    )


def test_mutation_transport_retry_is_always_zero() -> None:
    with pytest.raises(ExecutionJournalError, match="MUTATION_RETRY_FORBIDDEN"):
        MutationAttempt.build(
            kind=MutationKind.CREATE,
            generation=1,
            retry_index=1,
            deadline_ns=DEADLINE_NS,
            reservation_sha256=_sha("reservation"),
            authorization_id=AUTHORIZATION_ID,
            intent_sha256=INTENT_SHA256,
            runtime_commit=RUNTIME_COMMIT,
            session_nonce=SESSION_NONCE,
        )


def test_forged_attempt_identity_or_reconciliation_key_is_rejected() -> None:
    attempt = _attempt()

    with pytest.raises(ExecutionJournalError, match="ATTEMPT_IDENTITY_MISMATCH"):
        replace(attempt, attempt_id="f" * 64)
    with pytest.raises(ExecutionJournalError, match="RECONCILIATION_KEY_MISMATCH"):
        replace(
            attempt,
            reconciliation_key=ReconciliationKey(
                ReconciliationKeyKind.EMERGENCY_CLOSE_CLIENT_ID,
                attempt.client_id,
            ),
        )


def test_cancel_requires_fresh_open_proof_and_non_cancel_rejects_it() -> None:
    with pytest.raises(ExecutionJournalError, match="FRESH_OPEN_PROOF_REQUIRED"):
        MutationAttempt.build(
            kind=MutationKind.CANCEL,
            generation=1,
            retry_index=0,
            deadline_ns=DEADLINE_NS,
            reservation_sha256=_sha("cancel"),
            authorization_id=AUTHORIZATION_ID,
            intent_sha256=INTENT_SHA256,
            runtime_commit=RUNTIME_COMMIT,
            session_nonce=SESSION_NONCE,
        )
    with pytest.raises(ExecutionJournalError, match="UNEXPECTED_OPEN_PROOF"):
        MutationAttempt.build(
            kind=MutationKind.CREATE,
            generation=1,
            retry_index=0,
            deadline_ns=DEADLINE_NS,
            reservation_sha256=_sha("create"),
            authorization_id=AUTHORIZATION_ID,
            intent_sha256=INTENT_SHA256,
            runtime_commit=RUNTIME_COMMIT,
            session_nonce=SESSION_NONCE,
            fresh_open_proof_sha256=_sha("proof"),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"generation": 0},
        {"deadline_ns": 0},
        {"reservation_sha256": "bad"},
        {"authorization_id": "bad"},
        {"intent_sha256": "bad"},
    ],
)
def test_attempt_rejects_invalid_sanitized_fields(changes) -> None:
    arguments = {
        "kind": MutationKind.CREATE,
        "generation": 1,
        "retry_index": 0,
        "deadline_ns": DEADLINE_NS,
        "reservation_sha256": _sha("reservation"),
        "authorization_id": AUTHORIZATION_ID,
        "intent_sha256": INTENT_SHA256,
        "runtime_commit": RUNTIME_COMMIT,
        "session_nonce": SESSION_NONCE,
    }
    arguments.update(changes)

    with pytest.raises(ExecutionJournalError, match="INVALID_ATTEMPT"):
        MutationAttempt.build(**arguments)


def test_generation_admission_is_exact_monotonic_and_requires_reap(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")

    with pytest.raises(ExecutionJournalError, match="GENERATION_SEQUENCE"):
        journal.admit_generation(2, GenerationCapability.PRIMARY)
    journal.admit_generation(1, GenerationCapability.PRIMARY)
    with pytest.raises(ExecutionJournalError, match="GENERATION_ACTIVE"):
        journal.admit_generation(2, GenerationCapability.PRIMARY)
    with pytest.raises(ExecutionJournalError, match="REAP_GENERATION_MISMATCH"):
        journal.reap_generation(2)
    journal.reap_generation(1)
    journal.admit_generation(2, GenerationCapability.PRIMARY)


def test_recovery_generation_cannot_be_first_or_precede_prior_reap(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")

    with pytest.raises(ExecutionJournalError, match="RECOVERY_REQUIRES_REAP"):
        journal.admit_generation(1, GenerationCapability.RECOVERY)
    journal.admit_generation(1, GenerationCapability.PRIMARY)
    with pytest.raises(ExecutionJournalError, match="GENERATION_ACTIVE"):
        journal.admit_generation(2, GenerationCapability.RECOVERY)


def test_prepare_requires_exact_active_generation_and_unique_attempt(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    attempt = _attempt()

    with pytest.raises(ExecutionJournalError, match="ATTEMPT_GENERATION_NOT_ACTIVE"):
        journal.prepare_attempt(attempt)
    journal.admit_generation(1, GenerationCapability.PRIMARY)
    journal.prepare_attempt(attempt)
    with pytest.raises(ExecutionJournalError, match="ATTEMPT_ALREADY_EXISTS"):
        journal.prepare_attempt(attempt)
    journal.reap_generation(1)
    with pytest.raises(ExecutionJournalError, match="ATTEMPT_GENERATION_NOT_ACTIVE"):
        journal.prepare_attempt(_attempt(reservation="another"))


def test_go_requires_durable_prepared_and_cannot_repeat_or_follow_reap(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    journal.admit_generation(1, GenerationCapability.PRIMARY)
    attempt = _attempt()

    with pytest.raises(ExecutionJournalError, match="ATTEMPT_NOT_FOUND"):
        journal.record_go(attempt.attempt_id)
    journal.prepare_attempt(attempt)
    journal.record_go(attempt.attempt_id)
    assert journal.frontier(attempt.attempt_id) is FrontierState.GO_DURABLE
    with pytest.raises(ExecutionJournalError, match="GO_REQUIRES_PREPARED"):
        journal.record_go(attempt.attempt_id)
    journal.reap_generation(1)
    with pytest.raises(ExecutionJournalError, match="GO_REQUIRES_PREPARED"):
        journal.record_go(attempt.attempt_id)


def test_confirmation_requires_go_and_is_terminal(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    journal.admit_generation(1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    journal.prepare_attempt(attempt)

    with pytest.raises(ExecutionJournalError, match="CONFIRMATION_REQUIRES_GO"):
        journal.record_confirmed(attempt.attempt_id, _sha("result"))
    journal.record_go(attempt.attempt_id)
    journal.record_confirmed(attempt.attempt_id, _sha("result"))
    assert journal.frontier(attempt.attempt_id) is FrontierState.CONFIRMED
    with pytest.raises(ExecutionJournalError, match="CONFIRMATION_REQUIRES_GO"):
        journal.record_confirmed(attempt.attempt_id, _sha("result-2"))


def test_prepared_without_go_becomes_not_dispatched_only_after_exact_reap(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    journal.admit_generation(1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    journal.prepare_attempt(attempt)

    with pytest.raises(ExecutionJournalError, match="OUTCOME_REQUIRES_REAP"):
        journal.resolve_after_reap(attempt.attempt_id, BoundaryResult.EOF)
    assert journal.frontier(attempt.attempt_id) is FrontierState.PREPARED
    journal.reap_generation(1)
    assert (
        journal.resolve_after_reap(attempt.attempt_id, BoundaryResult.EOF)
        is FrontierState.NOT_DISPATCHED
    )


@pytest.mark.parametrize(
    "boundary_result",
    [BoundaryResult.ABSENT, BoundaryResult.CORRUPT, BoundaryResult.EOF],
)
def test_go_without_result_becomes_unknown_only_after_reap(tmp_path, boundary_result) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    journal.admit_generation(1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    journal.prepare_attempt(attempt)
    journal.record_go(attempt.attempt_id)

    with pytest.raises(ExecutionJournalError, match="OUTCOME_REQUIRES_REAP"):
        journal.resolve_after_reap(attempt.attempt_id, boundary_result)
    journal.reap_generation(1)
    assert journal.frontier(attempt.attempt_id) is FrontierState.GO_DURABLE
    assert (
        journal.resolve_after_reap(attempt.attempt_id, boundary_result)
        is FrontierState.UNKNOWN
    )


def test_reap_never_itself_implies_venue_non_dispatch(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    journal.admit_generation(1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    journal.prepare_attempt(attempt)
    journal.record_go(attempt.attempt_id)

    journal.reap_generation(1)

    assert journal.frontier(attempt.attempt_id) is FrontierState.GO_DURABLE


def test_forged_not_dispatched_after_go_fails_closed_on_replay(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    journal = ExecutionJournal(path)
    journal.admit_generation(1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    journal.prepare_attempt(attempt)
    journal.record_go(attempt.attempt_id)
    journal.reap_generation(1)
    _append_forged_event(
        path,
        {
            "type": "ATTEMPT_RESOLVED",
            "attempt_id": attempt.attempt_id,
            "generation": 1,
            "state": FrontierState.NOT_DISPATCHED.value,
            "boundary_result": BoundaryResult.ABSENT.value,
        },
    )

    with pytest.raises(ExecutionJournalError, match="NOT_DISPATCHED_REQUIRES_NO_GO"):
        ExecutionJournal(path)


def test_forged_unknown_before_reap_fails_closed_on_replay(tmp_path) -> None:
    path = tmp_path / "execution.jsonl"
    journal = ExecutionJournal(path)
    journal.admit_generation(1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    journal.prepare_attempt(attempt)
    journal.record_go(attempt.attempt_id)
    _append_forged_event(
        path,
        {
            "type": "ATTEMPT_RESOLVED",
            "attempt_id": attempt.attempt_id,
            "generation": 1,
            "state": FrontierState.UNKNOWN.value,
            "boundary_result": BoundaryResult.CORRUPT.value,
        },
    )

    with pytest.raises(ExecutionJournalError, match="OUTCOME_REQUIRES_REAP"):
        ExecutionJournal(path)


def test_failed_go_write_leaves_only_durable_prepared(tmp_path, monkeypatch) -> None:
    path = tmp_path / "execution.jsonl"
    journal = ExecutionJournal(path)
    journal.admit_generation(1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    journal.prepare_attempt(attempt)

    def failed_write(_fd: int, _data: bytes) -> int:
        raise OSError("injected write failure")

    monkeypatch.setattr(journal_module.os, "write", failed_write)
    with pytest.raises(ExecutionJournalError, match="JOURNAL_APPEND_FAILED"):
        journal.record_go(attempt.attempt_id)
    monkeypatch.undo()

    reopened = ExecutionJournal(path)
    assert reopened.frontier(attempt.attempt_id) is FrontierState.PREPARED
    reopened.reap_generation(1)
    assert (
        reopened.resolve_after_reap(attempt.attempt_id, BoundaryResult.ABSENT)
        is FrontierState.NOT_DISPATCHED
    )


def test_partial_append_then_write_failure_is_detected_as_truncation(tmp_path, monkeypatch) -> None:
    path = tmp_path / "execution.jsonl"
    journal = ExecutionJournal(path)
    journal.admit_generation(1, GenerationCapability.PRIMARY)
    real_write = os.write
    writes = 0

    def partial_then_fail(fd: int, data: bytes) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            return real_write(fd, data[: len(data) // 2])
        raise OSError("injected partial write failure")

    monkeypatch.setattr(journal_module.os, "write", partial_then_fail)
    with pytest.raises(ExecutionJournalError, match="JOURNAL_APPEND_FAILED"):
        journal.prepare_attempt(_attempt())
    monkeypatch.undo()

    with pytest.raises(ExecutionJournalError, match="JOURNAL_TRUNCATED"):
        ExecutionJournal(path)


def test_fsync_failure_after_go_is_conservatively_recovered_as_go(tmp_path, monkeypatch) -> None:
    path = tmp_path / "execution.jsonl"
    journal = ExecutionJournal(path)
    journal.admit_generation(1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    journal.prepare_attempt(attempt)
    real_fsync = os.fsync
    failed = False

    def fail_once(fd: int) -> None:
        nonlocal failed
        if not failed and stat.S_ISREG(os.fstat(fd).st_mode):
            failed = True
            raise OSError("injected fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(journal_module.os, "fsync", fail_once)
    with pytest.raises(ExecutionJournalError, match="JOURNAL_FSYNC_FAILED"):
        journal.record_go(attempt.attempt_id)
    monkeypatch.undo()

    reopened = ExecutionJournal(path)
    assert reopened.frontier(attempt.attempt_id) is FrontierState.GO_DURABLE


@pytest.mark.parametrize(
    ("kind", "mode"),
    [
        (MutationKind.CREATE, RecoveryMode.QUERY_PROBE_CLIENT_ID),
        (MutationKind.CANCEL, RecoveryMode.QUERY_TERMINAL_THEN_CONDITIONAL_CANCEL),
        (
            MutationKind.EMERGENCY_CLOSE,
            RecoveryMode.QUERY_CLOSE_ID_AND_FRESH_STATE,
        ),
    ],
)
def test_unknown_has_kind_specific_recovery_directive(tmp_path, kind, mode) -> None:
    journal, attempt = _unknown_attempt(tmp_path, kind)

    directive = journal.recovery_directive(attempt.attempt_id)

    assert directive.mode is mode
    assert directive.query_client_id == attempt.client_id
    assert directive.allows_post_create is False
    assert directive.allows_blind_retry is False
    assert directive.queries_terminal_state is (kind is MutationKind.CANCEL)
    cleanup_cancel_allowed = kind in {MutationKind.CREATE, MutationKind.CANCEL}
    assert directive.requires_fresh_open_proof is cleanup_cancel_allowed
    assert directive.allows_conditional_cleanup_cancel is cleanup_cancel_allowed
    assert directive.requires_fresh_position_state is (kind is MutationKind.EMERGENCY_CLOSE)
    assert directive.requires_fresh_order_state is (kind is MutationKind.EMERGENCY_CLOSE)
    assert directive.requires_fresh_trade_state is (kind is MutationKind.EMERGENCY_CLOSE)


def test_recovery_directive_requires_unknown(tmp_path) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    journal.admit_generation(1, GenerationCapability.PRIMARY)
    attempt = _attempt()
    journal.prepare_attempt(attempt)

    with pytest.raises(ExecutionJournalError, match="RECOVERY_REQUIRES_UNKNOWN"):
        journal.recovery_directive(attempt.attempt_id)


def test_recovery_capability_rejects_create_and_blind_close(tmp_path) -> None:
    journal, _attempt_one = _unknown_attempt(tmp_path, MutationKind.CREATE)
    journal.admit_generation(2, GenerationCapability.RECOVERY)

    with pytest.raises(ExecutionJournalError, match="RECOVERY_MUTATION_FORBIDDEN"):
        journal.prepare_attempt(_attempt(MutationKind.CREATE, generation=2, reservation="create-2"))
    with pytest.raises(ExecutionJournalError, match="RECOVERY_MUTATION_FORBIDDEN"):
        journal.prepare_attempt(
            _attempt(MutationKind.EMERGENCY_CLOSE, generation=2, reservation="close-2")
        )


def test_unknown_cancel_allows_only_new_conditional_attempt_after_fresh_open_proof(
    tmp_path,
) -> None:
    journal, original = _unknown_attempt(tmp_path, MutationKind.CANCEL)
    directive = journal.recovery_directive(original.attempt_id)
    journal.admit_generation(2, GenerationCapability.RECOVERY)

    with pytest.raises(ExecutionJournalError, match="FRESH_OPEN_PROOF_REQUIRED"):
        directive.new_conditional_cleanup_cancel(
            generation=2,
            deadline_ns=DEADLINE_NS + 2,
            reservation_sha256=_sha("conditional-cancel"),
            authorization_id=AUTHORIZATION_ID,
            intent_sha256=INTENT_SHA256,
            runtime_commit=RUNTIME_COMMIT,
            session_nonce=SESSION_NONCE,
            fresh_open_proof_sha256=None,
        )
    second = directive.new_conditional_cleanup_cancel(
        generation=2,
        deadline_ns=DEADLINE_NS + 2,
        reservation_sha256=_sha("conditional-cancel"),
        authorization_id=AUTHORIZATION_ID,
        intent_sha256=INTENT_SHA256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
        fresh_open_proof_sha256=_sha("fresh-open"),
    )
    assert second.attempt_id != original.attempt_id
    assert second.retry_index == 0
    assert second.recovery_of_attempt_id == original.attempt_id
    journal.prepare_attempt(second)
    assert journal.frontier(second.attempt_id) is FrontierState.PREPARED


def test_unknown_create_allows_new_cleanup_cancel_after_fresh_open_proof(tmp_path) -> None:
    journal, original = _unknown_attempt(tmp_path, MutationKind.CREATE)
    directive = journal.recovery_directive(original.attempt_id)
    journal.admit_generation(2, GenerationCapability.RECOVERY)

    cleanup = directive.new_conditional_cleanup_cancel(
        generation=2,
        deadline_ns=DEADLINE_NS + 2,
        reservation_sha256=_sha("create-unknown-cleanup"),
        authorization_id=AUTHORIZATION_ID,
        intent_sha256=INTENT_SHA256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
        fresh_open_proof_sha256=_sha("fresh-open-after-create-unknown"),
    )

    assert cleanup.kind is MutationKind.CANCEL
    assert cleanup.recovery_of_attempt_id == original.attempt_id
    assert cleanup.retry_index == 0
    journal.prepare_attempt(cleanup)


def test_confirmed_create_allows_cleanup_cancel_after_reap_and_fresh_open_proof(
    tmp_path,
) -> None:
    journal = ExecutionJournal(tmp_path / "execution.jsonl")
    journal.admit_generation(1, GenerationCapability.PRIMARY)
    original = _attempt(MutationKind.CREATE)
    journal.prepare_attempt(original)
    journal.record_go(original.attempt_id)
    journal.record_confirmed(original.attempt_id, _sha("create-confirmed"))
    journal.reap_generation(1)
    directive = journal.recovery_directive(original.attempt_id)
    journal.admit_generation(2, GenerationCapability.RECOVERY)

    cleanup = directive.new_conditional_cleanup_cancel(
        generation=2,
        deadline_ns=DEADLINE_NS + 2,
        reservation_sha256=_sha("confirmed-create-cleanup"),
        authorization_id=AUTHORIZATION_ID,
        intent_sha256=INTENT_SHA256,
        runtime_commit=RUNTIME_COMMIT,
        session_nonce=SESSION_NONCE,
        fresh_open_proof_sha256=_sha("fresh-open-after-confirmed-create"),
    )

    journal.prepare_attempt(cleanup)
    assert journal.frontier(cleanup.attempt_id) is FrontierState.PREPARED


def test_recovery_generation_rejects_cancel_not_derived_from_unknown(tmp_path) -> None:
    journal, _attempt_one = _unknown_attempt(tmp_path, MutationKind.CREATE)
    journal.admit_generation(2, GenerationCapability.RECOVERY)

    with pytest.raises(ExecutionJournalError, match="RECOVERY_CANCEL_NOT_AUTHORIZED"):
        journal.prepare_attempt(_attempt(MutationKind.CANCEL, generation=2, reservation="cancel-2"))
