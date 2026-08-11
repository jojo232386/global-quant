from __future__ import annotations

import hashlib
import io
import json
import os
import pty
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from dataclasses import replace
from pathlib import Path

import pytest

import global_quant.gate1b.process_boundary as process_boundary_module
from global_quant.gate1b.execution_journal import (
    DurableGenerationAdmission,
    ExecutionJournal,
    GenerationCapability,
)
from global_quant.gate1b.process_boundary import (
    DEFAULT_MAX_IPC_FRAME_BYTES,
    AbsoluteDeadline,
    ChildHardDeadline,
    CredentialBoundaryError,
    CredentialProcessSupervisor,
    CredentialWorkload,
    DeadlineExpired,
    GenerationAdmissionError,
    IPCCodec,
    IPCProtocolError,
    ProcessBoundaryError,
    ProcessIdentity,
    ProcessLifecycleJournal,
    build_seatbelt_argv,
    is_same_process_alive,
    read_process_identity,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src"
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
PYTHON = Path(sys.executable)


def _child_environment(**extra: str) -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(SOURCE_ROOT),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    environment.update(extra)
    return environment


def _child_script(body: str) -> list[str]:
    return [str(PYTHON), "-c", body]


def _supervisor(
    *,
    lifecycle_deadline: float,
    parent_environment: dict[str, str] | None = None,
    credential_stdin: int | None = None,
) -> CredentialProcessSupervisor:
    journal_dir = Path(tempfile.mkdtemp(prefix="gmaq-process-test-"))
    execution_journal = ExecutionJournal(journal_dir / "execution.jsonl")
    journal_path = journal_dir / "lifecycle.jsonl"
    journal = ProcessLifecycleJournal.start(
        journal_path,
        lifecycle_started_at=time.monotonic(),
        lifecycle_deadline=lifecycle_deadline,
        execution_journal_path=execution_journal.path,
    )

    return CredentialProcessSupervisor(
        lifecycle_journal=journal,
        execution_journal=execution_journal,
        parent_environment=parent_environment or _child_environment(),
        credential_stdin=credential_stdin,
        allow_test_workloads=True,
    )


def _replace_envelope(frame: bytes, **changes: object) -> bytes:
    (length,) = struct.unpack(">I", frame[:4])
    envelope = json.loads(frame[4 : 4 + length])
    envelope.update(changes)
    encoded = json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return struct.pack(">I", len(encoded)) + encoded


class MutableClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_absolute_deadline_clamps_every_local_limit_to_lifecycle_remaining() -> None:
    clock = MutableClock(100.0)
    deadline = AbsoluteDeadline(at=110.0, clock=clock)

    assert deadline.clamp(5.0) == 5.0
    clock.value = 108.25
    assert deadline.clamp(5.0) == pytest.approx(1.75)
    clock.value = 109.80
    assert deadline.clamp(5.0) == pytest.approx(0.20)


def test_absolute_deadline_never_restarts_after_exhaustion() -> None:
    clock = MutableClock(100.0)
    deadline = AbsoluteDeadline(at=101.0, clock=clock)
    clock.value = 101.0

    with pytest.raises(DeadlineExpired, match="LIFECYCLE_DEADLINE_EXHAUSTED"):
        deadline.clamp(5.0)
    clock.value = 102.0
    with pytest.raises(DeadlineExpired, match="LIFECYCLE_DEADLINE_EXHAUSTED"):
        deadline.clamp(180.0)


def test_child_io_authority_type_is_not_ordinary_constructible() -> None:
    authority_type = getattr(process_boundary_module, "ChildIOAuthority", None)

    assert authority_type is not None
    with pytest.raises(TypeError):
        authority_type()


def test_external_child_bootstrap_cannot_issue_production_io_authority() -> None:
    bootstrap = process_boundary_module.ChildBootstrap(
        generation=1,
        capability=GenerationCapability.PRIMARY,
        deadline=AbsoluteDeadline(time.monotonic() + 30.0),
        hard_deadline=object(),  # type: ignore[arg-type]
        identity=ProcessIdentity(
            pid=os.getpid(),
            ppid=os.getppid(),
            pgid=os.getpgrp(),
            sid=os.getsid(0),
            start_token="external-bootstrap",
        ),
        channel=object(),  # type: ignore[arg-type]
        workload_kind=process_boundary_module.CredentialWorkloadKind.PRODUCTION,
        _network_gate=object(),  # type: ignore[arg-type]
    )

    issuer = getattr(bootstrap, "issue_io_authority", None)
    assert callable(issuer)
    with pytest.raises(CredentialBoundaryError, match="TRUSTED_CHILD_BOOTSTRAP_REQUIRED"):
        issuer()


class _AlwaysIntactDeadline:
    def __init__(self) -> None:
        self.checks = 0

    def assert_intact(self) -> None:
        self.checks += 1


def _attested_bootstrap(
    *,
    workload_kind,
    guard_installed: bool,
):
    identity = read_process_identity(os.getpid())
    assert identity is not None
    gate = process_boundary_module._NetworkGate(
        ready=guard_installed,
        guard_attestation=(
            process_boundary_module._CREDENTIAL_GUARD_ATTESTATION if guard_installed else None
        ),
    )
    return process_boundary_module.ChildBootstrap(
        generation=1,
        capability=GenerationCapability.PRIMARY,
        deadline=AbsoluteDeadline(time.monotonic() + 30.0),
        hard_deadline=_AlwaysIntactDeadline(),  # type: ignore[arg-type]
        identity=identity,
        channel=object(),  # type: ignore[arg-type]
        workload_kind=workload_kind,
        _network_gate=gate,
        _bootstrap_attestation=process_boundary_module._CHILD_BOOTSTRAP_ATTESTATION,
    )


def test_uninstalled_guard_cannot_issue_child_io_authority() -> None:
    bootstrap = _attested_bootstrap(
        workload_kind=process_boundary_module.CredentialWorkloadKind.PRODUCTION,
        guard_installed=False,
    )

    with pytest.raises(
        CredentialBoundaryError,
        match="CREDENTIAL_GUARD_REQUIRED_BEFORE_NETWORK",
    ):
        bootstrap.issue_io_authority()


def test_test_only_bootstrap_can_never_issue_production_io_authority() -> None:
    bootstrap = _attested_bootstrap(
        workload_kind=process_boundary_module.CredentialWorkloadKind.TEST_ONLY,
        guard_installed=True,
    )

    with pytest.raises(CredentialBoundaryError, match="PRODUCTION_CHILD_WORKLOAD_REQUIRED"):
        bootstrap.issue_io_authority()


def test_child_bootstrap_issues_exactly_one_credential_free_authority() -> None:
    bootstrap = _attested_bootstrap(
        workload_kind=process_boundary_module.CredentialWorkloadKind.PRODUCTION,
        guard_installed=True,
    )

    authority = bootstrap.issue_io_authority()

    assert "generation=1" in repr(authority)
    assert "PRIMARY" in repr(authority)
    assert "credential" not in repr(authority).lower()
    with pytest.raises(CredentialBoundaryError, match="CHILD_IO_AUTHORITY_ALREADY_ISSUED"):
        bootstrap.issue_io_authority()


@pytest.mark.parametrize(
    ("event", "arguments"),
    [
        ("socket.connect", (object(), ("203.0.113.10", 443))),
        ("socket.connect_ex", (object(), ("2001:db8::10", 443, 0, 0))),
        ("socket.getaddrinfo", ("example.invalid", 443, 0, 0, 0)),
        ("socket.gethostbyname", ("example.invalid",)),
        ("socket.gethostbyaddr", ("127.0.0.1",)),
        ("socket.sendto", (object(), b"x", ("203.0.113.10", 443))),
    ],
)
def test_test_only_network_gate_rejects_dns_and_remote_before_io(
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    arguments: tuple[object, ...],
) -> None:
    hooks: list[object] = []
    monkeypatch.setattr(process_boundary_module.sys, "addaudithook", hooks.append)
    gate = process_boundary_module._NetworkGate(
        ready=True,
        guard_attestation=process_boundary_module._CREDENTIAL_GUARD_ATTESTATION,
    )

    process_boundary_module._install_network_audit_gate(
        gate,
        workload_kind=process_boundary_module.CredentialWorkloadKind.TEST_ONLY,
    )

    io_calls: list[str] = []
    with pytest.raises(CredentialBoundaryError, match="TEST_ONLY_NETWORK_TARGET_FORBIDDEN"):
        hooks[0](event, arguments)  # type: ignore[operator]
        io_calls.append("network")
    assert io_calls == []


@pytest.mark.parametrize(
    ("event", "arguments"),
    [
        ("socket.connect", (object(), ("127.0.0.1", 32123))),
        ("socket.connect", (object(), ("::1", 32123, 0, 0))),
        ("socket.getaddrinfo", ("127.0.0.1", 32123, 0, 0, 0)),
        ("socket.getaddrinfo", ("::1", 32123, 0, 0, 0)),
        ("socket.sendto", (object(), b"x", ("127.0.0.1", 32123))),
    ],
)
def test_test_only_network_gate_preserves_numeric_loopback(
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    arguments: tuple[object, ...],
) -> None:
    hooks: list[object] = []
    monkeypatch.setattr(process_boundary_module.sys, "addaudithook", hooks.append)
    gate = process_boundary_module._NetworkGate(
        ready=True,
        guard_attestation=process_boundary_module._CREDENTIAL_GUARD_ATTESTATION,
    )

    process_boundary_module._install_network_audit_gate(
        gate,
        workload_kind=process_boundary_module.CredentialWorkloadKind.TEST_ONLY,
    )

    hooks[0](event, arguments)  # type: ignore[operator]


def test_test_only_workload_has_a_distinct_os_loopback_only_profile() -> None:
    workload = CredentialWorkload.test_only(_child_script("raise SystemExit(0)"))

    wrapped = build_seatbelt_argv(workload)

    profile = wrapped[2]
    assert "(deny process-fork)" in profile
    assert "(deny network-inbound)" in profile
    assert '(deny network-outbound (require-not (remote ip "localhost:*")))' in profile


def test_production_profile_does_not_inherit_test_loopback_restriction() -> None:
    production_profile = process_boundary_module._seatbelt_profile(
        process_boundary_module.CredentialWorkloadKind.PRODUCTION
    )

    assert "(deny process-fork)" in production_profile
    assert "deny network-outbound" not in production_profile


def test_global_production_lease_blocks_an_independent_session_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_path = tmp_path / "global-production.lock"
    monkeypatch.setattr(
        process_boundary_module,
        "_PRODUCTION_EXECUTION_LEASE_PATH",
        lease_path,
    )
    lease_type = getattr(process_boundary_module, "_ProductionExecutionLease", None)

    assert lease_type is not None
    first = lease_type.acquire()
    try:
        with pytest.raises(GenerationAdmissionError, match="PRODUCTION_EXECUTION_LEASE_HELD"):
            lease_type.acquire()
    finally:
        first.release_after_exact_reap()

    second = lease_type.acquire()
    second.release_after_exact_reap()


def test_production_lease_survives_supervisor_fd_close_until_child_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_path = tmp_path / "global-production.lock"
    monkeypatch.setattr(
        process_boundary_module,
        "_PRODUCTION_EXECUTION_LEASE_PATH",
        lease_path,
    )
    lease_type = getattr(process_boundary_module, "_ProductionExecutionLease", None)
    assert lease_type is not None
    lease = lease_type.acquire()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        close_fds=True,
        pass_fds=(lease.fileno(),),
    )
    try:
        lease._close_supervisor_copy()
        with pytest.raises(GenerationAdmissionError, match="PRODUCTION_EXECUTION_LEASE_HELD"):
            lease_type.acquire()
    finally:
        child.terminate()
        child.wait(timeout=5)

    recovered = lease_type.acquire()
    recovered.release_after_exact_reap()


def test_supervisor_derives_each_phase_absolute_deadline_from_same_lifecycle() -> None:
    clock = MutableClock(100.0)
    deadline = AbsoluteDeadline(at=110.0, clock=clock)

    assert deadline.authorize_phase(5.0) == 105.0
    clock.value = 104.0
    assert deadline.authorize_phase(5.0) == 109.0
    clock.value = 109.25
    assert deadline.authorize_phase(5.0) == 110.0


@pytest.mark.parametrize("local_limit", [0.0, -1.0, float("inf"), float("nan")])
def test_absolute_deadline_rejects_invalid_local_limits(local_limit: float) -> None:
    deadline = AbsoluteDeadline(at=110.0, clock=lambda: 100.0)

    with pytest.raises(ValueError, match="LOCAL_LIMIT_MUST_BE_FINITE_POSITIVE"):
        deadline.clamp(local_limit)


def test_ipc_round_trip_is_versioned_sequenced_and_digest_bound() -> None:
    codec = IPCCodec()
    frame = codec.encode("ADMISSION", {"generation": 3, "deadline": 123.25}, sequence=7)

    message = codec.decode(frame, expected_sequence=7)

    assert message.version == 1
    assert message.sequence == 7
    assert message.kind == "ADMISSION"
    assert message.payload == {"generation": 3, "deadline": 123.25}
    assert len(message.digest) == 64


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda frame: frame[:2], "IPC_TRUNCATED_HEADER"),
        (lambda frame: frame[:-1], "IPC_TRUNCATED_BODY"),
        (
            lambda frame: struct.pack(">I", DEFAULT_MAX_IPC_FRAME_BYTES + 1),
            "IPC_FRAME_OVERSIZED",
        ),
        (lambda frame: _replace_envelope(frame, version=2), "IPC_VERSION_MISMATCH"),
        (lambda frame: _replace_envelope(frame, sequence=9), "IPC_SEQUENCE_MISMATCH"),
        (lambda frame: _replace_envelope(frame, digest="0" * 64), "IPC_DIGEST_MISMATCH"),
    ],
)
def test_ipc_corruption_fails_closed(mutate, reason: str) -> None:
    codec = IPCCodec()
    valid = codec.encode("READY", {"safe": True}, sequence=0)

    with pytest.raises(IPCProtocolError, match=reason):
        codec.decode(mutate(valid), expected_sequence=0)


def test_ipc_eof_fails_closed() -> None:
    codec = IPCCodec()

    with pytest.raises(IPCProtocolError, match="IPC_EOF"):
        codec.read(io.BytesIO(b""), expected_sequence=0)


@pytest.mark.parametrize(
    ("changes", "expected_sequence", "reason"),
    [
        ({"version": True}, 0, "IPC_VERSION_MISMATCH"),
        ({"sequence": True}, 1, "IPC_SEQUENCE_MISMATCH"),
    ],
)
def test_ipc_rejects_boolean_version_or_sequence(
    changes: dict[str, object], expected_sequence: int, reason: str
) -> None:
    codec = IPCCodec()
    valid = codec.encode("READY", {"safe": True}, sequence=0)

    with pytest.raises(IPCProtocolError, match=reason):
        codec.decode(_replace_envelope(valid, **changes), expected_sequence=expected_sequence)


@pytest.mark.parametrize(
    "payload",
    [
        {"api_key": "raw-value"},
        {"nested": {"signature": "signed-material"}},
        {"headers": {"X-MBX-APIKEY": "raw-value"}},
        {"signed_url": "https://example.invalid/order?signature=abc"},
    ],
)
def test_ipc_rejects_credential_derived_request_material(payload: dict[str, object]) -> None:
    with pytest.raises(CredentialBoundaryError, match="IPC_CREDENTIAL_MATERIAL_FORBIDDEN"):
        IPCCodec().encode("RESULT", payload, sequence=0)


def test_ipc_rejects_child_known_credential_canary_without_serializing_it() -> None:
    canary = "credential-canary-never-crosses-boundary"
    codec = IPCCodec(forbidden_values=(canary,))

    with pytest.raises(CredentialBoundaryError, match="IPC_CREDENTIAL_MATERIAL_FORBIDDEN"):
        codec.encode("RESULT", {"value": canary}, sequence=0)


def test_ipc_allows_nonsecret_authorization_identity() -> None:
    codec = IPCCodec()

    frame = codec.encode(
        "ATTEMPT",
        {"authorization_id": "recovery-auth-17", "reservation_id": "reserve-3"},
        sequence=0,
    )

    assert codec.decode(frame, expected_sequence=0).payload["authorization_id"] == (
        "recovery-auth-17"
    )


def test_hard_deadline_exits_a_stuck_child_with_default_sigalrm() -> None:
    script = """
import time
from global_quant.gate1b.process_boundary import AbsoluteDeadline, ChildHardDeadline
deadline = AbsoluteDeadline(time.monotonic() + 0.20)
ChildHardDeadline.install(deadline)
time.sleep(30)
"""
    started = time.monotonic()
    process = subprocess.run(
        _child_script(script),
        cwd=PROJECT_ROOT,
        env=_child_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=3,
        check=False,
    )

    assert process.returncode == -signal.SIGALRM
    assert time.monotonic() - started < 2.0


def test_hard_deadline_consumes_sequenced_permits_but_never_past_ceiling() -> None:
    script = """
import signal
import time
from global_quant.gate1b.process_boundary import (
    AbsoluteDeadline,
    ChildHardDeadline,
    PhaseDeadlinePermit,
    ProcessBoundaryError,
)
initial = time.monotonic() + 2.0
timer = ChildHardDeadline.install(AbsoluteDeadline(initial))
first_phase = time.monotonic() + 0.20
first = PhaseDeadlinePermit.issue(
    generation=1, sequence=0, absolute_deadline=first_phase, lifecycle_deadline=initial
)
timer._arm_permit(first, generation=1)
second_phase = time.monotonic() + 0.40
assert second_phase > first_phase
second = PhaseDeadlinePermit.issue(
    generation=1, sequence=1, absolute_deadline=second_phase, lifecycle_deadline=initial
)
timer._arm_permit(second, generation=1)
try:
    timer._arm_permit(first, generation=1)
except ProcessBoundaryError as exc:
    assert str(exc) == "PHASE_PERMIT_MISMATCH"
else:
    raise AssertionError("replayed permit accepted")
signal.signal(signal.SIGALRM, signal.SIG_IGN)
try:
    timer.assert_intact()
except ProcessBoundaryError as exc:
    assert str(exc) == "HARD_DEADLINE_TAMPERED"
else:
    raise AssertionError("tampering accepted")
signal.signal(signal.SIGALRM, signal.SIG_DFL)
"""

    process = subprocess.run(
        _child_script(script),
        cwd=PROJECT_ROOT,
        env=_child_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )

    assert process.returncode == 0, process.stderr
    assert process.stdout == ""
    assert process.stderr == ""


@pytest.mark.skipif(sys.platform != "darwin", reason="target containment proof is macOS-specific")
def test_outer_seatbelt_is_single_and_rejects_nested_sandbox_exec() -> None:
    argv = _child_script("pass")
    workload = CredentialWorkload.test_only(argv)

    wrapped = build_seatbelt_argv(workload)

    assert wrapped[:3] == [str(SANDBOX_EXEC), "-p", wrapped[2]]
    assert wrapped.count(str(SANDBOX_EXEC)) == 1
    assert "(deny process-fork)" in wrapped[2]
    with pytest.raises(ProcessBoundaryError, match="NESTED_SANDBOX_EXEC_FORBIDDEN"):
        CredentialWorkload.test_only([str(SANDBOX_EXEC), "-p", "(version 1)", *argv])


@pytest.mark.skipif(sys.platform != "darwin", reason="target containment proof is macOS-specific")
def test_child_bootstrap_denies_fork_and_posix_spawn_and_starts_own_session() -> None:
    script = """
import time
from global_quant.gate1b.process_boundary import credential_child_bootstrap
bootstrap = credential_child_bootstrap()
bootstrap.channel.send("PHASE", {"phase": "pre-write"})
time.sleep(30)
"""
    supervisor = _supervisor(lifecycle_deadline=time.monotonic() + 5.0)
    child = supervisor.launch(CredentialWorkload.test_only(_child_script(script)), generation=1)
    try:
        message = child.channel.receive()
        assert message.kind == "PHASE"
        assert message.payload == {"phase": "pre-write"}
        assert child.identity.pid == child.identity.pgid == child.identity.sid
        assert child.identity.ppid == os.getpid()
        assert child.process.stdout is None
        assert child.process.stderr is None
    finally:
        attestation = supervisor.kill_and_reap(child)

    assert attestation.signal == signal.SIGKILL
    assert attestation.local_process_quiesced is True
    assert attestation.venue_mutation_absent_proven is False
    assert is_same_process_alive(child.identity) is False
    with pytest.raises(ChildProcessError):
        os.waitpid(child.identity.pid, os.WNOHANG)


@pytest.mark.skipif(sys.platform != "darwin", reason="target containment proof is macOS-specific")
@pytest.mark.parametrize(
    "phase",
    ["dns", "connect", "tls", "pre-write", "partial-write", "read", "parse"],
)
def test_supervisor_sigkills_and_exactly_reaps_every_stuck_phase(phase: str) -> None:
    script = f"""
import time
from global_quant.gate1b.process_boundary import credential_child_bootstrap
bootstrap = credential_child_bootstrap()
bootstrap.channel.send("PHASE", {{"phase": {phase!r}}})
time.sleep(30)
"""
    supervisor = _supervisor(lifecycle_deadline=time.monotonic() + 5.0)
    child = supervisor.launch(CredentialWorkload.test_only(_child_script(script)), generation=1)
    assert child.channel.receive().payload == {"phase": phase}

    attestation = supervisor.kill_and_reap(child, local_limit=1.0)

    assert attestation.identity == child.identity
    assert attestation.returncode == -signal.SIGKILL
    assert not is_same_process_alive(child.identity)


@pytest.mark.skipif(sys.platform != "darwin", reason="target containment proof is macOS-specific")
def test_generation_admission_rejects_a_live_or_unreaped_old_identity() -> None:
    script = """
import time
from global_quant.gate1b.process_boundary import credential_child_bootstrap
credential_child_bootstrap()
time.sleep(30)
"""
    supervisor = _supervisor(lifecycle_deadline=time.monotonic() + 5.0)
    workload = CredentialWorkload.test_only(_child_script(script))
    first = supervisor.launch(workload, generation=1)
    try:
        with pytest.raises(GenerationAdmissionError, match="OLD_GENERATION_STILL_PRESENT"):
            supervisor.launch(workload, generation=2)
    finally:
        supervisor.kill_and_reap(first)

    second = supervisor.launch(workload, generation=2)
    supervisor.kill_and_reap(second)


def test_generation_zero_is_never_mutation_capable() -> None:
    supervisor = _supervisor(lifecycle_deadline=time.monotonic() + 5.0)

    with pytest.raises(ValueError, match="GENERATION_MUST_BE_POSITIVE"):
        supervisor.launch(CredentialWorkload.test_only(_child_script("pass")), generation=0)


@pytest.mark.skipif(sys.platform != "darwin", reason="target capability proof is macOS-specific")
def test_generation_capability_round_trips_and_recovery_child_rejects_create() -> None:
    script = """
from global_quant.gate1b.execution_journal import MutationKind
from global_quant.gate1b.process_boundary import CredentialBoundaryError, credential_child_bootstrap
bootstrap = credential_child_bootstrap()
try:
    bootstrap.assert_mutation_allowed(MutationKind.CREATE)
except CredentialBoundaryError:
    create_allowed = False
else:
    create_allowed = True
bootstrap.channel.send(
    "CAPABILITY",
    {"capability": bootstrap.capability.value, "create_allowed": create_allowed},
)
"""
    supervisor = _supervisor(lifecycle_deadline=time.monotonic() + 8.0)
    workload = CredentialWorkload.test_only(_child_script(script))

    primary = supervisor.launch(workload, generation=1)
    assert primary.capability is GenerationCapability.PRIMARY
    assert primary.channel.receive().payload == {
        "capability": "PRIMARY",
        "create_allowed": True,
    }
    supervisor.reap(primary)

    recovery = supervisor.launch(workload, generation=2)
    assert recovery.capability is GenerationCapability.RECOVERY
    assert recovery.channel.receive().payload == {
        "capability": "RECOVERY",
        "create_allowed": False,
    }
    supervisor.reap(recovery)


@pytest.mark.skipif(sys.platform != "darwin", reason="target containment proof is macOS-specific")
def test_reap_timeout_escalates_to_sigkill_and_exact_reap() -> None:
    script = """
import time
from global_quant.gate1b.process_boundary import credential_child_bootstrap
credential_child_bootstrap()
time.sleep(30)
"""
    supervisor = _supervisor(lifecycle_deadline=time.monotonic() + 5.0)
    child = supervisor.launch(CredentialWorkload.test_only(_child_script(script)), generation=1)

    attestation = supervisor.reap(child, local_limit=0.01)

    assert attestation.signal == signal.SIGKILL
    assert attestation.returncode == -signal.SIGKILL
    assert not is_same_process_alive(child.identity)


@pytest.mark.skipif(sys.platform != "darwin", reason="target containment proof is macOS-specific")
def test_failed_escalation_keeps_generation_gate_closed(monkeypatch) -> None:
    script = """
import time
from global_quant.gate1b.process_boundary import credential_child_bootstrap
credential_child_bootstrap()
time.sleep(30)
"""
    supervisor = _supervisor(lifecycle_deadline=time.monotonic() + 5.0)
    workload = CredentialWorkload.test_only(_child_script(script))
    child = supervisor.launch(workload, generation=1)
    real_wait = child.process.wait

    def always_timeout(timeout=None):
        raise subprocess.TimeoutExpired(child.launch_argv, timeout)

    monkeypatch.setattr(child.process, "wait", always_timeout)
    with pytest.raises(ProcessBoundaryError, match="EXACT_REAP_NOT_PROVEN"):
        supervisor.reap(child, local_limit=0.01)
    with pytest.raises(GenerationAdmissionError, match="OLD_GENERATION_STILL_PRESENT"):
        supervisor.launch(workload, generation=2)

    monkeypatch.setattr(child.process, "wait", real_wait)
    supervisor.kill_and_reap(child)


@pytest.mark.skipif(sys.platform != "darwin", reason="target containment proof is macOS-specific")
def test_identity_is_durably_staged_before_admission(tmp_path: Path) -> None:
    post_admission_marker = tmp_path / "post-admission"
    stage_observations: list[tuple[int, object]] = []
    execution_journal = ExecutionJournal(tmp_path / "execution.jsonl")

    def stage_identity(generation, identity, receipt) -> None:
        assert not post_admission_marker.exists()
        assert receipt.identity == identity
        stage_observations.append((generation, identity))

    script = f"""
from pathlib import Path
from global_quant.gate1b.process_boundary import credential_child_bootstrap
credential_child_bootstrap()
Path({str(post_admission_marker)!r}).write_text("admitted", encoding="ascii")
"""
    supervisor = CredentialProcessSupervisor(
        lifecycle_journal=ProcessLifecycleJournal.start(
            tmp_path / "lifecycle.jsonl",
            lifecycle_started_at=time.monotonic(),
            lifecycle_deadline=time.monotonic() + 5.0,
            execution_journal_path=execution_journal.path,
        ),
        execution_journal=execution_journal,
        parent_environment=_child_environment(),
        on_identity_staged=stage_identity,
        allow_test_workloads=True,
    )

    child = supervisor.launch(CredentialWorkload.test_only(_child_script(script)), generation=1)
    attestation = supervisor.reap(child)

    assert stage_observations == [(1, child.identity)]
    assert post_admission_marker.read_text(encoding="ascii") == "admitted"
    assert attestation.returncode == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="target containment proof is macOS-specific")
def test_failed_durable_admission_hook_prevents_admission_and_reaps_child(tmp_path) -> None:
    staged_identities = []
    execution_journal = ExecutionJournal(tmp_path / "execution.jsonl")

    def reject_stage(generation, identity, receipt) -> None:
        staged_identities.append(identity)
        raise ProcessBoundaryError("PROCESS_IDENTITY_NOT_DURABLE")

    supervisor = CredentialProcessSupervisor(
        lifecycle_journal=ProcessLifecycleJournal.start(
            tmp_path / "lifecycle.jsonl",
            lifecycle_started_at=time.monotonic(),
            lifecycle_deadline=time.monotonic() + 5.0,
            execution_journal_path=execution_journal.path,
        ),
        execution_journal=execution_journal,
        parent_environment=_child_environment(),
        on_identity_staged=reject_stage,
        allow_test_workloads=True,
    )
    script = """
from global_quant.gate1b.process_boundary import credential_child_bootstrap
credential_child_bootstrap()
raise AssertionError("admission must not arrive")
"""

    with pytest.raises(ProcessBoundaryError, match="PROCESS_IDENTITY_NOT_DURABLE"):
        supervisor.launch(CredentialWorkload.test_only(_child_script(script)), generation=1)

    assert len(staged_identities) == 1
    assert not is_same_process_alive(staged_identities[0])


@pytest.mark.skipif(sys.platform != "darwin", reason="target TTY proof is macOS-specific")
def test_launch_can_connect_child_stdin_directly_to_hidden_input_tty() -> None:
    master_fd, slave_fd = pty.openpty()
    try:
        supervisor = _supervisor(
            lifecycle_deadline=time.monotonic() + 5.0,
            credential_stdin=slave_fd,
        )
        script = """
import os
from global_quant.gate1b.process_boundary import credential_child_bootstrap
bootstrap = credential_child_bootstrap()
bootstrap.channel.send("TTY", {"stdin_is_tty": os.isatty(0)})
"""
        child = supervisor.launch(CredentialWorkload.test_only(_child_script(script)), generation=1)
        message = child.channel.receive()
        attestation = supervisor.reap(child)
    finally:
        os.close(master_fd)
        os.close(slave_fd)

    assert message.payload == {"stdin_is_tty": True}
    assert attestation.returncode == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="target orphan-reap proof is macOS-specific")
def test_child_hard_timer_bounds_orphan_after_supervisor_sigkill(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "orphan-child.pid"
    lifecycle_path = tmp_path / "orphan-lifecycle.jsonl"
    execution_path = tmp_path / "orphan-execution.jsonl"
    child_script = f"""
import os
import time
from pathlib import Path
from global_quant.gate1b.process_boundary import credential_child_bootstrap
credential_child_bootstrap()
Path({str(child_pid_path)!r}).write_text(str(os.getpid()), encoding="ascii")
time.sleep(30)
"""
    supervisor_script = f"""
import time
from global_quant.gate1b.process_boundary import (
    CredentialProcessSupervisor,
    CredentialWorkload,
    ProcessLifecycleJournal,
)
from global_quant.gate1b.execution_journal import ExecutionJournal
journal_execution = ExecutionJournal({str(execution_path)!r})
journal = ProcessLifecycleJournal.start(
    {str(lifecycle_path)!r},
    lifecycle_started_at=time.monotonic(),
    lifecycle_deadline=time.monotonic() + 0.35,
    execution_journal_path=journal_execution.path,
)
supervisor = CredentialProcessSupervisor(
    lifecycle_journal=journal,
    execution_journal=journal_execution,
    parent_environment={_child_environment()!r},
    allow_test_workloads=True,
)
supervisor.launch(CredentialWorkload.test_only({_child_script(child_script)!r}), generation=1)
time.sleep(30)
"""
    supervisor = subprocess.Popen(
        _child_script(supervisor_script),
        cwd=PROJECT_ROOT,
        env=_child_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 2.0
        child_pid: int | None = None
        while child_pid is None and time.monotonic() < deadline:
            try:
                raw_pid = child_pid_path.read_text(encoding="ascii")
                parsed_pid = int(raw_pid)
                if parsed_pid > 0:
                    child_pid = parsed_pid
            except (FileNotFoundError, ValueError):
                pass
            if child_pid is None:
                time.sleep(0.01)
        assert child_pid is not None
        identity = read_process_identity(child_pid)
        assert identity is not None

        os.kill(supervisor.pid, signal.SIGKILL)
        assert supervisor.wait(timeout=1.0) == -signal.SIGKILL

        disappearance_deadline = time.monotonic() + 2.0
        while is_same_process_alive(identity) and time.monotonic() < disappearance_deadline:
            time.sleep(0.01)
        assert is_same_process_alive(identity) is False
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=1.0)


@pytest.mark.skipif(
    sys.platform != "darwin", reason="target credential-boundary proof is macOS-specific"
)
def test_credential_canary_is_absent_from_argv_env_stdio_and_ipc() -> None:
    canary = "credential-canary-never-leaks"
    script = """
import os
import sys
from global_quant.gate1b.process_boundary import credential_child_bootstrap
bootstrap = credential_child_bootstrap()
canary = "-".join(("credential", "canary", "never", "leaks"))
bootstrap.channel.send(
    "CANARY_CHECK",
    {"present": canary in repr((sys.argv, dict(os.environ)))},
)
"""
    parent_environment = _child_environment(GMAQ_TEST_CREDENTIAL_CANARY=canary)
    supervisor = _supervisor(
        lifecycle_deadline=time.monotonic() + 5.0,
        parent_environment=parent_environment,
    )

    child = supervisor.launch(CredentialWorkload.test_only(_child_script(script)), generation=1)
    message = child.channel.receive()
    attestation = supervisor.reap(child)

    assert message.payload == {"present": False}
    assert attestation.returncode == 0
    assert canary not in "\0".join(child.launch_argv)
    assert child.process.stdout is None
    assert child.process.stderr is None
    admission = IPCCodec().encode(
        "ADMISSION",
        {"generation": 1, "deadline": time.monotonic() + 1.0},
        sequence=0,
    )
    assert canary.encode() not in admission


def test_supervisor_refuses_to_run_if_credential_named_environment_exists(tmp_path) -> None:
    parent_environment = _child_environment(BINANCE_DEMO_API_KEY="must-not-be-read")
    execution_journal = ExecutionJournal(tmp_path / "execution.jsonl")

    with pytest.raises(CredentialBoundaryError, match="SUPERVISOR_CREDENTIAL_ENVIRONMENT_PRESENT"):
        CredentialProcessSupervisor(
            lifecycle_journal=ProcessLifecycleJournal.start(
                tmp_path / "lifecycle.jsonl",
                lifecycle_started_at=time.monotonic(),
                lifecycle_deadline=time.monotonic() + 5.0,
                execution_journal_path=execution_journal.path,
            ),
            execution_journal=execution_journal,
            parent_environment=parent_environment,
        )


def test_read_process_identity_matches_current_process() -> None:
    identity = read_process_identity(os.getpid())

    assert identity is not None
    assert identity.pid == os.getpid()
    assert identity.ppid == os.getppid()
    assert identity.pgid == os.getpgid(0)
    assert identity.sid == os.getsid(0)
    assert is_same_process_alive(identity)


def test_build_seatbelt_rejects_untyped_argv() -> None:
    with pytest.raises(ProcessBoundaryError, match="CREDENTIAL_WORKLOAD_TYPE_REQUIRED"):
        build_seatbelt_argv([])  # type: ignore[arg-type]


def test_ipc_decode_rejects_noncanonical_or_extra_fields() -> None:
    codec = IPCCodec()
    frame = codec.encode("READY", {"safe": True}, sequence=0)
    (length,) = struct.unpack(">I", frame[:4])
    envelope = json.loads(frame[4 : 4 + length])
    envelope["unexpected"] = True
    malformed = _replace_envelope(frame, **envelope)

    with pytest.raises(IPCProtocolError, match="IPC_ENVELOPE_FIELDS_INVALID"):
        codec.decode(malformed, expected_sequence=0)


def test_process_death_attestation_never_claims_venue_non_dispatch() -> None:
    script = """
from global_quant.gate1b.process_boundary import credential_child_bootstrap
credential_child_bootstrap()
"""
    if sys.platform != "darwin":
        pytest.skip("target containment proof is macOS-specific")
    supervisor = _supervisor(lifecycle_deadline=time.monotonic() + 5.0)
    child = supervisor.launch(CredentialWorkload.test_only(_child_script(script)), generation=1)

    attestation = supervisor.reap(child)

    assert attestation.local_process_quiesced is True
    assert attestation.venue_mutation_absent_proven is False
    assert "venue" not in attestation.proves


def test_lifecycle_journal_restores_original_deadline_and_active_identity(tmp_path) -> None:
    path = tmp_path / "process-lifecycle.jsonl"
    execution_journal = ExecutionJournal(tmp_path / "execution.jsonl")
    original_started_at = time.monotonic()
    original_deadline = time.monotonic() + 30.0
    journal = ProcessLifecycleJournal.start(
        path,
        lifecycle_started_at=original_started_at,
        lifecycle_deadline=original_deadline,
        execution_journal_path=execution_journal.path,
    )
    identity = read_process_identity(os.getpid())
    assert identity is not None
    receipt = journal.stage_identity(1, identity)

    restored = ProcessLifecycleJournal.restore(path)
    genesis = json.loads(path.read_text(encoding="ascii").splitlines()[0])

    assert restored.lifecycle_started_at == original_started_at
    assert restored.lifecycle_deadline == original_deadline
    assert genesis["event"]["lifecycle_started_at"] == original_started_at
    assert genesis["event"]["lifecycle_started_at"] != original_deadline - 180.0
    assert restored.active_identity == identity
    restored.verify_receipt(receipt)
    with pytest.raises(ProcessBoundaryError, match="PROCESS_JOURNAL_RECEIPT_MISMATCH"):
        restored.verify_receipt(replace(receipt, journal_digest="0" * 64))
    with pytest.raises(ProcessBoundaryError, match="PROCESS_LIFECYCLE_ALREADY_EXISTS"):
        ProcessLifecycleJournal.start(
            path,
            lifecycle_started_at=time.monotonic(),
            lifecycle_deadline=time.monotonic() + 180.0,
            execution_journal_path=execution_journal.path,
        )


@pytest.mark.parametrize(
    ("lifecycle_started_at", "lifecycle_deadline"),
    [
        (float("nan"), 200.0),
        (float("inf"), 200.0),
        (True, 200.0),
        (200.0, 200.0),
        (201.0, 200.0),
    ],
)
def test_lifecycle_journal_rejects_invalid_exact_start_authority(
    tmp_path: Path,
    lifecycle_started_at: object,
    lifecycle_deadline: float,
) -> None:
    with pytest.raises(ValueError, match="LIFECYCLE_STARTED_AT_INVALID"):
        ProcessLifecycleJournal.start(
            tmp_path / "lifecycle.jsonl",
            lifecycle_started_at=lifecycle_started_at,  # type: ignore[arg-type]
            lifecycle_deadline=lifecycle_deadline,
            execution_journal_path=tmp_path / "execution.jsonl",
        )


def test_lifecycle_journal_rejects_semantically_tampered_start_with_valid_hash_and_head(
    tmp_path: Path,
) -> None:
    path = tmp_path / "lifecycle.jsonl"
    lifecycle = ProcessLifecycleJournal.start(
        path,
        lifecycle_started_at=123.5,
        lifecycle_deadline=222.25,
        execution_journal_path=tmp_path / "execution.jsonl",
    )
    encoded = json.loads(path.read_text(encoding="ascii"))
    encoded["event"]["lifecycle_started_at"] = encoded["event"]["lifecycle_deadline"]
    core = {key: value for key, value in encoded.items() if key != "digest"}
    encoded["digest"] = hashlib.sha256(
        json.dumps(
            core,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()
    path.write_text(
        json.dumps(
            encoded,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n",
        encoding="ascii",
    )
    head = json.loads(lifecycle.head_path.read_text(encoding="ascii"))
    head["digest"] = encoded["digest"]
    lifecycle.head_path.write_text(
        json.dumps(head, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii",
    )

    with pytest.raises(ProcessBoundaryError, match="PROCESS_JOURNAL_GENESIS_INVALID"):
        ProcessLifecycleJournal.restore(path)


def test_unadmitted_staged_identity_disappearance_reuses_generation_with_new_stage(
    tmp_path: Path,
) -> None:
    execution_journal = ExecutionJournal(tmp_path / "execution.jsonl")
    lifecycle = ProcessLifecycleJournal.start(
        tmp_path / "lifecycle.jsonl",
        lifecycle_started_at=time.monotonic(),
        lifecycle_deadline=time.monotonic() + 30.0,
        execution_journal_path=execution_journal.path,
    )
    first_identity = ProcessIdentity(987_654_301, 1, 987_654_301, 987_654_301, "stage-1")
    first = lifecycle.stage_identity(1, first_identity)
    supervisor = CredentialProcessSupervisor(
        lifecycle_journal=ProcessLifecycleJournal.restore(lifecycle.path),
        execution_journal=execution_journal,
        parent_environment=_child_environment(),
        allow_test_workloads=True,
    )

    supervisor.attest_previous_disappearance()
    second_identity = ProcessIdentity(987_654_302, 1, 987_654_302, 987_654_302, "stage-2")
    second = lifecycle.stage_identity(1, second_identity)

    assert first.generation == second.generation == 1
    assert (first.stage_ordinal, second.stage_ordinal) == (1, 2)
    assert lifecycle.last_generation == 0


def test_crash_after_execution_admit_recovers_binding_and_reaps_before_next_generation(
    tmp_path: Path,
) -> None:
    execution_journal = ExecutionJournal(tmp_path / "execution.jsonl")
    lifecycle_deadline = time.monotonic() - 0.01
    lifecycle = ProcessLifecycleJournal.start(
        tmp_path / "lifecycle.jsonl",
        lifecycle_started_at=lifecycle_deadline - 30.0,
        lifecycle_deadline=lifecycle_deadline,
        execution_journal_path=execution_journal.path,
    )
    identity = ProcessIdentity(987_654_303, 1, 987_654_303, 987_654_303, "admitted")
    lifecycle.stage_identity(1, identity)
    admission_record = execution_journal.admit_generation(
        DurableGenerationAdmission(1, identity.sha256),
        GenerationCapability.PRIMARY,
    )

    supervisor = CredentialProcessSupervisor(
        lifecycle_journal=ProcessLifecycleJournal.restore(lifecycle.path),
        execution_journal=execution_journal,
        parent_environment=_child_environment(),
        allow_test_workloads=True,
    )
    supervisor.attest_previous_disappearance()

    assert lifecycle.active_identity is None
    assert lifecycle.last_generation == 1
    reaped = execution_journal.records()[-1].event
    assert type(reaped).__name__ == "_GenerationReaped"
    assert reaped.receipt.generation == 1
    assert reaped.receipt.process_identity_sha256 == identity.sha256
    assert reaped.receipt.admission_record_sequence == admission_record.sequence
    assert reaped.receipt.admission_record_digest == admission_record.digest


def test_process_journal_refuses_to_append_over_a_mismatched_durable_head(
    tmp_path: Path,
) -> None:
    execution_journal = ExecutionJournal(tmp_path / "execution.jsonl")
    lifecycle = ProcessLifecycleJournal.start(
        tmp_path / "lifecycle.jsonl",
        lifecycle_started_at=time.monotonic(),
        lifecycle_deadline=time.monotonic() + 30.0,
        execution_journal_path=execution_journal.path,
    )
    head = json.loads(lifecycle.head_path.read_text(encoding="ascii"))
    head["digest"] = "0" * 64
    lifecycle.head_path.write_text(
        json.dumps(head, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii",
    )

    with pytest.raises(ProcessBoundaryError, match="PROCESS_JOURNAL_HEAD_DIGEST_MISMATCH"):
        lifecycle.stage_identity(
            1,
            ProcessIdentity(987_654_304, 1, 987_654_304, 987_654_304, "head-mismatch"),
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="target reap proof is macOS-specific")
def test_reap_attestation_is_publicly_verified_against_durable_process_head() -> None:
    script = """
from global_quant.gate1b.process_boundary import credential_child_bootstrap
credential_child_bootstrap()
"""
    supervisor = _supervisor(lifecycle_deadline=time.monotonic() + 5.0)
    child = supervisor.launch(CredentialWorkload.test_only(_child_script(script)), generation=1)

    attestation = supervisor.reap(child)
    restored = ProcessLifecycleJournal.restore(attestation.process_journal_path)
    restored.verify_reap_attestation(attestation)

    assert attestation.attested_monotonic_ns > 0
    assert attestation.stage_ordinal == 1
    with pytest.raises(ProcessBoundaryError, match="PROCESS_REAP_ATTESTATION_MISMATCH"):
        restored.verify_reap_attestation(
            replace(attestation, attested_monotonic_ns=attestation.attested_monotonic_ns + 1)
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="target timer proof is macOS-specific")
def test_supervisor_phase_permit_enforces_actual_sigkill_before_child_timer() -> None:
    script = """
import time
from global_quant.gate1b.process_boundary import credential_child_bootstrap
bootstrap = credential_child_bootstrap()
bootstrap.channel.send("PHASE_READY", {"ready": True})
bootstrap.accept_phase_permit()
time.sleep(30)
"""
    supervisor = _supervisor(lifecycle_deadline=time.monotonic() + 5.0)
    child = supervisor.launch(CredentialWorkload.test_only(_child_script(script)), generation=1)
    assert child.channel.receive().payload == {"ready": True}

    supervisor.issue_phase_permit(child, local_limit=0.25)
    attestation = supervisor.reap(child, local_limit=1.0)

    assert attestation.signal == signal.SIGKILL
    assert attestation.returncode == -signal.SIGKILL


@pytest.mark.skipif(sys.platform != "darwin", reason="target containment proof is macOS-specific")
def test_seatbelt_argv_always_runs_fixed_trusted_bootstrap_before_test_workload() -> None:
    workload = CredentialWorkload.test_only(_child_script("raise SystemExit(0)"))

    wrapped = build_seatbelt_argv(workload)

    bootstrap_index = wrapped.index("--gmaq-trusted-bootstrap")
    workload_index = wrapped.index("--", bootstrap_index)
    assert (
        Path(wrapped[bootstrap_index - 1]).resolve()
        == Path(
            __import__("global_quant.gate1b.process_boundary", fromlist=["__file__"]).__file__
        ).resolve()
    )
    assert tuple(wrapped[workload_index + 1 :]) == workload.argv
    assert wrapped.count(str(SANDBOX_EXEC)) == 1
    with pytest.raises(ProcessBoundaryError, match="CREDENTIAL_WORKLOAD_TYPE_REQUIRED"):
        build_seatbelt_argv(_child_script("pass"))  # type: ignore[arg-type]


def test_production_workload_is_exact_allowlisted_blob_not_arbitrary_argv(tmp_path) -> None:
    candidate = tmp_path / "credential_session.py"
    candidate.write_text("raise SystemExit(0)\n", encoding="ascii")
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()

    with pytest.raises(ProcessBoundaryError, match="PRODUCTION_WORKLOAD_PATH_NOT_ALLOWLISTED"):
        CredentialWorkload.production(candidate, runtime_sha256=digest)

    process_module = __import__("global_quant.gate1b.process_boundary", fromlist=["__file__"])
    legacy_entrypoint = Path(process_module.__file__).with_name("credential_session.py")
    legacy_digest = hashlib.sha256(legacy_entrypoint.read_bytes()).hexdigest()
    workload = CredentialWorkload.production(
        legacy_entrypoint,
        runtime_sha256=legacy_digest,
    )
    assert workload.runtime_path == legacy_entrypoint.resolve()
    assert workload.runtime_sha256 == legacy_digest
    assert workload.argv == (
        sys.executable,
        "-I",
        "-S",
        str(legacy_entrypoint.resolve()),
    )


def test_child_hard_deadline_has_no_arbitrary_float_phase_arm_api() -> None:
    assert not hasattr(ChildHardDeadline, "arm_phase")


@pytest.mark.skipif(sys.platform != "darwin", reason="target TTY proof is macOS-specific")
def test_credential_guard_installs_ipc_canary_and_requires_tty_devnull_policy() -> None:
    script = """
import getpass
from global_quant.gate1b.process_boundary import CredentialBoundaryError, credential_child_bootstrap
bootstrap = credential_child_bootstrap()
bootstrap.channel.send("PROMPT_READY", {"ready": True})
secret = getpass.getpass("")
bootstrap.install_credential_guard(secret)
try:
    bootstrap.channel.send("LEAK", {"value": secret})
except CredentialBoundaryError:
    bootstrap.channel.send("GUARD", {"installed": True})
else:
    raise AssertionError("credential crossed IPC")
"""
    master_fd, slave_fd = pty.openpty()
    journal_path = Path(tempfile.mkdtemp(prefix="gmaq-process-test-")) / "lifecycle.jsonl"
    execution_journal = ExecutionJournal(journal_path.with_name("execution.jsonl"))
    journal = ProcessLifecycleJournal.start(
        journal_path,
        lifecycle_started_at=time.monotonic(),
        lifecycle_deadline=time.monotonic() + 5.0,
        execution_journal_path=execution_journal.path,
    )
    supervisor = CredentialProcessSupervisor(
        lifecycle_journal=journal,
        execution_journal=execution_journal,
        parent_environment=_child_environment(),
        credential_stdin=slave_fd,
        allow_test_workloads=True,
    )
    try:
        child = supervisor.launch(CredentialWorkload.test_only(_child_script(script)), generation=1)
        assert child.channel.receive().payload == {"ready": True}
        # getpass disables echo with TCSAFLUSH.  Writing before that transition
        # races with the intentional input flush and can leave the child
        # waiting until its hard timer.  Wait for the real terminal state that
        # a human observes as the hidden prompt before supplying the canary.
        tty_ready_deadline = time.monotonic() + 1.0
        while termios.tcgetattr(slave_fd)[3] & termios.ECHO:
            if time.monotonic() >= tty_ready_deadline:
                pytest.fail("credential prompt never disabled terminal echo")
        os.write(master_fd, b"credential-canary-from-tty\n")
        os.close(slave_fd)
        slave_fd = -1
        assert child.channel.receive().payload == {"installed": True}
        os.close(master_fd)
        master_fd = -1
        attestation = supervisor.reap(child)
        assert attestation.returncode in {0, -signal.SIGHUP}
        assert attestation.local_process_quiesced is True
    finally:
        if master_fd >= 0:
            os.close(master_fd)
        if slave_fd >= 0:
            os.close(slave_fd)
