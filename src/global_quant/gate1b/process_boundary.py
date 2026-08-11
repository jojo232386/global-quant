"""Process containment primitives for the frozen Gate 1B v1.6 kernel.

The supervisor side of this module is credential-free.  It owns the sole
absolute monotonic lifecycle deadline and launches one credential-capable
process generation at a time.  The child receives only sanitized control data,
arms a non-cooperative kernel timer before credentials can be read, and proves
that macOS Seatbelt has denied descendant creation.

Process exit is intentionally a local fact.  A reap attestation never claims
that a venue mutation was not dispatched.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, BinaryIO, Self

if TYPE_CHECKING:
    from global_quant.gate1b.execution_journal import GenerationCapability, MutationKind

IPC_VERSION = 1
DEFAULT_MAX_IPC_FRAME_BYTES = 64 * 1024
_CONTROL_FD_ENV = "GMAQ_GATE1B_CONTROL_FD"
_BOOTSTRAP_STATE_ENV = "GMAQ_GATE1B_BOOTSTRAP_STATE"
_PRODUCTION_LEASE_FD_ENV = "GMAQ_GATE1B_PRODUCTION_LEASE_FD"
_TRUSTED_BOOTSTRAP_ARGUMENT = "--gmaq-trusted-bootstrap"
_SEATBELT_EXEC = Path("/usr/bin/sandbox-exec")
_PRODUCTION_SEATBELT_PROFILE = "(version 1)\n(allow default)\n(deny process-fork)"
_TEST_ONLY_SEATBELT_PROFILE = (
    "(version 1)\n"
    "(allow default)\n"
    "(deny process-fork)\n"
    "(deny network-inbound)\n"
    '(deny network-outbound (require-not (remote ip "localhost:*")))'
)
_HANDSHAKE_LOCAL_LIMIT_SECONDS = 2.0
_SUPERVISOR_KILL_LEAD_SECONDS = 0.01
_PROCESS_JOURNAL_SCHEMA = "gate1b.process-lifecycle.v1"
_PROCESS_JOURNAL_HEAD_SCHEMA = "gate1b.process-lifecycle-head.v1"
_PROCESS_JOURNAL_MAX_RECORD_BYTES = 8_192
_ZERO_DIGEST = "0" * 64
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PRODUCTION_EXECUTION_LEASE_PATH = Path(
    f"/private/tmp/gmaq-gate1b-v1.6-production-{os.getuid()}.lock"
)
_PRODUCTION_EXECUTION_LEASE_SCHEMA = "gate1b.production-execution-lease.v1"
_PRODUCTION_WORKLOAD_PATH = Path(__file__).resolve().with_name("credential_session.py")
# Exact reviewed fixed credential entrypoint.  Callers cannot nominate another
# script or bless changed bytes by supplying their own digest.
_PRODUCTION_WORKLOAD_SHA256: str | None = (
    "c0532c9dbc068b42337823504a8c9f4482d60def406f0fd53e57af7884d0331b"
)
_ALLOWED_CHILD_ENVIRONMENT_NAMES = frozenset(
    {"PATH", "PYTHONPATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"}
)
_CREDENTIAL_ENVIRONMENT_NAMES = frozenset(
    {
        "BINANCE_DEMO_API_KEY",
        "BINANCE_DEMO_API_SECRET",
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
        "BINANCE_FUTURES_TESTNET_API_KEY",
        "BINANCE_FUTURES_TESTNET_API_SECRET",
    }
)
_FORBIDDEN_IPC_KEY = re.compile(
    r"(?:^|_)(?:api_?key|secret|signature|authorization|headers?|signed_?url|"
    r"signed_?request|request_?body|query_?string|credential)(?:_|$)",
    re.IGNORECASE,
)
_FORBIDDEN_SIGNED_VALUE = re.compile(
    r"(?:x-mbx-apikey|authorization\s*:|[?&]signature=)", re.IGNORECASE
)
_MESSAGE_KIND = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_ENVELOPE_FIELDS = frozenset({"version", "sequence", "kind", "payload", "digest"})


class ProcessBoundaryError(RuntimeError):
    """The process containment contract could not be proven."""


class DeadlineExpired(ProcessBoundaryError):
    """The supervisor-issued lifecycle deadline has no remaining time."""


class IPCProtocolError(ProcessBoundaryError):
    """Sanitized control IPC failed closed."""


class CredentialBoundaryError(ProcessBoundaryError):
    """Credential material reached a credential-free boundary."""


class GenerationAdmissionError(ProcessBoundaryError):
    """A new credential process was attempted before the old one disappeared."""


class CredentialWorkloadKind(StrEnum):
    PRODUCTION = "PRODUCTION"
    TEST_ONLY = "TEST_ONLY"


@dataclass(frozen=True, slots=True)
class CredentialWorkload:
    """Exact reviewed credential entrypoint or an explicitly test-only workload."""

    kind: CredentialWorkloadKind
    argv: tuple[str, ...]
    runtime_path: Path | None
    runtime_sha256: str | None

    def __post_init__(self) -> None:
        if type(self.kind) is not CredentialWorkloadKind or not self.argv:
            raise ProcessBoundaryError("CREDENTIAL_WORKLOAD_INVALID")
        if any(type(argument) is not str or not argument for argument in self.argv):
            raise ProcessBoundaryError("CREDENTIAL_WORKLOAD_INVALID")
        if any(Path(argument).name == _SEATBELT_EXEC.name for argument in self.argv):
            raise ProcessBoundaryError("NESTED_SANDBOX_EXEC_FORBIDDEN")
        if self.kind is CredentialWorkloadKind.PRODUCTION:
            if (
                _PRODUCTION_WORKLOAD_SHA256 is None
                or self.runtime_sha256 != _PRODUCTION_WORKLOAD_SHA256
                or self.runtime_path is None
                or self.runtime_path.resolve() != _PRODUCTION_WORKLOAD_PATH
                or self.runtime_sha256 is None
                or _SHA256.fullmatch(self.runtime_sha256) is None
                or self.argv
                != (
                    sys.executable,
                    "-I",
                    "-S",
                    str(_PRODUCTION_WORKLOAD_PATH),
                )
            ):
                raise ProcessBoundaryError("PRODUCTION_WORKLOAD_INVALID")
            try:
                actual = hashlib.sha256(self.runtime_path.read_bytes()).hexdigest()
            except OSError as exc:
                raise ProcessBoundaryError("PRODUCTION_WORKLOAD_UNAVAILABLE") from exc
            if not hmac.compare_digest(actual, self.runtime_sha256):
                raise ProcessBoundaryError("PRODUCTION_WORKLOAD_DIGEST_MISMATCH")
        elif self.runtime_path is not None or self.runtime_sha256 is not None:
            raise ProcessBoundaryError("TEST_WORKLOAD_RUNTIME_BINDING_FORBIDDEN")

    @classmethod
    def production(
        cls,
        runtime_path: str | os.PathLike[str],
        *,
        runtime_sha256: str,
    ) -> Self:
        path = Path(runtime_path).resolve()
        if path != _PRODUCTION_WORKLOAD_PATH:
            raise ProcessBoundaryError("PRODUCTION_WORKLOAD_PATH_NOT_ALLOWLISTED")
        if _PRODUCTION_WORKLOAD_SHA256 is None:
            raise ProcessBoundaryError("PRODUCTION_WORKLOAD_NOT_FROZEN")
        if not hmac.compare_digest(runtime_sha256, _PRODUCTION_WORKLOAD_SHA256):
            raise ProcessBoundaryError("PRODUCTION_WORKLOAD_DIGEST_NOT_ALLOWLISTED")
        return cls(
            kind=CredentialWorkloadKind.PRODUCTION,
            argv=(sys.executable, "-I", "-S", str(path)),
            runtime_path=path,
            runtime_sha256=runtime_sha256,
        )

    @classmethod
    def test_only(cls, argv: Sequence[str]) -> Self:
        return cls(
            kind=CredentialWorkloadKind.TEST_ONLY,
            argv=tuple(argv),
            runtime_path=None,
            runtime_sha256=None,
        )


@dataclass(frozen=True)
class AbsoluteDeadline:
    """One absolute monotonic lifecycle deadline; it can never be restarted."""

    at: float
    clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.at):
            raise ValueError("LIFECYCLE_DEADLINE_MUST_BE_FINITE")

    def remaining(self) -> float:
        return max(0.0, self.at - self.clock())

    def clamp(self, local_limit: float) -> float:
        if not math.isfinite(local_limit) or local_limit <= 0:
            raise ValueError("LOCAL_LIMIT_MUST_BE_FINITE_POSITIVE")
        remaining = self.at - self.clock()
        if remaining <= 0:
            raise DeadlineExpired("LIFECYCLE_DEADLINE_EXHAUSTED")
        return min(local_limit, remaining)

    def authorize_phase(self, local_limit: float) -> float:
        """Derive a phase absolute deadline without creating a new lifecycle."""

        now = self.clock()
        if not math.isfinite(local_limit) or local_limit <= 0:
            raise ValueError("LOCAL_LIMIT_MUST_BE_FINITE_POSITIVE")
        if self.at - now <= 0:
            raise DeadlineExpired("LIFECYCLE_DEADLINE_EXHAUSTED")
        return min(self.at, now + local_limit)


@dataclass(frozen=True, slots=True)
class PhaseDeadlinePermit:
    """Sequenced controller authority for one child phase deadline."""

    generation: int
    sequence: int
    absolute_deadline: float
    lifecycle_deadline: float
    digest: str

    @classmethod
    def issue(
        cls,
        *,
        generation: int,
        sequence: int,
        absolute_deadline: float,
        lifecycle_deadline: float,
    ) -> Self:
        material = {
            "absolute_deadline": absolute_deadline,
            "generation": generation,
            "lifecycle_deadline": lifecycle_deadline,
            "sequence": sequence,
        }
        digest = hashlib.sha256(_canonical_json(material)).hexdigest()
        return cls(digest=digest, **material)

    def __post_init__(self) -> None:
        if (
            type(self.generation) is not int
            or self.generation <= 0
            or type(self.sequence) is not int
            or self.sequence < 0
            or type(self.absolute_deadline) not in {int, float}
            or not math.isfinite(float(self.absolute_deadline))
            or type(self.lifecycle_deadline) not in {int, float}
            or not math.isfinite(float(self.lifecycle_deadline))
            or self.absolute_deadline > self.lifecycle_deadline
            or type(self.digest) is not str
            or _SHA256.fullmatch(self.digest) is None
        ):
            raise ProcessBoundaryError("PHASE_PERMIT_INVALID")
        material = {
            "absolute_deadline": self.absolute_deadline,
            "generation": self.generation,
            "lifecycle_deadline": self.lifecycle_deadline,
            "sequence": self.sequence,
        }
        expected = hashlib.sha256(_canonical_json(material)).hexdigest()
        if not hmac.compare_digest(expected, self.digest):
            raise ProcessBoundaryError("PHASE_PERMIT_DIGEST_MISMATCH")

    def to_payload(self) -> dict[str, int | float | str]:
        return {
            "generation": self.generation,
            "sequence": self.sequence,
            "absolute_deadline": self.absolute_deadline,
            "lifecycle_deadline": self.lifecycle_deadline,
            "digest": self.digest,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        if set(payload) != {
            "generation",
            "sequence",
            "absolute_deadline",
            "lifecycle_deadline",
            "digest",
        }:
            raise ProcessBoundaryError("PHASE_PERMIT_INVALID")
        return cls(
            generation=payload["generation"],  # type: ignore[arg-type]
            sequence=payload["sequence"],  # type: ignore[arg-type]
            absolute_deadline=payload["absolute_deadline"],  # type: ignore[arg-type]
            lifecycle_deadline=payload["lifecycle_deadline"],  # type: ignore[arg-type]
            digest=payload["digest"],  # type: ignore[arg-type]
        )


@dataclass
class ChildHardDeadline:
    """Child-owned SIGALRM backstop installed before credential acquisition.

    The public API accepts supervisor-derived phase deadlines under one
    immutable lifecycle ceiling, but exposes no cancellation.  Safety call
    sites must use :meth:`assert_intact` immediately before mutation I/O; it
    detects a changed handler, a blocked signal, a cancelled timer, or a timer
    whose estimated expiry moved past the currently authorized phase.
    """

    deadline: AbsoluteDeadline
    _armed_at: float
    _next_permit_sequence: int = 0

    @classmethod
    def install(cls, deadline: AbsoluteDeadline) -> Self:
        if threading.current_thread() is not threading.main_thread():
            raise ProcessBoundaryError("HARD_DEADLINE_REQUIRES_MAIN_THREAD")
        remaining = deadline.at - deadline.clock()
        if remaining <= 0:
            raise DeadlineExpired("LIFECYCLE_DEADLINE_EXHAUSTED")
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        if hasattr(signal, "pthread_sigmask"):
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
        signal.setitimer(signal.ITIMER_REAL, remaining, 0.0)
        timer = cls(deadline=deadline, _armed_at=deadline.at)
        timer.assert_intact()
        return timer

    @classmethod
    def resume(cls, deadline: AbsoluteDeadline) -> Self:
        """Re-attest the inherited timer immediately after trusted exec."""

        timer = cls(deadline=deadline, _armed_at=deadline.at)
        timer.assert_intact()
        return timer

    def _arm_permit(self, permit: PhaseDeadlinePermit, *, generation: int) -> None:
        if type(permit) is not PhaseDeadlinePermit:
            raise ProcessBoundaryError("PHASE_PERMIT_REQUIRED")
        if (
            permit.generation != generation
            or permit.sequence != self._next_permit_sequence
            or permit.lifecycle_deadline != self.deadline.at
        ):
            raise ProcessBoundaryError("PHASE_PERMIT_MISMATCH")
        if permit.absolute_deadline > self.deadline.at:
            raise ProcessBoundaryError("HARD_DEADLINE_CEILING_EXCEEDED")
        remaining = permit.absolute_deadline - self.deadline.clock()
        if remaining <= 0:
            raise DeadlineExpired("LIFECYCLE_DEADLINE_EXHAUSTED")
        self._armed_at = permit.absolute_deadline
        self._next_permit_sequence += 1
        signal.setitimer(signal.ITIMER_REAL, remaining, 0.0)
        self.assert_intact()

    def assert_intact(self) -> None:
        if signal.getsignal(signal.SIGALRM) != signal.SIG_DFL:
            raise ProcessBoundaryError("HARD_DEADLINE_TAMPERED")
        if hasattr(signal, "pthread_sigmask"):
            blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())
            if signal.SIGALRM in blocked:
                raise ProcessBoundaryError("HARD_DEADLINE_TAMPERED")
        remaining, interval = signal.getitimer(signal.ITIMER_REAL)
        if remaining <= 0 or interval != 0:
            raise ProcessBoundaryError("HARD_DEADLINE_TAMPERED")
        estimated_expiry = self.deadline.clock() + remaining
        if estimated_expiry > self._armed_at + 0.05:
            raise ProcessBoundaryError("HARD_DEADLINE_TAMPERED")


@dataclass(frozen=True)
class IPCMessage:
    version: int
    sequence: int
    kind: str
    payload: dict[str, Any]
    digest: str


class IPCCodec:
    """Canonical, length-bounded, sequenced and digest-bound JSON framing."""

    def __init__(
        self,
        *,
        max_frame_bytes: int = DEFAULT_MAX_IPC_FRAME_BYTES,
        forbidden_values: Sequence[str] = (),
    ) -> None:
        if type(max_frame_bytes) is not int or max_frame_bytes <= 0:
            raise ValueError("IPC_MAX_FRAME_BYTES_INVALID")
        self.max_frame_bytes = max_frame_bytes
        self._forbidden_values = tuple(value for value in forbidden_values if value)

    def encode(self, kind: str, payload: Mapping[str, Any], *, sequence: int) -> bytes:
        if not isinstance(kind, str) or _MESSAGE_KIND.fullmatch(kind) is None:
            raise IPCProtocolError("IPC_MESSAGE_KIND_INVALID")
        if type(sequence) is not int or sequence < 0:
            raise IPCProtocolError("IPC_SEQUENCE_INVALID")
        if not isinstance(payload, Mapping):
            raise IPCProtocolError("IPC_PAYLOAD_INVALID")
        materialized = dict(payload)
        self._assert_sanitized(materialized)
        core = {
            "kind": kind,
            "payload": materialized,
            "sequence": sequence,
            "version": IPC_VERSION,
        }
        digest = hashlib.sha256(_canonical_json(core)).hexdigest()
        envelope = {**core, "digest": digest}
        encoded = _canonical_json(envelope)
        if len(encoded) > self.max_frame_bytes:
            raise IPCProtocolError("IPC_FRAME_OVERSIZED")
        return struct.pack(">I", len(encoded)) + encoded

    def decode(self, frame: bytes, *, expected_sequence: int) -> IPCMessage:
        if len(frame) < 4:
            raise IPCProtocolError("IPC_TRUNCATED_HEADER")
        (length,) = struct.unpack(">I", frame[:4])
        if length > self.max_frame_bytes:
            raise IPCProtocolError("IPC_FRAME_OVERSIZED")
        if len(frame) < 4 + length:
            raise IPCProtocolError("IPC_TRUNCATED_BODY")
        if len(frame) != 4 + length:
            raise IPCProtocolError("IPC_TRAILING_BYTES")
        body = frame[4:]
        try:
            envelope = json.loads(
                body,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise IPCProtocolError("IPC_JSON_INVALID") from exc
        if not isinstance(envelope, dict) or frozenset(envelope) != _ENVELOPE_FIELDS:
            raise IPCProtocolError("IPC_ENVELOPE_FIELDS_INVALID")
        if type(envelope["version"]) is not int or envelope["version"] != IPC_VERSION:
            raise IPCProtocolError("IPC_VERSION_MISMATCH")
        if type(envelope["sequence"]) is not int or envelope["sequence"] != expected_sequence:
            raise IPCProtocolError("IPC_SEQUENCE_MISMATCH")
        if (
            envelope["sequence"] < 0
            or not isinstance(envelope["kind"], str)
            or _MESSAGE_KIND.fullmatch(envelope["kind"]) is None
            or not isinstance(envelope["payload"], dict)
            or not isinstance(envelope["digest"], str)
        ):
            raise IPCProtocolError("IPC_ENVELOPE_VALUE_INVALID")
        self._assert_sanitized(envelope["payload"])
        core = {
            "kind": envelope["kind"],
            "payload": envelope["payload"],
            "sequence": envelope["sequence"],
            "version": envelope["version"],
        }
        expected_digest = hashlib.sha256(_canonical_json(core)).hexdigest()
        if not hmac.compare_digest(envelope["digest"], expected_digest):
            raise IPCProtocolError("IPC_DIGEST_MISMATCH")
        if body != _canonical_json(envelope):
            raise IPCProtocolError("IPC_NONCANONICAL_ENCODING")
        return IPCMessage(
            version=IPC_VERSION,
            sequence=envelope["sequence"],
            kind=envelope["kind"],
            payload=envelope["payload"],
            digest=envelope["digest"],
        )

    def read(self, stream: BinaryIO | socket.socket, *, expected_sequence: int) -> IPCMessage:
        header = _read_exact(
            stream,
            4,
            empty_reason="IPC_EOF",
            partial_reason="IPC_TRUNCATED_HEADER",
        )
        (length,) = struct.unpack(">I", header)
        if length > self.max_frame_bytes:
            raise IPCProtocolError("IPC_FRAME_OVERSIZED")
        body = _read_exact(
            stream,
            length,
            empty_reason="IPC_TRUNCATED_BODY",
            partial_reason="IPC_TRUNCATED_BODY",
        )
        return self.decode(header + body, expected_sequence=expected_sequence)

    def _assert_sanitized(self, value: Any, *, key: str | None = None) -> None:
        if (
            key is not None
            and key.casefold() != "authorization_id"
            and _FORBIDDEN_IPC_KEY.search(key)
        ):
            raise CredentialBoundaryError("IPC_CREDENTIAL_MATERIAL_FORBIDDEN")
        if value is None or type(value) in {bool, int}:
            return
        if type(value) is float:
            if not math.isfinite(value):
                raise IPCProtocolError("IPC_NONFINITE_NUMBER_FORBIDDEN")
            return
        if isinstance(value, str):
            if _FORBIDDEN_SIGNED_VALUE.search(value) or any(
                forbidden in value for forbidden in self._forbidden_values
            ):
                raise CredentialBoundaryError("IPC_CREDENTIAL_MATERIAL_FORBIDDEN")
            return
        if isinstance(value, list):
            for item in value:
                self._assert_sanitized(item)
            return
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if not isinstance(nested_key, str):
                    raise IPCProtocolError("IPC_PAYLOAD_KEY_INVALID")
                self._assert_sanitized(nested_value, key=nested_key)
            return
        raise IPCProtocolError("IPC_PAYLOAD_TYPE_FORBIDDEN")


class IPCChannel:
    """One duplex channel with independent strict send/receive sequences."""

    def __init__(
        self,
        transport: socket.socket,
        codec: IPCCodec | None = None,
        *,
        send_sequence: int = 0,
        receive_sequence: int = 0,
    ) -> None:
        if (
            type(send_sequence) is not int
            or send_sequence < 0
            or type(receive_sequence) is not int
            or receive_sequence < 0
        ):
            raise IPCProtocolError("IPC_SEQUENCE_INVALID")
        self._transport = transport
        self._codec = codec if codec is not None else IPCCodec()
        self._send_sequence = send_sequence
        self._receive_sequence = receive_sequence

    def send(self, kind: str, payload: Mapping[str, Any]) -> None:
        frame = self._codec.encode(kind, payload, sequence=self._send_sequence)
        try:
            self._transport.sendall(frame)
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            raise IPCProtocolError("IPC_WRITE_FAILED") from exc
        self._send_sequence += 1

    def receive(self) -> IPCMessage:
        try:
            message = self._codec.read(
                self._transport,
                expected_sequence=self._receive_sequence,
            )
        except TimeoutError as exc:
            raise IPCProtocolError("IPC_TIMEOUT") from exc
        self._receive_sequence += 1
        return message

    def install_forbidden_values(self, values: Sequence[str]) -> None:
        """Install child-known credential canaries without resetting IPC sequence."""

        if any(type(value) is not str or not value for value in values):
            raise CredentialBoundaryError("CREDENTIAL_CANARY_INVALID")
        self._codec = IPCCodec(
            max_frame_bytes=self._codec.max_frame_bytes,
            forbidden_values=tuple(values),
        )

    def fileno(self) -> int:
        return self._transport.fileno()

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    ppid: int
    pgid: int
    sid: int
    start_token: str

    def to_payload(self) -> dict[str, int | str]:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "pgid": self.pgid,
            "sid": self.sid,
            "start_token": self.start_token,
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_payload())).hexdigest()

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        if set(payload) != {"pid", "ppid", "pgid", "sid", "start_token"}:
            raise ProcessBoundaryError("CHILD_IDENTITY_INVALID")
        if any(type(payload[name]) is not int for name in ("pid", "ppid", "pgid", "sid")):
            raise ProcessBoundaryError("CHILD_IDENTITY_INVALID")
        if not isinstance(payload["start_token"], str) or not payload["start_token"]:
            raise ProcessBoundaryError("CHILD_IDENTITY_INVALID")
        return cls(
            pid=payload["pid"],
            ppid=payload["ppid"],
            pgid=payload["pgid"],
            sid=payload["sid"],
            start_token=payload["start_token"],
        )


def read_process_identity(pid: int) -> ProcessIdentity | None:
    """Read PID plus non-reusable start identity without spawning a helper."""

    if type(pid) is not int or pid <= 0:
        raise ValueError("PID_MUST_BE_POSITIVE")
    if sys.platform == "darwin":
        return _read_darwin_identity(pid)
    if sys.platform.startswith("linux"):
        return _read_linux_identity(pid)
    raise ProcessBoundaryError("PROCESS_START_IDENTITY_UNSUPPORTED")


def is_same_process_alive(identity: ProcessIdentity) -> bool:
    current = read_process_identity(identity.pid)
    return current is not None and current.start_token == identity.start_token


@dataclass(frozen=True, slots=True)
class DurableIdentityReceipt:
    """Identity admission bound to an owner-only journal record and durable head."""

    generation: int
    stage_ordinal: int
    identity: ProcessIdentity
    journal_path: Path
    journal_sequence: int
    journal_digest: str
    head_sequence: int
    head_digest: str

    def __post_init__(self) -> None:
        if (
            type(self.generation) is not int
            or self.generation <= 0
            or type(self.stage_ordinal) is not int
            or self.stage_ordinal <= 0
            or type(self.identity) is not ProcessIdentity
            or not isinstance(self.journal_path, Path)
            or type(self.journal_sequence) is not int
            or self.journal_sequence <= 0
            or type(self.head_sequence) is not int
            or self.head_sequence < self.journal_sequence
            or _SHA256.fullmatch(self.journal_digest) is None
            or _SHA256.fullmatch(self.head_digest) is None
        ):
            raise ProcessBoundaryError("PROCESS_JOURNAL_RECEIPT_INVALID")


@dataclass(frozen=True, slots=True)
class _ProcessJournalRecord:
    sequence: int
    previous_digest: str
    event: dict[str, Any]
    digest: str


@dataclass(frozen=True, slots=True)
class _ProcessJournalState:
    lifecycle_started_at: float
    lifecycle_deadline: float
    execution_journal_path: Path
    active_identity: ProcessIdentity | None
    active_generation: int | None
    active_stage_ordinal: int | None
    active_admission_committed: bool
    last_generation: int
    last_stage_ordinal: int


class ProcessLifecycleJournal:
    """Small durable containment journal; restart cannot mint a new lifecycle."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    def start(
        cls,
        path: str | os.PathLike[str],
        *,
        lifecycle_started_at: float,
        lifecycle_deadline: float,
        execution_journal_path: str | os.PathLike[str],
    ) -> Self:
        journal = cls(Path(path))
        if type(lifecycle_deadline) not in {int, float} or not math.isfinite(
            float(lifecycle_deadline)
        ):
            raise ValueError("LIFECYCLE_DEADLINE_MUST_BE_FINITE")
        if (
            type(lifecycle_started_at) not in {int, float}
            or not math.isfinite(float(lifecycle_started_at))
            or lifecycle_started_at >= lifecycle_deadline
        ):
            raise ValueError("LIFECYCLE_STARTED_AT_INVALID")
        journal._path.parent.mkdir(parents=False, exist_ok=True)
        flags = (
            os.O_RDWR
            | os.O_APPEND
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            fd = os.open(journal._path, flags, 0o600)
        except FileExistsError as exc:
            raise ProcessBoundaryError("PROCESS_LIFECYCLE_ALREADY_EXISTS") from exc
        except OSError as exc:
            raise ProcessBoundaryError("PROCESS_JOURNAL_CREATE_FAILED") from exc
        try:
            os.fchmod(fd, 0o600)
            _validate_owner_file(os.fstat(fd), "PROCESS_JOURNAL")
            record, encoded = _build_process_record(
                sequence=1,
                previous_digest=_ZERO_DIGEST,
                event={
                    "type": "LIFECYCLE_STARTED",
                    "lifecycle_started_at": lifecycle_started_at,
                    "lifecycle_deadline": lifecycle_deadline,
                    "boot_token": _boot_token(),
                    "execution_journal_path": str(Path(execution_journal_path).resolve()),
                },
            )
            _write_all(fd, encoded)
            os.fsync(fd)
            journal._write_head(record)
        except BaseException:
            raise
        finally:
            os.close(fd)
        return cls.restore(journal._path)

    @classmethod
    def restore(cls, path: str | os.PathLike[str]) -> Self:
        journal = cls(Path(path))
        journal._read_state()
        return journal

    @property
    def path(self) -> Path:
        return self._path

    @property
    def head_path(self) -> Path:
        return self._path.with_name(f"{self._path.name}.head")

    @property
    def lifecycle_started_at(self) -> float:
        return self._read_state().lifecycle_started_at

    @property
    def lifecycle_deadline(self) -> float:
        return self._read_state().lifecycle_deadline

    @property
    def active_identity(self) -> ProcessIdentity | None:
        return self._read_state().active_identity

    @property
    def active_generation(self) -> int | None:
        return self._read_state().active_generation

    @property
    def active_admission_committed(self) -> bool:
        return self._read_state().active_admission_committed

    @property
    def execution_journal_path(self) -> Path:
        return self._read_state().execution_journal_path

    @property
    def last_generation(self) -> int:
        return self._read_state().last_generation

    def stage_identity(
        self,
        generation: int,
        identity: ProcessIdentity,
    ) -> DurableIdentityReceipt:
        if type(generation) is not int or generation <= 0 or type(identity) is not ProcessIdentity:
            raise ProcessBoundaryError("PROCESS_IDENTITY_STAGE_INVALID")
        stage_ordinal = self._read_state().last_stage_ordinal + 1
        record = self._append(
            {
                "type": "IDENTITY_STAGED",
                "generation": generation,
                "stage_ordinal": stage_ordinal,
                "identity": identity.to_payload(),
                "process_identity_sha256": identity.sha256,
            }
        )
        receipt = DurableIdentityReceipt(
            generation=generation,
            stage_ordinal=stage_ordinal,
            identity=identity,
            journal_path=self._path,
            journal_sequence=record.sequence,
            journal_digest=record.digest,
            head_sequence=record.sequence,
            head_digest=record.digest,
        )
        self.verify_receipt(receipt)
        return receipt

    def record_execution_admission(
        self,
        *,
        generation: int,
        identity: ProcessIdentity,
        execution_journal: Any,
        admission_record: Any,
    ) -> _ProcessJournalRecord:
        proof = _validate_execution_admission(
            expected_path=self.execution_journal_path,
            generation=generation,
            identity=identity,
            execution_journal=execution_journal,
            admission_record=admission_record,
        )
        return self._append(
            {
                "type": "EXECUTION_ADMISSION_COMMITTED",
                "generation": generation,
                "stage_ordinal": self._read_state().active_stage_ordinal,
                "process_identity_sha256": identity.sha256,
                **proof,
            }
        )

    def record_reap(
        self,
        *,
        generation: int,
        identity: ProcessIdentity,
        returncode: int,
        signal_number: int | None,
        execution_journal: Any | None = None,
        execution_reap_record: Any | None = None,
    ) -> _ProcessJournalRecord:
        state = self._read_state()
        execution_proof = self._execution_reap_proof(
            state=state,
            generation=generation,
            identity=identity,
            execution_journal=execution_journal,
            execution_reap_record=execution_reap_record,
        )
        return self._append(
            {
                "type": "IDENTITY_REAPED",
                "generation": generation,
                "stage_ordinal": state.active_stage_ordinal,
                "process_identity_sha256": identity.sha256,
                "waited_pid": identity.pid,
                "returncode": returncode,
                "signal": signal_number,
                "exact_pid_waited": True,
                "descendant_creation_denied": True,
                "local_process_quiesced": True,
                "venue_mutation_absent_proven": False,
                "attested_monotonic_ns": time.monotonic_ns(),
                **execution_proof,
            }
        )

    def record_orphan_disappearance(
        self,
        *,
        generation: int,
        identity: ProcessIdentity,
        execution_journal: Any | None = None,
        execution_reap_record: Any | None = None,
    ) -> _ProcessJournalRecord:
        if is_same_process_alive(identity):
            raise GenerationAdmissionError("OLD_GENERATION_STILL_PRESENT")
        state = self._read_state()
        execution_proof = self._execution_reap_proof(
            state=state,
            generation=generation,
            identity=identity,
            execution_journal=execution_journal,
            execution_reap_record=execution_reap_record,
        )
        return self._append(
            {
                "type": "IDENTITY_ORPHAN_DISAPPEARED",
                "generation": generation,
                "stage_ordinal": state.active_stage_ordinal,
                "process_identity_sha256": identity.sha256,
                "pid1_reap_observed": True,
                "descendant_creation_denied": True,
                "local_process_quiesced": True,
                "venue_mutation_absent_proven": False,
                "attested_monotonic_ns": time.monotonic_ns(),
                **execution_proof,
            }
        )

    def verify_reap_attestation(self, attestation: ReapAttestation) -> None:
        """Verify an exact reap against this journal's durable chain and head."""

        if (
            type(attestation) is not ReapAttestation
            or attestation.process_journal_path.resolve() != self._path.resolve()
        ):
            raise ProcessBoundaryError("PROCESS_REAP_ATTESTATION_MISMATCH")
        records, head = self._read_records_and_head()
        if not 0 < attestation.journal_sequence <= len(records):
            raise ProcessBoundaryError("PROCESS_REAP_ATTESTATION_MISMATCH")
        record = records[attestation.journal_sequence - 1]
        expected = {
            "type": "IDENTITY_REAPED",
            "generation": attestation.generation,
            "stage_ordinal": attestation.stage_ordinal,
            "process_identity_sha256": attestation.process_identity_sha256,
            "waited_pid": attestation.waited_pid,
            "returncode": attestation.returncode,
            "signal": attestation.signal,
            "exact_pid_waited": attestation.exact_pid_waited,
            "descendant_creation_denied": attestation.descendant_creation_denied,
            "local_process_quiesced": attestation.local_process_quiesced,
            "venue_mutation_absent_proven": attestation.venue_mutation_absent_proven,
            "attested_monotonic_ns": attestation.attested_monotonic_ns,
            "execution_journal_sequence": attestation.execution_journal_sequence,
            "execution_journal_digest": attestation.execution_journal_digest,
            "execution_head_sequence": attestation.execution_head_sequence,
            "execution_head_digest": attestation.execution_head_digest,
        }
        if (
            record.digest != attestation.journal_digest
            or record.event != expected
            or attestation.journal_head_sequence != attestation.journal_sequence
            or attestation.journal_head_digest != attestation.journal_digest
            or head[0] < attestation.journal_head_sequence
            or records[attestation.journal_head_sequence - 1].digest
            != attestation.journal_head_digest
            or attestation.identity.sha256 != attestation.process_identity_sha256
            or attestation.waited_pid != attestation.identity.pid
        ):
            raise ProcessBoundaryError("PROCESS_REAP_ATTESTATION_MISMATCH")
        from global_quant.gate1b.execution_journal import ExecutionJournal

        execution_journal = ExecutionJournal(self.execution_journal_path)
        execution_records, execution_head = _validated_execution_records(
            expected_path=self.execution_journal_path,
            execution_journal=execution_journal,
        )
        execution_sequence = attestation.execution_journal_sequence
        stored_head_sequence = attestation.execution_head_sequence
        if (
            type(execution_sequence) is not int
            or not 0 < execution_sequence <= len(execution_records)
            or type(stored_head_sequence) is not int
            or not execution_sequence <= stored_head_sequence <= len(execution_records)
        ):
            raise ProcessBoundaryError("PROCESS_REAP_ATTESTATION_MISMATCH")
        execution_record = execution_records[execution_sequence - 1]
        execution_event = execution_record.event
        execution_receipt = getattr(execution_event, "receipt", None)
        admission_matches_receipt = _execution_admission_matches_receipt(
            records=execution_records,
            generation=attestation.generation,
            identity=attestation.identity,
            receipt=execution_receipt,
        )
        if (
            execution_record.digest != attestation.execution_journal_digest
            or type(execution_event).__name__ != "_GenerationReaped"
            or not admission_matches_receipt
            or execution_receipt.generation != attestation.generation
            or execution_receipt.process_identity_sha256 != attestation.process_identity_sha256
            or execution_receipt.returncode != attestation.returncode
            or execution_receipt.signal != attestation.signal
            or execution_receipt.local_process_quiesced is not True
            or execution_receipt.venue_mutation_absent_proven is not False
            or execution_records[stored_head_sequence - 1].digest
            != attestation.execution_head_digest
            or execution_head.sequence < stored_head_sequence
        ):
            raise ProcessBoundaryError("PROCESS_REAP_ATTESTATION_MISMATCH")

    def _execution_reap_proof(
        self,
        *,
        state: _ProcessJournalState,
        generation: int,
        identity: ProcessIdentity,
        execution_journal: Any | None,
        execution_reap_record: Any | None,
    ) -> dict[str, int | str | None]:
        if execution_journal is None:
            raise ProcessBoundaryError("EXECUTION_JOURNAL_PROOF_REQUIRED")
        if state.active_admission_committed:
            if execution_reap_record is None:
                raise ProcessBoundaryError("EXECUTION_REAP_PROOF_REQUIRED")
            return _validate_execution_reap(
                expected_path=self.execution_journal_path,
                generation=generation,
                identity=identity,
                execution_journal=execution_journal,
                reap_record=execution_reap_record,
            )
        if execution_reap_record is not None:
            raise ProcessBoundaryError("UNADMITTED_EXECUTION_REAP_FORBIDDEN")
        records, _head = _validated_execution_records(
            expected_path=self.execution_journal_path,
            execution_journal=execution_journal,
        )
        if any(
            type(record.event).__name__ == "_GenerationAdmitted"
            and record.event.generation == generation
            for record in records
        ):
            raise ProcessBoundaryError("UNCOMMITTED_EXECUTION_ADMISSION_PRESENT")
        return {
            "execution_journal_sequence": None,
            "execution_journal_digest": None,
            "execution_head_sequence": None,
            "execution_head_digest": None,
        }

    def verify_receipt(self, receipt: DurableIdentityReceipt) -> None:
        if type(receipt) is not DurableIdentityReceipt or receipt.journal_path != self._path:
            raise ProcessBoundaryError("PROCESS_JOURNAL_RECEIPT_MISMATCH")
        records, head = self._read_records_and_head()
        if receipt.journal_sequence > len(records):
            raise ProcessBoundaryError("PROCESS_JOURNAL_RECEIPT_MISMATCH")
        record = records[receipt.journal_sequence - 1]
        event = record.event
        if (
            record.digest != receipt.journal_digest
            or head != (receipt.head_sequence, receipt.head_digest)
            or receipt.head_sequence != receipt.journal_sequence
            or receipt.head_digest != receipt.journal_digest
            or event.get("type") != "IDENTITY_STAGED"
            or event.get("generation") != receipt.generation
            or event.get("stage_ordinal") != receipt.stage_ordinal
            or event.get("identity") != receipt.identity.to_payload()
            or event.get("process_identity_sha256") != receipt.identity.sha256
        ):
            raise ProcessBoundaryError("PROCESS_JOURNAL_RECEIPT_MISMATCH")

    def _read_state(self) -> _ProcessJournalState:
        records, _head = self._read_records_and_head()
        return _replay_process_records(records)

    def _read_records_and_head(
        self,
    ) -> tuple[tuple[_ProcessJournalRecord, ...], tuple[int, str]]:
        with self._locked_fd() as fd:
            records = _read_process_records(fd)
            head = _read_process_head(self.head_path)
            if head[0] > len(records):
                raise ProcessBoundaryError("PROCESS_JOURNAL_HEAD_AHEAD")
            if records[head[0] - 1].digest != head[1]:
                raise ProcessBoundaryError("PROCESS_JOURNAL_HEAD_DIGEST_MISMATCH")
            if head[0] < len(records):
                latest = records[-1]
                self._write_head(latest)
                head = (latest.sequence, latest.digest)
            return records, head

    def _append(self, event: dict[str, Any]) -> _ProcessJournalRecord:
        with self._locked_fd() as fd:
            records = _read_process_records(fd)
            head = _read_process_head(self.head_path)
            if head[0] > len(records):
                raise ProcessBoundaryError("PROCESS_JOURNAL_HEAD_AHEAD")
            if records[head[0] - 1].digest != head[1]:
                raise ProcessBoundaryError("PROCESS_JOURNAL_HEAD_DIGEST_MISMATCH")
            if head[0] < len(records):
                self._write_head(records[-1])
            _validate_process_event_append(_replay_process_records(records), event)
            previous = records[-1]
            record, encoded = _build_process_record(
                sequence=previous.sequence + 1,
                previous_digest=previous.digest,
                event=event,
            )
            _write_all(fd, encoded)
            os.fsync(fd)
            self._write_head(record)
            return record

    @contextmanager
    def _locked_fd(self):
        try:
            entry = os.stat(self._path, follow_symlinks=False)
            _validate_owner_file(entry, "PROCESS_JOURNAL")
            fd = os.open(
                self._path,
                os.O_RDWR
                | os.O_APPEND
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
        except ProcessBoundaryError:
            raise
        except OSError as exc:
            raise ProcessBoundaryError("PROCESS_JOURNAL_OPEN_FAILED") from exc
        try:
            opened = os.fstat(fd)
            _validate_owner_file(opened, "PROCESS_JOURNAL")
            if (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino):
                raise ProcessBoundaryError("PROCESS_JOURNAL_PATH_RACE")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield fd
        finally:
            os.close(fd)

    def _write_head(self, record: _ProcessJournalRecord) -> None:
        _write_process_head(self.head_path, record.sequence, record.digest)


def build_seatbelt_argv(workload: CredentialWorkload) -> list[str]:
    """Compose one Seatbelt around the fixed trusted bootstrap and typed workload."""

    if type(workload) is not CredentialWorkload:
        raise ProcessBoundaryError("CREDENTIAL_WORKLOAD_TYPE_REQUIRED")
    if not _SEATBELT_EXEC.is_file():
        raise ProcessBoundaryError("SANDBOX_EXEC_UNAVAILABLE")
    mode = workload.kind.value
    digest = workload.runtime_sha256 or "-"
    runtime_path = str(workload.runtime_path) if workload.runtime_path is not None else "-"
    return [
        str(_SEATBELT_EXEC),
        "-p",
        _seatbelt_profile(workload.kind),
        sys.executable,
        "-I",
        "-S",
        str(Path(__file__).resolve()),
        _TRUSTED_BOOTSTRAP_ARGUMENT,
        mode,
        runtime_path,
        digest,
        "--",
        *workload.argv,
    ]


def _seatbelt_profile(workload_kind: CredentialWorkloadKind) -> str:
    if workload_kind is CredentialWorkloadKind.PRODUCTION:
        return _PRODUCTION_SEATBELT_PROFILE
    if workload_kind is CredentialWorkloadKind.TEST_ONLY:
        return _TEST_ONLY_SEATBELT_PROFILE
    raise ProcessBoundaryError("CREDENTIAL_WORKLOAD_KIND_INVALID")


def _assert_descendant_creation_denied() -> None:
    if sys.platform != "darwin":
        raise ProcessBoundaryError("DESCENDANT_CONTAINMENT_UNSUPPORTED")
    try:
        pid = os.fork()
    except OSError as exc:
        if exc.errno != errno.EPERM:
            raise ProcessBoundaryError("FORK_DENIAL_NOT_PROVEN") from exc
    else:
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        raise ProcessBoundaryError("FORK_DENIAL_NOT_PROVEN")
    try:
        pid = os.posix_spawn("/usr/bin/true", ["true"], {})
    except OSError as exc:
        if exc.errno != errno.EPERM:
            raise ProcessBoundaryError("POSIX_SPAWN_DENIAL_NOT_PROVEN") from exc
    else:
        os.waitpid(pid, 0)
        raise ProcessBoundaryError("POSIX_SPAWN_DENIAL_NOT_PROVEN")


@dataclass(slots=True)
class _NetworkGate:
    ready: bool = False
    authority_issued: bool = False
    guard_attestation: object | None = None


_CHILD_BOOTSTRAP_ATTESTATION = object()
_CHILD_IO_AUTHORITY_CONSTRUCTION_TOKEN = object()
_CREDENTIAL_GUARD_ATTESTATION = object()


class ChildIOAuthority:
    """In-memory proof that this exact production child may perform I/O.

    The authority contains no credential or credential-derived material.  It
    can be issued once by the trusted post-exec bootstrap, bound to one
    transport, and is re-attested against the live PID/start identity, guard,
    and hard timer before every transport operation.
    """

    __slots__ = ("_bootstrap", "_bound", "_identity", "_network_gate")

    def __init__(
        self,
        bootstrap: ChildBootstrap,
        *,
        _construction_token: object,
    ) -> None:
        if _construction_token is not _CHILD_IO_AUTHORITY_CONSTRUCTION_TOKEN:
            raise CredentialBoundaryError("CHILD_IO_AUTHORITY_CONSTRUCTION_FORBIDDEN")
        self._bootstrap = bootstrap
        self._identity = bootstrap.identity
        self._network_gate = bootstrap._network_gate
        self._bound = False

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(generation={self._bootstrap.generation}, "
            f"capability={self._bootstrap.capability.value!r}, "
            f"pid={self._identity.pid}, bound={self._bound})"
        )

    def _assert_live(self, *, require_bound: bool) -> None:
        bootstrap = self._bootstrap
        if (
            type(bootstrap) is not ChildBootstrap
            or bootstrap._bootstrap_attestation is not _CHILD_BOOTSTRAP_ATTESTATION
            or bootstrap.workload_kind is not CredentialWorkloadKind.PRODUCTION
            or bootstrap._network_gate is not self._network_gate
            or bootstrap.identity != self._identity
            or self._network_gate.authority_issued is not True
            or self._network_gate.guard_attestation is not _CREDENTIAL_GUARD_ATTESTATION
            or (require_bound and self._bound is not True)
        ):
            raise CredentialBoundaryError("CHILD_IO_AUTHORITY_INVALID")
        if os.getpid() != self._identity.pid:
            raise CredentialBoundaryError("CHILD_IO_AUTHORITY_PROCESS_CHANGED")
        observed = read_process_identity(os.getpid())
        if observed != self._identity:
            raise CredentialBoundaryError("CHILD_IO_AUTHORITY_PROCESS_CHANGED")
        bootstrap.assert_network_ready()

    def _bind_transport(self) -> None:
        self._assert_transport_bindable()
        self._bound = True

    def _assert_transport_bindable(self) -> None:
        self._assert_live(require_bound=False)
        if self._bound:
            raise CredentialBoundaryError("CHILD_IO_AUTHORITY_ALREADY_BOUND")

    def assert_io_authorized(self) -> None:
        """Re-attest the exact child session immediately before transport I/O."""

        self._assert_live(require_bound=True)


@dataclass(frozen=True)
class ChildBootstrap:
    generation: int
    capability: GenerationCapability
    deadline: AbsoluteDeadline
    hard_deadline: ChildHardDeadline
    identity: ProcessIdentity
    channel: IPCChannel = field(compare=False, repr=False)
    workload_kind: CredentialWorkloadKind
    _network_gate: _NetworkGate = field(compare=False, repr=False)
    _bootstrap_attestation: object = field(default=None, compare=False, repr=False)

    def accept_phase_permit(self) -> PhaseDeadlinePermit:
        message = self.channel.receive()
        if message.kind != "PHASE_PERMIT" or not isinstance(message.payload, dict):
            raise ProcessBoundaryError("PHASE_PERMIT_MESSAGE_INVALID")
        permit = PhaseDeadlinePermit.from_payload(message.payload)
        self.hard_deadline._arm_permit(permit, generation=self.generation)
        return permit

    def install_credential_guard(self, *credential_values: str) -> None:
        """Bind known credential canaries before any network-capable operation."""

        if self._network_gate.ready:
            raise CredentialBoundaryError("CREDENTIAL_GUARD_ALREADY_INSTALLED")
        if not credential_values or any(
            type(value) is not str or not value for value in credential_values
        ):
            raise CredentialBoundaryError("CREDENTIAL_CANARY_INVALID")
        self.hard_deadline.assert_intact()
        if not os.isatty(0):
            raise CredentialBoundaryError("CREDENTIAL_INPUT_TTY_REQUIRED")
        if not _is_devnull(1) or not _is_devnull(2):
            raise CredentialBoundaryError("CREDENTIAL_OUTPUT_DEVNULL_REQUIRED")
        exposed_values = [*sys.argv, *os.environ.keys(), *os.environ.values()]
        if any(secret in exposed for secret in credential_values for exposed in exposed_values):
            raise CredentialBoundaryError("CREDENTIAL_PRESENT_IN_PROCESS_METADATA")
        self.channel.install_forbidden_values(credential_values)
        self._network_gate.guard_attestation = _CREDENTIAL_GUARD_ATTESTATION
        self._network_gate.ready = True

    def assert_network_ready(self) -> None:
        self.hard_deadline.assert_intact()
        if (
            not self._network_gate.ready
            or self._network_gate.guard_attestation is not _CREDENTIAL_GUARD_ATTESTATION
        ):
            raise CredentialBoundaryError("CREDENTIAL_GUARD_REQUIRED_BEFORE_NETWORK")

    def issue_io_authority(self) -> ChildIOAuthority:
        """Issue this trusted production child session's sole transport proof."""

        if (
            type(self) is not ChildBootstrap
            or self._bootstrap_attestation is not _CHILD_BOOTSTRAP_ATTESTATION
        ):
            raise CredentialBoundaryError("TRUSTED_CHILD_BOOTSTRAP_REQUIRED")
        if self.workload_kind is not CredentialWorkloadKind.PRODUCTION:
            raise CredentialBoundaryError("PRODUCTION_CHILD_WORKLOAD_REQUIRED")
        if self._network_gate.authority_issued:
            raise CredentialBoundaryError("CHILD_IO_AUTHORITY_ALREADY_ISSUED")
        if os.getpid() != self.identity.pid or read_process_identity(os.getpid()) != self.identity:
            raise CredentialBoundaryError("CHILD_IO_AUTHORITY_PROCESS_CHANGED")
        self.assert_network_ready()
        self._network_gate.authority_issued = True
        return ChildIOAuthority(
            self,
            _construction_token=_CHILD_IO_AUTHORITY_CONSTRUCTION_TOKEN,
        )

    def assert_mutation_allowed(self, mutation_kind: MutationKind) -> None:
        """Enforce the journal-bound generation capability inside the child."""

        from global_quant.gate1b.execution_journal import (
            GenerationCapability,
            MutationKind,
        )

        if type(mutation_kind) is not MutationKind:
            raise CredentialBoundaryError("MUTATION_KIND_INVALID")
        self.hard_deadline.assert_intact()
        if (
            self.capability is GenerationCapability.RECOVERY
            and mutation_kind is MutationKind.CREATE
        ):
            raise CredentialBoundaryError("RECOVERY_CREATE_CAPABILITY_FORBIDDEN")


def credential_child_bootstrap() -> ChildBootstrap:
    """Re-attest the trusted bootstrap as the workload's mandatory first action."""

    raw_state = os.environ.pop(_BOOTSTRAP_STATE_ENV, None)
    raw_fd = os.environ.pop(_CONTROL_FD_ENV, None)
    if raw_state is None or raw_fd is None or not raw_fd.isascii() or not raw_fd.isdigit():
        raise ProcessBoundaryError("TRUSTED_BOOTSTRAP_REQUIRED")
    try:
        state = json.loads(raw_state)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProcessBoundaryError("BOOTSTRAP_STATE_INVALID") from exc
    if not isinstance(state, dict) or set(state) != {
        "capability",
        "deadline",
        "generation",
        "identity",
        "runtime_path",
        "runtime_sha256",
        "workload_kind",
    }:
        raise ProcessBoundaryError("BOOTSTRAP_STATE_INVALID")
    if raw_state.encode("ascii") != _canonical_json(state):
        raise ProcessBoundaryError("BOOTSTRAP_STATE_INVALID")
    generation = state["generation"]
    absolute = state["deadline"]
    if type(generation) is not int or generation <= 0:
        raise ProcessBoundaryError("ADMISSION_GENERATION_INVALID")
    if type(absolute) not in {int, float} or not math.isfinite(float(absolute)):
        raise ProcessBoundaryError("ADMISSION_DEADLINE_INVALID")
    try:
        workload_kind = CredentialWorkloadKind(state["workload_kind"])
        from global_quant.gate1b.execution_journal import GenerationCapability

        capability = GenerationCapability(state["capability"])
    except (TypeError, ValueError) as exc:
        raise ProcessBoundaryError("BOOTSTRAP_STATE_INVALID") from exc
    if capability is not _generation_capability(generation):
        raise ProcessBoundaryError("BOOTSTRAP_CAPABILITY_MISMATCH")
    identity = ProcessIdentity.from_payload(state["identity"])
    observed = read_process_identity(os.getpid())
    if observed != identity:
        raise ProcessBoundaryError("BOOTSTRAP_IDENTITY_CHANGED")
    if workload_kind is CredentialWorkloadKind.PRODUCTION:
        runtime_path = Path(state["runtime_path"]).resolve()
        runtime_sha256 = state["runtime_sha256"]
        if runtime_path != _PRODUCTION_WORKLOAD_PATH or runtime_sha256 is None:
            raise ProcessBoundaryError("PRODUCTION_WORKLOAD_INVALID")
        try:
            actual = hashlib.sha256(runtime_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ProcessBoundaryError("PRODUCTION_WORKLOAD_UNAVAILABLE") from exc
        if not hmac.compare_digest(actual, runtime_sha256):
            raise ProcessBoundaryError("PRODUCTION_WORKLOAD_DIGEST_MISMATCH")
    deadline = AbsoluteDeadline(float(absolute))
    hard_deadline = ChildHardDeadline.resume(deadline)
    transport = socket.socket(fileno=int(raw_fd))
    channel = IPCChannel(transport, send_sequence=1, receive_sequence=1)
    network_gate = _NetworkGate()
    _install_network_audit_gate(network_gate, workload_kind=workload_kind)
    return ChildBootstrap(
        generation=generation,
        capability=capability,
        deadline=deadline,
        hard_deadline=hard_deadline,
        identity=identity,
        channel=channel,
        workload_kind=workload_kind,
        _network_gate=network_gate,
        _bootstrap_attestation=_CHILD_BOOTSTRAP_ATTESTATION,
    )


@dataclass(frozen=True, slots=True)
class _InitialChildBootstrap:
    generation: int
    capability: str
    deadline: AbsoluteDeadline
    hard_deadline: ChildHardDeadline
    identity: ProcessIdentity
    channel: IPCChannel = field(compare=False, repr=False)


def _initial_credential_child_bootstrap() -> _InitialChildBootstrap:
    raw_fd = os.environ.pop(_CONTROL_FD_ENV, None)
    if raw_fd is None or not raw_fd.isascii() or not raw_fd.isdigit():
        raise ProcessBoundaryError("CONTROL_FD_MISSING")
    transport = socket.socket(fileno=int(raw_fd))
    channel = IPCChannel(transport)
    admission = channel.receive()
    if admission.kind != "ADMISSION" or set(admission.payload) != {
        "generation",
        "capability",
        "deadline",
    }:
        raise ProcessBoundaryError("ADMISSION_MESSAGE_INVALID")
    generation = admission.payload["generation"]
    capability = admission.payload["capability"]
    absolute = admission.payload["deadline"]
    if type(generation) is not int or generation <= 0:
        raise ProcessBoundaryError("ADMISSION_GENERATION_INVALID")
    if (
        type(capability) is not str
        or capability not in {"PRIMARY", "RECOVERY"}
        or (generation == 1) != (capability == "PRIMARY")
    ):
        raise ProcessBoundaryError("ADMISSION_CAPABILITY_INVALID")
    if type(absolute) not in {int, float} or not math.isfinite(float(absolute)):
        raise ProcessBoundaryError("ADMISSION_DEADLINE_INVALID")
    deadline = AbsoluteDeadline(float(absolute))
    hard_deadline = ChildHardDeadline.install(deadline)
    _assert_descendant_creation_denied()
    _acquire_controlling_tty_if_present()
    identity = read_process_identity(os.getpid())
    if identity is None:
        raise ProcessBoundaryError("CHILD_IDENTITY_UNAVAILABLE")
    if identity.pid != identity.pgid or identity.pid != identity.sid:
        raise ProcessBoundaryError("CHILD_SESSION_ISOLATION_INVALID")
    channel.send(
        "HANDSHAKE",
        {
            "generation": generation,
            "capability": capability,
            "identity": identity.to_payload(),
            "hard_deadline_installed": True,
            "descendant_creation_denied": True,
        },
    )
    return _InitialChildBootstrap(
        generation=generation,
        capability=capability,
        deadline=deadline,
        hard_deadline=hard_deadline,
        identity=identity,
        channel=channel,
    )


def _install_network_audit_gate(
    gate: _NetworkGate,
    *,
    workload_kind: CredentialWorkloadKind,
) -> None:
    if type(gate) is not _NetworkGate or type(workload_kind) is not CredentialWorkloadKind:
        raise CredentialBoundaryError("NETWORK_AUDIT_GATE_INVALID")
    blocked_events = frozenset(
        {
            "socket.connect",
            "socket.connect_ex",
            "socket.getaddrinfo",
            "socket.gethostbyaddr",
            "socket.gethostbyname",
            "socket.sendto",
        }
    )

    def audit(event: str, arguments: tuple[object, ...]) -> None:
        if event not in blocked_events:
            return
        if not gate.ready or gate.guard_attestation is not _CREDENTIAL_GUARD_ATTESTATION:
            raise CredentialBoundaryError("CREDENTIAL_GUARD_REQUIRED_BEFORE_NETWORK")
        if workload_kind is CredentialWorkloadKind.PRODUCTION:
            return
        if not _is_numeric_loopback_audit_event(event, arguments):
            raise CredentialBoundaryError("TEST_ONLY_NETWORK_TARGET_FORBIDDEN")

    sys.addaudithook(audit)


def _is_numeric_loopback_audit_event(
    event: str,
    arguments: tuple[object, ...],
) -> bool:
    """Allow test workloads to reach loopback without permitting name lookup."""

    if event == "socket.gethostbyaddr":
        return False
    if event in {"socket.getaddrinfo", "socket.gethostbyname"}:
        host = arguments[0] if arguments else None
    elif event in {"socket.connect", "socket.connect_ex"}:
        address = arguments[1] if len(arguments) > 1 else None
        host = address[0] if isinstance(address, tuple) and address else None
    elif event == "socket.sendto":
        address = arguments[-1] if arguments else None
        host = address[0] if isinstance(address, tuple) and address else None
    else:  # pragma: no cover - caller has an exact event allowlist.
        return False
    return type(host) is str and host in {"127.0.0.1", "::1"}


def _is_devnull(fd: int) -> bool:
    try:
        return os.path.samestat(os.fstat(fd), os.stat(os.devnull))
    except OSError:
        return False


def _acquire_controlling_tty_if_present() -> None:
    if not os.isatty(0):
        return
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError as exc:
        if exc.errno not in {errno.EPERM, errno.EINVAL}:
            raise ProcessBoundaryError("CREDENTIAL_TTY_ADMISSION_FAILED") from exc


def _trusted_bootstrap_main(arguments: Sequence[str]) -> None:
    if len(arguments) < 5 or arguments[3] != "--":
        raise ProcessBoundaryError("TRUSTED_BOOTSTRAP_ARGUMENTS_INVALID")
    try:
        workload_kind = CredentialWorkloadKind(arguments[0])
    except ValueError as exc:
        raise ProcessBoundaryError("TRUSTED_BOOTSTRAP_ARGUMENTS_INVALID") from exc
    runtime_path = None if arguments[1] == "-" else str(Path(arguments[1]).resolve())
    runtime_sha256 = None if arguments[2] == "-" else arguments[2]
    raw_lease_fd = os.environ.pop(_PRODUCTION_LEASE_FD_ENV, None)
    if workload_kind is CredentialWorkloadKind.PRODUCTION:
        if raw_lease_fd is None or not raw_lease_fd.isascii() or not raw_lease_fd.isdigit():
            raise ProcessBoundaryError("PRODUCTION_EXECUTION_LEASE_REQUIRED")
        lease_fd = int(raw_lease_fd)
        try:
            lease_metadata = os.fstat(lease_fd)
        except OSError as exc:
            raise ProcessBoundaryError("PRODUCTION_EXECUTION_LEASE_REQUIRED") from exc
        if (
            not stat.S_ISREG(lease_metadata.st_mode)
            or lease_metadata.st_uid != os.getuid()
            or stat.S_IMODE(lease_metadata.st_mode) != 0o600
        ):
            raise ProcessBoundaryError("PRODUCTION_EXECUTION_LEASE_INVALID")
        os.set_inheritable(lease_fd, True)
    elif raw_lease_fd is not None:
        raise ProcessBoundaryError("TEST_WORKLOAD_PRODUCTION_LEASE_FORBIDDEN")
    target = tuple(arguments[4:])
    if not target:
        raise ProcessBoundaryError("TRUSTED_BOOTSTRAP_ARGUMENTS_INVALID")
    if workload_kind is CredentialWorkloadKind.PRODUCTION:
        if (
            _PRODUCTION_WORKLOAD_SHA256 is None
            or runtime_path != str(_PRODUCTION_WORKLOAD_PATH)
            or runtime_sha256 is None
            or _SHA256.fullmatch(runtime_sha256) is None
            or not hmac.compare_digest(runtime_sha256, _PRODUCTION_WORKLOAD_SHA256)
            or target
            != (
                sys.executable,
                "-I",
                "-S",
                str(_PRODUCTION_WORKLOAD_PATH),
            )
        ):
            raise ProcessBoundaryError("PRODUCTION_WORKLOAD_INVALID")
        try:
            actual = hashlib.sha256(_PRODUCTION_WORKLOAD_PATH.read_bytes()).hexdigest()
        except OSError as exc:
            raise ProcessBoundaryError("PRODUCTION_WORKLOAD_UNAVAILABLE") from exc
        if not hmac.compare_digest(actual, runtime_sha256):
            raise ProcessBoundaryError("PRODUCTION_WORKLOAD_DIGEST_MISMATCH")
    elif runtime_path is not None or runtime_sha256 is not None:
        raise ProcessBoundaryError("TEST_WORKLOAD_RUNTIME_BINDING_FORBIDDEN")
    bootstrap = _initial_credential_child_bootstrap()
    state = {
        "capability": bootstrap.capability,
        "deadline": bootstrap.deadline.at,
        "generation": bootstrap.generation,
        "identity": bootstrap.identity.to_payload(),
        "runtime_path": runtime_path,
        "runtime_sha256": runtime_sha256,
        "workload_kind": workload_kind.value,
    }
    control_fd = bootstrap.channel.fileno()
    os.set_inheritable(control_fd, True)
    environment = dict(os.environ)
    environment[_CONTROL_FD_ENV] = str(control_fd)
    environment[_BOOTSTRAP_STATE_ENV] = _canonical_json(state).decode("ascii")
    bootstrap.hard_deadline.assert_intact()
    try:
        os.execvpe(target[0], list(target), environment)
    except OSError as exc:
        raise ProcessBoundaryError("CREDENTIAL_WORKLOAD_EXEC_FAILED") from exc


def _generation_capability(generation: int) -> GenerationCapability:
    from global_quant.gate1b.execution_journal import GenerationCapability

    if type(generation) is not int or generation <= 0:
        raise ProcessBoundaryError("ADMISSION_GENERATION_INVALID")
    return GenerationCapability.PRIMARY if generation == 1 else GenerationCapability.RECOVERY


class _ProductionExecutionLease:
    """Host-wide advisory lease inherited by the sole production child.

    The fixed owner-only file is never deleted and its contents are not the
    authority.  The kernel flock is inherited across both exec boundaries, so
    a supervisor crash cannot free the lease while its hard-timed child still
    exists.
    """

    __slots__ = ("_bound_identity", "_descriptor", "_released")

    def __init__(self, descriptor: int) -> None:
        self._descriptor = descriptor
        self._bound_identity: ProcessIdentity | None = None
        self._released = False

    @classmethod
    def acquire(cls) -> _ProductionExecutionLease:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(_PRODUCTION_EXECUTION_LEASE_PATH, flags, 0o600)
        except OSError as exc:
            raise ProcessBoundaryError("PRODUCTION_EXECUTION_LEASE_UNAVAILABLE") from exc
        try:
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ProcessBoundaryError("PRODUCTION_EXECUTION_LEASE_INVALID")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise GenerationAdmissionError("PRODUCTION_EXECUTION_LEASE_HELD") from exc
            os.set_inheritable(descriptor, True)
            return cls(descriptor)
        except BaseException:
            os.close(descriptor)
            raise

    def fileno(self) -> int:
        if self._released:
            raise ProcessBoundaryError("PRODUCTION_EXECUTION_LEASE_RELEASED")
        return self._descriptor

    def bind_identity(self, *, generation: int, identity: ProcessIdentity) -> None:
        if (
            self._released
            or self._bound_identity is not None
            or type(generation) is not int
            or generation <= 0
            or type(identity) is not ProcessIdentity
            or read_process_identity(identity.pid) != identity
        ):
            raise ProcessBoundaryError("PRODUCTION_EXECUTION_LEASE_BINDING_INVALID")
        encoded = _canonical_json(
            {
                "generation": generation,
                "identity": identity.to_payload(),
                "identity_sha256": identity.sha256,
                "schema_version": _PRODUCTION_EXECUTION_LEASE_SCHEMA,
                "status": "ACTIVE",
            }
        )
        try:
            os.lseek(self._descriptor, 0, os.SEEK_SET)
            os.ftruncate(self._descriptor, 0)
            _write_all(self._descriptor, encoded)
            os.fsync(self._descriptor)
        except OSError as exc:
            raise ProcessBoundaryError("PRODUCTION_EXECUTION_LEASE_BINDING_FAILED") from exc
        self._bound_identity = identity

    def release_after_exact_reap(self) -> None:
        if self._released:
            return
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self._descriptor)
            self._released = True

    def _close_supervisor_copy(self) -> None:
        """Model supervisor death; an inherited child descriptor retains flock."""

        if self._released:
            return
        os.close(self._descriptor)
        self._released = True


@dataclass
class ManagedChild:
    generation: int
    capability: GenerationCapability
    process: subprocess.Popen[bytes]
    identity: ProcessIdentity
    channel: IPCChannel
    launch_argv: tuple[str, ...]
    workload: CredentialWorkload
    admission_receipt: DurableIdentityReceipt | None
    production_lease: _ProductionExecutionLease | None = field(default=None, repr=False)
    _reaped: bool = False
    _next_phase_sequence: int = 0
    _phase_watchdog: threading.Timer | None = field(default=None, repr=False)
    _active_phase_digest: str | None = None


@dataclass(frozen=True, slots=True)
class ReapAttestation:
    generation: int
    stage_ordinal: int
    identity: ProcessIdentity
    process_identity_sha256: str
    waited_pid: int
    returncode: int
    signal: int | None
    process_journal_path: Path
    attested_monotonic_ns: int
    exact_pid_waited: bool = True
    descendant_creation_denied: bool = True
    local_process_quiesced: bool = True
    venue_mutation_absent_proven: bool = False
    journal_sequence: int = 0
    journal_digest: str = ""
    journal_head_sequence: int = 0
    journal_head_digest: str = ""
    execution_journal_sequence: int | None = None
    execution_journal_digest: str | None = None
    execution_head_sequence: int | None = None
    execution_head_digest: str | None = None
    proves: tuple[str, ...] = (
        "exact_pid_wait",
        "descendant_creation_denied",
        "local_process_quiescence",
    )

    def __post_init__(self) -> None:
        if (
            type(self.generation) is not int
            or self.generation <= 0
            or type(self.stage_ordinal) is not int
            or self.stage_ordinal <= 0
            or type(self.identity) is not ProcessIdentity
            or self.process_identity_sha256 != self.identity.sha256
            or self.waited_pid != self.identity.pid
            or type(self.returncode) is not int
            or (self.signal is not None and (type(self.signal) is not int or self.signal <= 0))
            or not isinstance(self.process_journal_path, Path)
            or type(self.attested_monotonic_ns) is not int
            or self.attested_monotonic_ns <= 0
            or self.exact_pid_waited is not True
            or self.descendant_creation_denied is not True
            or self.local_process_quiesced is not True
            or self.venue_mutation_absent_proven is not False
            or type(self.journal_sequence) is not int
            or self.journal_sequence <= 0
            or _SHA256.fullmatch(self.journal_digest) is None
            or self.journal_head_sequence != self.journal_sequence
            or self.journal_head_digest != self.journal_digest
            or type(self.execution_journal_sequence) is not int
            or self.execution_journal_sequence <= 0
            or type(self.execution_head_sequence) is not int
            or self.execution_head_sequence < self.execution_journal_sequence
            or type(self.execution_journal_digest) is not str
            or _SHA256.fullmatch(self.execution_journal_digest) is None
            or type(self.execution_head_digest) is not str
            or _SHA256.fullmatch(self.execution_head_digest) is None
        ):
            raise ProcessBoundaryError("PROCESS_REAP_ATTESTATION_INVALID")


class CredentialProcessSupervisor:
    """Credential-free, single-generation launch/reap wrapper.

    This is deliberately not an execution event loop.  Mutation dispatch and
    reconciliation remain higher-level kernel responsibilities.
    """

    def __init__(
        self,
        *,
        lifecycle_journal: ProcessLifecycleJournal,
        execution_journal: Any,
        parent_environment: Mapping[str, str],
        on_identity_staged: (
            Callable[[int, ProcessIdentity, DurableIdentityReceipt], None] | None
        ) = None,
        credential_stdin: int | BinaryIO | None = None,
        allow_test_workloads: bool = False,
    ) -> None:
        from global_quant.gate1b.execution_journal import ExecutionJournal

        contaminated = _CREDENTIAL_ENVIRONMENT_NAMES.intersection(parent_environment)
        if contaminated:
            raise CredentialBoundaryError("SUPERVISOR_CREDENTIAL_ENVIRONMENT_PRESENT")
        if type(lifecycle_journal) is not ProcessLifecycleJournal:
            raise TypeError("PROCESS_LIFECYCLE_JOURNAL_REQUIRED")
        if type(execution_journal) is not ExecutionJournal:
            raise TypeError("EXECUTION_JOURNAL_REQUIRED")
        if execution_journal.path.resolve() != lifecycle_journal.execution_journal_path.resolve():
            raise ProcessBoundaryError("EXECUTION_JOURNAL_PATH_MISMATCH")
        self._lifecycle_journal = lifecycle_journal
        self._execution_journal = execution_journal
        self.deadline = AbsoluteDeadline(lifecycle_journal.lifecycle_deadline)
        self._child_environment = {
            name: value
            for name, value in parent_environment.items()
            if name in _ALLOWED_CHILD_ENVIRONMENT_NAMES
        }
        if on_identity_staged is not None and not callable(on_identity_staged):
            raise TypeError("STAGE_IDENTITY_CALLBACK_INVALID")
        self._on_identity_staged = on_identity_staged
        self._credential_stdin = credential_stdin
        self._allow_test_workloads = allow_test_workloads is True
        self._active: ManagedChild | None = None
        self._previous_identity = lifecycle_journal.active_identity
        self._previous_generation = lifecycle_journal.active_generation

    def launch(
        self,
        workload: CredentialWorkload,
        *,
        generation: int,
    ) -> ManagedChild:
        if type(generation) is not int or generation <= 0:
            raise ValueError("GENERATION_MUST_BE_POSITIVE")
        if type(workload) is not CredentialWorkload:
            raise ProcessBoundaryError("CREDENTIAL_WORKLOAD_TYPE_REQUIRED")
        if workload.kind is CredentialWorkloadKind.TEST_ONLY and not self._allow_test_workloads:
            raise ProcessBoundaryError("TEST_WORKLOAD_FORBIDDEN_IN_PRODUCTION")
        current_state = self._lifecycle_journal._read_state()
        self._previous_identity = current_state.active_identity
        self._previous_generation = current_state.active_generation
        if self._active is not None or self._previous_identity is not None:
            raise GenerationAdmissionError("OLD_GENERATION_STILL_PRESENT")
        if generation != current_state.last_generation + 1:
            raise GenerationAdmissionError("GENERATION_SEQUENCE_INVALID")
        handshake_timeout = self.deadline.clamp(_HANDSHAKE_LOCAL_LIMIT_SECONDS)
        launch_argv = build_seatbelt_argv(workload)
        production_lease = (
            _ProductionExecutionLease.acquire()
            if workload.kind is CredentialWorkloadKind.PRODUCTION
            else None
        )
        try:
            parent_transport, child_transport = socket.socketpair(
                socket.AF_UNIX,
                socket.SOCK_STREAM,
            )
        except BaseException:
            if production_lease is not None:
                production_lease.release_after_exact_reap()
            raise
        environment = dict(self._child_environment)
        environment[_CONTROL_FD_ENV] = str(child_transport.fileno())
        if production_lease is not None:
            environment[_PRODUCTION_LEASE_FD_ENV] = str(production_lease.fileno())
        process: subprocess.Popen[bytes] | None = None
        staged_child: ManagedChild | None = None
        capability = _generation_capability(generation)
        try:
            pass_fds = (child_transport.fileno(),)
            if production_lease is not None:
                pass_fds = (*pass_fds, production_lease.fileno())
            process = subprocess.Popen(
                launch_argv,
                env=environment,
                stdin=self._credential_stdin,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                pass_fds=pass_fds,
                start_new_session=True,
            )
            child_transport.close()
            parent_transport.settimeout(handshake_timeout)
            channel = IPCChannel(parent_transport)
            staged_identity = self._observe_pre_admission_identity(process)
            if production_lease is not None:
                production_lease.bind_identity(
                    generation=generation,
                    identity=staged_identity,
                )
            receipt = self._lifecycle_journal.stage_identity(generation, staged_identity)
            staged_child = ManagedChild(
                generation=generation,
                capability=capability,
                process=process,
                identity=staged_identity,
                channel=channel,
                launch_argv=tuple(launch_argv),
                workload=workload,
                admission_receipt=receipt,
                production_lease=production_lease,
            )
            self._active = staged_child
            self._previous_identity = staged_identity
            self._previous_generation = generation
            self._lifecycle_journal.verify_receipt(receipt)
            if self._on_identity_staged is not None:
                self._on_identity_staged(generation, staged_identity, receipt)
            admission_record = self._admit_execution_generation(
                generation=generation,
                identity=staged_identity,
                capability=capability,
            )
            self._lifecycle_journal.record_execution_admission(
                generation=generation,
                identity=staged_identity,
                execution_journal=self._execution_journal,
                admission_record=admission_record,
            )
            channel.send(
                "ADMISSION",
                {
                    "generation": generation,
                    "capability": capability.value,
                    "deadline": self.deadline.at,
                },
            )
            handshake = channel.receive()
            identity = self._validate_handshake(
                process,
                generation,
                handshake,
                staged_identity=staged_identity,
                capability=capability,
            )
            parent_transport.settimeout(self.deadline.clamp(5.0))
            staged_child.identity = identity
            return staged_child
        except BaseException as original:
            child_transport.close()
            parent_transport.close()
            if staged_child is not None:
                try:
                    self._reap_failed_staged_child(staged_child)
                except BaseException as cleanup_error:
                    raise cleanup_error from original
            elif process is not None:
                _kill_and_wait_unadmitted(process)
                if production_lease is not None:
                    production_lease.release_after_exact_reap()
            elif production_lease is not None:
                production_lease.release_after_exact_reap()
            raise

    def attest_previous_disappearance(self) -> None:
        if self._active is not None:
            raise GenerationAdmissionError("OLD_GENERATION_STILL_PRESENT")
        state = self._lifecycle_journal._read_state()
        if state.active_identity is None:
            self._previous_identity = None
            self._previous_generation = None
            return
        if is_same_process_alive(state.active_identity):
            raise GenerationAdmissionError("OLD_GENERATION_STILL_PRESENT")
        generation = state.active_generation
        if generation is None:
            raise ProcessBoundaryError("PROCESS_JOURNAL_ACTIVE_STATE_INVALID")
        admission_record = self._matching_execution_admission(
            generation=generation,
            identity=state.active_identity,
        )
        if state.active_admission_committed and admission_record is None:
            raise ProcessBoundaryError("EXECUTION_ADMISSION_PROOF_MISSING")
        if not state.active_admission_committed and admission_record is not None:
            self._lifecycle_journal.record_execution_admission(
                generation=generation,
                identity=state.active_identity,
                execution_journal=self._execution_journal,
                admission_record=admission_record,
            )
            state = self._lifecycle_journal._read_state()
        execution_reap_record = None
        if state.active_admission_committed:
            execution_reap_record = self._matching_execution_reap(
                generation=generation,
                identity=state.active_identity,
            )
            if execution_reap_record is None:
                if time.monotonic() < self.deadline.at:
                    raise ProcessBoundaryError("ORPHAN_EXIT_STATUS_NOT_PROVABLE")
                execution_reap_record = self._append_execution_reap(
                    generation=generation,
                    identity=state.active_identity,
                    admission_record=admission_record,
                    returncode=-int(signal.SIGALRM),
                    signal_number=int(signal.SIGALRM),
                )
        self._lifecycle_journal.record_orphan_disappearance(
            generation=generation,
            identity=state.active_identity,
            execution_journal=self._execution_journal,
            execution_reap_record=execution_reap_record,
        )
        self._previous_identity = None
        self._previous_generation = None

    def kill_and_reap(
        self,
        child: ManagedChild,
        *,
        local_limit: float = 5.0,
    ) -> ReapAttestation:
        self._require_active(child)
        sent_signal: int | None = None
        if child.process.poll() is None:
            os.kill(child.identity.pid, signal.SIGKILL)
            sent_signal = int(signal.SIGKILL)
        return self._wait_and_attest(child, local_limit=local_limit, sent_signal=sent_signal)

    def reap(
        self,
        child: ManagedChild,
        *,
        local_limit: float = 5.0,
    ) -> ReapAttestation:
        self._require_active(child)
        return self._wait_and_attest(child, local_limit=local_limit, sent_signal=None)

    def issue_phase_permit(
        self,
        child: ManagedChild,
        *,
        local_limit: float,
    ) -> PhaseDeadlinePermit:
        self._require_active(child)
        absolute = self.deadline.authorize_phase(local_limit)
        permit = PhaseDeadlinePermit.issue(
            generation=child.generation,
            sequence=child._next_phase_sequence,
            absolute_deadline=absolute,
            lifecycle_deadline=self.deadline.at,
        )
        delay = max(0.0, absolute - time.monotonic() - _SUPERVISOR_KILL_LEAD_SECONDS)
        watchdog = threading.Timer(delay, self._expire_phase, args=(child, permit.digest))
        watchdog.daemon = True
        if child._phase_watchdog is not None:
            child._phase_watchdog.cancel()
        child._phase_watchdog = watchdog
        child._active_phase_digest = permit.digest
        child._next_phase_sequence += 1
        watchdog.start()
        child.channel.send("PHASE_PERMIT", permit.to_payload())
        return permit

    def _expire_phase(self, child: ManagedChild, permit_digest: str) -> None:
        if (
            self._active is child
            and not child._reaped
            and child._active_phase_digest == permit_digest
        ):
            _signal_exact_child(child)

    def _wait_and_attest(
        self,
        child: ManagedChild,
        *,
        local_limit: float,
        sent_signal: int | None,
    ) -> ReapAttestation:
        try:
            returncode = self._wait_once(child.process, local_limit)
        except (DeadlineExpired, subprocess.TimeoutExpired):
            if sent_signal is None:
                sent_signal = _signal_exact_child(child)
            try:
                returncode = self._wait_once(child.process, 5.0)
            except (DeadlineExpired, subprocess.TimeoutExpired) as exc:
                child.channel.close()
                # The active generation is deliberately retained.  No new
                # credential-bearing process and no quiescence attestation are
                # possible until an exact reap is later proven.
                raise ProcessBoundaryError("EXACT_REAP_NOT_PROVEN") from exc
        child.channel.close()
        if is_same_process_alive(child.identity):
            raise ProcessBoundaryError("REAP_IDENTITY_STILL_PRESENT")
        if child.production_lease is not None:
            child.production_lease.release_after_exact_reap()
            child.production_lease = None
        exit_signal = -returncode if returncode < 0 else sent_signal
        execution_reap_record = self._ensure_execution_reap(
            generation=child.generation,
            identity=child.identity,
            returncode=returncode,
            signal_number=exit_signal,
        )
        record = self._lifecycle_journal.record_reap(
            generation=child.generation,
            identity=child.identity,
            returncode=returncode,
            signal_number=exit_signal,
            execution_journal=self._execution_journal,
            execution_reap_record=execution_reap_record,
        )
        head_sequence, head_digest = _read_process_head(self._lifecycle_journal.head_path)
        if child._phase_watchdog is not None:
            child._phase_watchdog.cancel()
        child._reaped = True
        self._active = None
        self._previous_identity = None
        self._previous_generation = None
        event = record.event
        return ReapAttestation(
            generation=child.generation,
            stage_ordinal=event["stage_ordinal"],
            identity=child.identity,
            process_identity_sha256=child.identity.sha256,
            waited_pid=child.process.pid,
            returncode=returncode,
            signal=exit_signal,
            process_journal_path=self._lifecycle_journal.path,
            attested_monotonic_ns=event["attested_monotonic_ns"],
            journal_sequence=record.sequence,
            journal_digest=record.digest,
            journal_head_sequence=head_sequence,
            journal_head_digest=head_digest,
            execution_journal_sequence=event["execution_journal_sequence"],
            execution_journal_digest=event["execution_journal_digest"],
            execution_head_sequence=event["execution_head_sequence"],
            execution_head_digest=event["execution_head_digest"],
        )

    def _wait_once(self, process: subprocess.Popen[bytes], local_limit: float) -> int:
        try:
            timeout = self.deadline.clamp(local_limit)
        except DeadlineExpired:
            timeout = 0.0
        return process.wait(timeout=timeout)

    def _require_active(self, child: ManagedChild) -> None:
        if self._active is not child or child._reaped:
            raise GenerationAdmissionError("CHILD_NOT_ACTIVE")

    def _reap_failed_staged_child(self, child: ManagedChild) -> None:
        sent_signal = _signal_exact_child(child)
        try:
            returncode = self._wait_once(child.process, _HANDSHAKE_LOCAL_LIMIT_SECONDS)
        except (DeadlineExpired, subprocess.TimeoutExpired) as exc:
            child.channel.close()
            raise ProcessBoundaryError("STAGED_CHILD_EXACT_REAP_NOT_PROVEN") from exc
        if is_same_process_alive(child.identity):
            raise ProcessBoundaryError("STAGED_CHILD_EXACT_REAP_NOT_PROVEN")
        if child.production_lease is not None:
            child.production_lease.release_after_exact_reap()
            child.production_lease = None
        exit_signal = -returncode if returncode < 0 else sent_signal
        admission_record = self._matching_execution_admission(
            generation=child.generation,
            identity=child.identity,
        )
        if admission_record is not None and not self._lifecycle_journal.active_admission_committed:
            self._lifecycle_journal.record_execution_admission(
                generation=child.generation,
                identity=child.identity,
                execution_journal=self._execution_journal,
                admission_record=admission_record,
            )
        execution_reap_record = None
        if self._lifecycle_journal.active_admission_committed:
            execution_reap_record = self._ensure_execution_reap(
                generation=child.generation,
                identity=child.identity,
                returncode=returncode,
                signal_number=exit_signal,
            )
        self._lifecycle_journal.record_reap(
            generation=child.generation,
            identity=child.identity,
            returncode=returncode,
            signal_number=exit_signal,
            execution_journal=self._execution_journal,
            execution_reap_record=execution_reap_record,
        )
        child._reaped = True
        self._active = None
        self._previous_identity = None
        self._previous_generation = None

    def _admit_execution_generation(
        self,
        *,
        generation: int,
        identity: ProcessIdentity,
        capability: GenerationCapability,
    ) -> Any:
        from global_quant.gate1b.execution_journal import DurableGenerationAdmission

        return self._execution_journal.admit_generation(
            DurableGenerationAdmission(generation, identity.sha256),
            capability,
        )

    def _matching_execution_admission(
        self,
        *,
        generation: int,
        identity: ProcessIdentity,
    ) -> Any | None:
        matching = None
        for record in self._execution_journal.records():
            event = record.event
            if type(event).__name__ != "_GenerationAdmitted":
                continue
            if event.generation != generation:
                continue
            if event.process_identity_sha256 != identity.sha256:
                raise ProcessBoundaryError("EXECUTION_ADMISSION_IDENTITY_MISMATCH")
            matching = record
        return matching

    def _matching_execution_reap(
        self,
        *,
        generation: int,
        identity: ProcessIdentity,
    ) -> Any | None:
        matching = None
        for record in self._execution_journal.records():
            event = record.event
            if type(event).__name__ != "_GenerationReaped":
                continue
            receipt = event.receipt
            if receipt.generation != generation:
                continue
            if receipt.process_identity_sha256 != identity.sha256:
                raise ProcessBoundaryError("EXECUTION_REAP_IDENTITY_MISMATCH")
            matching = record
        return matching

    def _append_execution_reap(
        self,
        *,
        generation: int,
        identity: ProcessIdentity,
        admission_record: Any,
        returncode: int,
        signal_number: int | None,
    ) -> Any:
        from global_quant.gate1b.execution_journal import ProcessReapReceipt

        admission_proof = _validate_execution_admission(
            expected_path=self._lifecycle_journal.execution_journal_path,
            generation=generation,
            identity=identity,
            execution_journal=self._execution_journal,
            admission_record=admission_record,
        )
        return self._execution_journal.reap_generation(
            ProcessReapReceipt(
                generation=generation,
                process_identity_sha256=identity.sha256,
                admission_record_sequence=admission_proof["execution_journal_sequence"],
                admission_record_digest=admission_proof["execution_journal_digest"],
                returncode=returncode,
                signal=signal_number,
                local_process_quiesced=True,
                venue_mutation_absent_proven=False,
            )
        )

    def _ensure_execution_reap(
        self,
        *,
        generation: int,
        identity: ProcessIdentity,
        returncode: int,
        signal_number: int | None,
    ) -> Any:
        admission = self._matching_execution_admission(
            generation=generation,
            identity=identity,
        )
        if admission is None:
            raise ProcessBoundaryError("EXECUTION_ADMISSION_PROOF_MISSING")
        existing = self._matching_execution_reap(
            generation=generation,
            identity=identity,
        )
        if existing is not None:
            receipt = existing.event.receipt
            if receipt.returncode != returncode or receipt.signal != signal_number:
                raise ProcessBoundaryError("EXECUTION_REAP_RESULT_MISMATCH")
            return existing
        return self._append_execution_reap(
            generation=generation,
            identity=identity,
            admission_record=admission,
            returncode=returncode,
            signal_number=signal_number,
        )

    @staticmethod
    def _validate_handshake(
        process: subprocess.Popen[bytes],
        generation: int,
        message: IPCMessage,
        *,
        staged_identity: ProcessIdentity,
        capability: GenerationCapability,
    ) -> ProcessIdentity:
        if message.kind != "HANDSHAKE" or set(message.payload) != {
            "generation",
            "capability",
            "identity",
            "hard_deadline_installed",
            "descendant_creation_denied",
        }:
            raise ProcessBoundaryError("CHILD_HANDSHAKE_INVALID")
        if (
            message.payload["generation"] != generation
            or message.payload["capability"] != capability.value
            or message.payload["hard_deadline_installed"] is not True
            or message.payload["descendant_creation_denied"] is not True
            or not isinstance(message.payload["identity"], dict)
        ):
            raise ProcessBoundaryError("CHILD_HANDSHAKE_INVALID")
        claimed = ProcessIdentity.from_payload(message.payload["identity"])
        observed = read_process_identity(process.pid)
        if (
            observed is None
            or claimed != observed
            or claimed != staged_identity
            or claimed.pid != process.pid
            or claimed.ppid != os.getpid()
            or claimed.pgid != claimed.pid
            or claimed.sid != claimed.pid
        ):
            raise ProcessBoundaryError("CHILD_HANDSHAKE_IDENTITY_MISMATCH")
        return claimed

    @staticmethod
    def _observe_pre_admission_identity(process: subprocess.Popen[bytes]) -> ProcessIdentity:
        observed = read_process_identity(process.pid)
        if (
            observed is None
            or observed.pid != process.pid
            or observed.ppid != os.getpid()
            or observed.pgid != observed.pid
            or observed.sid != observed.pid
        ):
            raise ProcessBoundaryError("PRE_ADMISSION_IDENTITY_INVALID")
        return observed


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise IPCProtocolError("IPC_JSON_VALUE_INVALID") from exc


def _validated_execution_records(
    *,
    expected_path: Path,
    execution_journal: Any,
) -> tuple[tuple[Any, ...], Any]:
    from global_quant.gate1b.execution_journal import ExecutionJournal

    if (
        type(execution_journal) is not ExecutionJournal
        or execution_journal.path.resolve() != expected_path.resolve()
    ):
        raise ProcessBoundaryError("EXECUTION_JOURNAL_PATH_MISMATCH")
    try:
        records = execution_journal.records()
    except BaseException as exc:
        raise ProcessBoundaryError("EXECUTION_JOURNAL_PROOF_INVALID") from exc
    if not records:
        raise ProcessBoundaryError("EXECUTION_JOURNAL_PROOF_INVALID")
    return records, records[-1]


def _validate_execution_admission(
    *,
    expected_path: Path,
    generation: int,
    identity: ProcessIdentity,
    execution_journal: Any,
    admission_record: Any,
) -> dict[str, int | str]:
    records, head = _validated_execution_records(
        expected_path=expected_path,
        execution_journal=execution_journal,
    )
    sequence = getattr(admission_record, "sequence", None)
    if (
        type(sequence) is not int
        or not 0 < sequence <= len(records)
        or records[sequence - 1] != admission_record
        or type(admission_record.event).__name__ != "_GenerationAdmitted"
        or admission_record.event.generation != generation
        or admission_record.event.capability.value != ("PRIMARY" if generation == 1 else "RECOVERY")
        or admission_record.event.process_identity_sha256 != identity.sha256
        or type(admission_record.digest) is not str
        or _SHA256.fullmatch(admission_record.digest) is None
    ):
        raise ProcessBoundaryError("EXECUTION_ADMISSION_PROOF_INVALID")
    return {
        "execution_journal_sequence": admission_record.sequence,
        "execution_journal_digest": admission_record.digest,
        "execution_head_sequence": head.sequence,
        "execution_head_digest": head.digest,
    }


def _validate_execution_reap(
    *,
    expected_path: Path,
    generation: int,
    identity: ProcessIdentity,
    execution_journal: Any,
    reap_record: Any,
) -> dict[str, int | str]:
    records, head = _validated_execution_records(
        expected_path=expected_path,
        execution_journal=execution_journal,
    )
    sequence = getattr(reap_record, "sequence", None)
    event = getattr(reap_record, "event", None)
    receipt = getattr(event, "receipt", None)
    if (
        type(sequence) is not int
        or not 0 < sequence <= len(records)
        or records[sequence - 1] != reap_record
        or type(event).__name__ != "_GenerationReaped"
        or not _execution_admission_matches_receipt(
            records=records,
            generation=generation,
            identity=identity,
            receipt=receipt,
        )
        or receipt.generation != generation
        or receipt.process_identity_sha256 != identity.sha256
        or receipt.local_process_quiesced is not True
        or receipt.venue_mutation_absent_proven is not False
        or type(reap_record.digest) is not str
        or _SHA256.fullmatch(reap_record.digest) is None
    ):
        raise ProcessBoundaryError("EXECUTION_REAP_PROOF_INVALID")
    return {
        "execution_journal_sequence": reap_record.sequence,
        "execution_journal_digest": reap_record.digest,
        "execution_head_sequence": head.sequence,
        "execution_head_digest": head.digest,
    }


def _execution_admission_matches_receipt(
    *,
    records: list[Any] | tuple[Any, ...],
    generation: int,
    identity: ProcessIdentity,
    receipt: Any,
) -> bool:
    sequence = getattr(receipt, "admission_record_sequence", None)
    digest = getattr(receipt, "admission_record_digest", None)
    if type(sequence) is not int or not 0 < sequence <= len(records):
        return False
    admission_record = records[sequence - 1]
    admission_event = getattr(admission_record, "event", None)
    return (
        type(digest) is str
        and _SHA256.fullmatch(digest) is not None
        and admission_record.digest == digest
        and type(admission_event).__name__ == "_GenerationAdmitted"
        and admission_event.generation == generation
        and admission_event.process_identity_sha256 == identity.sha256
    )


def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        try:
            written = os.write(fd, remaining)
        except OSError as exc:
            raise ProcessBoundaryError("PROCESS_JOURNAL_WRITE_FAILED") from exc
        if written <= 0:
            raise ProcessBoundaryError("PROCESS_JOURNAL_WRITE_FAILED")
        remaining = remaining[written:]


def _validate_owner_file(file_stat: os.stat_result, prefix: str) -> None:
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_nlink != 1
        or file_stat.st_uid != os.getuid()
        or stat.S_IMODE(file_stat.st_mode) != 0o600
    ):
        raise ProcessBoundaryError(f"{prefix}_UNSAFE")


def _build_process_record(
    *,
    sequence: int,
    previous_digest: str,
    event: dict[str, Any],
) -> tuple[_ProcessJournalRecord, bytes]:
    core = {
        "schema_version": _PROCESS_JOURNAL_SCHEMA,
        "sequence": sequence,
        "previous_digest": previous_digest,
        "event": event,
    }
    digest = hashlib.sha256(_canonical_json(core)).hexdigest()
    record = {**core, "digest": digest}
    encoded = _canonical_json(record) + b"\n"
    if len(encoded) > _PROCESS_JOURNAL_MAX_RECORD_BYTES:
        raise ProcessBoundaryError("PROCESS_JOURNAL_RECORD_OVERSIZED")
    return (
        _ProcessJournalRecord(
            sequence=sequence,
            previous_digest=previous_digest,
            event=event,
            digest=digest,
        ),
        encoded,
    )


def _read_process_records(fd: int) -> tuple[_ProcessJournalRecord, ...]:
    os.lseek(fd, 0, os.SEEK_SET)
    result: list[_ProcessJournalRecord] = []
    previous = _ZERO_DIGEST
    with os.fdopen(os.dup(fd), "rb") as stream:
        while True:
            raw = stream.readline(_PROCESS_JOURNAL_MAX_RECORD_BYTES + 1)
            if not raw:
                break
            if len(raw) > _PROCESS_JOURNAL_MAX_RECORD_BYTES:
                raise ProcessBoundaryError("PROCESS_JOURNAL_RECORD_OVERSIZED")
            if not raw.endswith(b"\n"):
                raise ProcessBoundaryError("PROCESS_JOURNAL_TRUNCATED")
            try:
                value = json.loads(raw[:-1].decode("ascii"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProcessBoundaryError("PROCESS_JOURNAL_MALFORMED") from exc
            if (
                not isinstance(value, dict)
                or set(value)
                != {"schema_version", "sequence", "previous_digest", "event", "digest"}
                or value["schema_version"] != _PROCESS_JOURNAL_SCHEMA
                or type(value["sequence"]) is not int
                or value["sequence"] != len(result) + 1
                or value["previous_digest"] != previous
                or not isinstance(value["event"], dict)
                or type(value["digest"]) is not str
                or _SHA256.fullmatch(value["digest"]) is None
                or raw != _canonical_json(value) + b"\n"
            ):
                raise ProcessBoundaryError("PROCESS_JOURNAL_RECORD_INVALID")
            core = {key: item for key, item in value.items() if key != "digest"}
            digest = hashlib.sha256(_canonical_json(core)).hexdigest()
            if not hmac.compare_digest(digest, value["digest"]):
                raise ProcessBoundaryError("PROCESS_JOURNAL_DIGEST_MISMATCH")
            record = _ProcessJournalRecord(
                sequence=value["sequence"],
                previous_digest=value["previous_digest"],
                event=value["event"],
                digest=value["digest"],
            )
            result.append(record)
            previous = record.digest
    if not result:
        raise ProcessBoundaryError("PROCESS_JOURNAL_TRUNCATED")
    _replay_process_records(tuple(result))
    return tuple(result)


def _replay_process_records(
    records: tuple[_ProcessJournalRecord, ...],
) -> _ProcessJournalState:
    genesis = records[0].event
    if (
        genesis.get("type") != "LIFECYCLE_STARTED"
        or set(genesis)
        != {
            "type",
            "lifecycle_started_at",
            "lifecycle_deadline",
            "boot_token",
            "execution_journal_path",
        }
        or type(genesis["lifecycle_started_at"]) not in {int, float}
        or not math.isfinite(float(genesis["lifecycle_started_at"]))
        or type(genesis["lifecycle_deadline"]) not in {int, float}
        or not math.isfinite(float(genesis["lifecycle_deadline"]))
        or genesis["lifecycle_started_at"] >= genesis["lifecycle_deadline"]
        or genesis["boot_token"] != _boot_token()
        or type(genesis["execution_journal_path"]) is not str
        or not Path(genesis["execution_journal_path"]).is_absolute()
    ):
        raise ProcessBoundaryError("PROCESS_JOURNAL_GENESIS_INVALID")
    state = _ProcessJournalState(
        lifecycle_started_at=float(genesis["lifecycle_started_at"]),
        lifecycle_deadline=float(genesis["lifecycle_deadline"]),
        execution_journal_path=Path(genesis["execution_journal_path"]),
        active_identity=None,
        active_generation=None,
        active_stage_ordinal=None,
        active_admission_committed=False,
        last_generation=0,
        last_stage_ordinal=0,
    )
    for record in records[1:]:
        state = _apply_process_event(state, record.event)
    return state


def _validate_process_event_append(state: _ProcessJournalState, event: dict[str, Any]) -> None:
    _apply_process_event(state, event)


def _apply_process_event(
    state: _ProcessJournalState,
    event: dict[str, Any],
) -> _ProcessJournalState:
    event_type = event.get("type")
    if event_type == "IDENTITY_STAGED":
        if set(event) != {
            "type",
            "generation",
            "stage_ordinal",
            "identity",
            "process_identity_sha256",
        }:
            raise ProcessBoundaryError("PROCESS_JOURNAL_EVENT_INVALID")
        identity = ProcessIdentity.from_payload(event["identity"])
        generation = event["generation"]
        stage_ordinal = event["stage_ordinal"]
        if (
            state.active_identity is not None
            or type(generation) is not int
            or generation != state.last_generation + 1
            or type(stage_ordinal) is not int
            or stage_ordinal != state.last_stage_ordinal + 1
            or event["process_identity_sha256"] != identity.sha256
        ):
            raise ProcessBoundaryError("PROCESS_JOURNAL_STAGE_INVALID")
        return _ProcessJournalState(
            lifecycle_started_at=state.lifecycle_started_at,
            lifecycle_deadline=state.lifecycle_deadline,
            execution_journal_path=state.execution_journal_path,
            active_identity=identity,
            active_generation=generation,
            active_stage_ordinal=stage_ordinal,
            active_admission_committed=False,
            last_generation=state.last_generation,
            last_stage_ordinal=stage_ordinal,
        )
    if event_type == "EXECUTION_ADMISSION_COMMITTED":
        if (
            set(event)
            != {
                "type",
                "generation",
                "stage_ordinal",
                "process_identity_sha256",
                "execution_journal_sequence",
                "execution_journal_digest",
                "execution_head_sequence",
                "execution_head_digest",
            }
            or state.active_identity is None
            or state.active_admission_committed
            or event["generation"] != state.active_generation
            or event["stage_ordinal"] != state.active_stage_ordinal
            or event["process_identity_sha256"] != state.active_identity.sha256
            or type(event["execution_journal_sequence"]) is not int
            or event["execution_journal_sequence"] <= 0
            or type(event["execution_head_sequence"]) is not int
            or event["execution_head_sequence"] < event["execution_journal_sequence"]
            or type(event["execution_journal_digest"]) is not str
            or _SHA256.fullmatch(event["execution_journal_digest"]) is None
            or type(event["execution_head_digest"]) is not str
            or _SHA256.fullmatch(event["execution_head_digest"]) is None
        ):
            raise ProcessBoundaryError("PROCESS_JOURNAL_ADMISSION_COMMIT_INVALID")
        return _ProcessJournalState(
            lifecycle_started_at=state.lifecycle_started_at,
            lifecycle_deadline=state.lifecycle_deadline,
            execution_journal_path=state.execution_journal_path,
            active_identity=state.active_identity,
            active_generation=state.active_generation,
            active_stage_ordinal=state.active_stage_ordinal,
            active_admission_committed=True,
            last_generation=state.active_generation,
            last_stage_ordinal=state.last_stage_ordinal,
        )
    if event_type in {"IDENTITY_REAPED", "IDENTITY_ORPHAN_DISAPPEARED"}:
        execution_fields = {
            "execution_journal_sequence",
            "execution_journal_digest",
            "execution_head_sequence",
            "execution_head_digest",
        }
        expected_fields = (
            {
                "type",
                "generation",
                "stage_ordinal",
                "process_identity_sha256",
                "waited_pid",
                "returncode",
                "signal",
                "exact_pid_waited",
                "descendant_creation_denied",
                "local_process_quiesced",
                "venue_mutation_absent_proven",
                "attested_monotonic_ns",
                *execution_fields,
            }
            if event_type == "IDENTITY_REAPED"
            else {
                "type",
                "generation",
                "stage_ordinal",
                "process_identity_sha256",
                "pid1_reap_observed",
                "descendant_creation_denied",
                "local_process_quiesced",
                "venue_mutation_absent_proven",
                "attested_monotonic_ns",
                *execution_fields,
            }
        )
        execution_values = tuple(event.get(field) for field in execution_fields)
        valid_execution_proof = (
            state.active_admission_committed
            and type(event.get("execution_journal_sequence")) is int
            and event["execution_journal_sequence"] > 0
            and type(event.get("execution_head_sequence")) is int
            and event["execution_head_sequence"] >= event["execution_journal_sequence"]
            and type(event.get("execution_journal_digest")) is str
            and _SHA256.fullmatch(event["execution_journal_digest"]) is not None
            and type(event.get("execution_head_digest")) is str
            and _SHA256.fullmatch(event["execution_head_digest"]) is not None
        ) or (
            not state.active_admission_committed
            and all(value is None for value in execution_values)
        )
        if (
            set(event) != expected_fields
            or state.active_identity is None
            or event["generation"] != state.active_generation
            or event["stage_ordinal"] != state.active_stage_ordinal
            or event["process_identity_sha256"] != state.active_identity.sha256
            or event["descendant_creation_denied"] is not True
            or event["local_process_quiesced"] is not True
            or event["venue_mutation_absent_proven"] is not False
            or type(event["attested_monotonic_ns"]) is not int
            or event["attested_monotonic_ns"] <= 0
            or not valid_execution_proof
            or (
                event_type == "IDENTITY_REAPED"
                and (
                    event["exact_pid_waited"] is not True
                    or event["waited_pid"] != state.active_identity.pid
                    or type(event["returncode"]) is not int
                    or (
                        event["signal"] is not None
                        and (type(event["signal"]) is not int or event["signal"] <= 0)
                    )
                )
            )
            or (
                event_type == "IDENTITY_ORPHAN_DISAPPEARED"
                and event["pid1_reap_observed"] is not True
            )
        ):
            raise ProcessBoundaryError("PROCESS_JOURNAL_REAP_INVALID")
        return _ProcessJournalState(
            lifecycle_started_at=state.lifecycle_started_at,
            lifecycle_deadline=state.lifecycle_deadline,
            execution_journal_path=state.execution_journal_path,
            active_identity=None,
            active_generation=None,
            active_stage_ordinal=None,
            active_admission_committed=False,
            last_generation=state.last_generation,
            last_stage_ordinal=state.last_stage_ordinal,
        )
    raise ProcessBoundaryError("PROCESS_JOURNAL_EVENT_INVALID")


def _read_process_head(path: Path) -> tuple[int, str]:
    try:
        file_stat = os.stat(path, follow_symlinks=False)
        _validate_owner_file(file_stat, "PROCESS_JOURNAL_HEAD")
        raw = path.read_bytes()
    except ProcessBoundaryError:
        raise
    except OSError as exc:
        raise ProcessBoundaryError("PROCESS_JOURNAL_HEAD_OPEN_FAILED") from exc
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProcessBoundaryError("PROCESS_JOURNAL_HEAD_INVALID") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "sequence", "digest"}
        or value["schema_version"] != _PROCESS_JOURNAL_HEAD_SCHEMA
        or type(value["sequence"]) is not int
        or value["sequence"] <= 0
        or type(value["digest"]) is not str
        or _SHA256.fullmatch(value["digest"]) is None
        or raw != _canonical_json(value) + b"\n"
    ):
        raise ProcessBoundaryError("PROCESS_JOURNAL_HEAD_INVALID")
    return value["sequence"], value["digest"]


def _write_process_head(path: Path, sequence: int, digest: str) -> None:
    encoded = (
        _canonical_json(
            {
                "schema_version": _PROCESS_JOURNAL_HEAD_SCHEMA,
                "sequence": sequence,
                "digest": digest,
            }
        )
        + b"\n"
    )
    parent_fd = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    temporary = f".{path.name}.{os.getpid()}.tmp"
    fd: int | None = None
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_fd,
        )
        os.fchmod(fd, 0o600)
        _write_all(fd, encoded)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temporary, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as exc:
        raise ProcessBoundaryError("PROCESS_JOURNAL_HEAD_WRITE_FAILED") from exc
    finally:
        if fd is not None:
            os.close(fd)
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent_fd)
        os.close(parent_fd)


def _boot_token() -> str:
    if sys.platform.startswith("linux"):
        try:
            value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except OSError as exc:
            raise ProcessBoundaryError("BOOT_IDENTITY_UNAVAILABLE") from exc
        if not value:
            raise ProcessBoundaryError("BOOT_IDENTITY_UNAVAILABLE")
        return f"linux:{value}"
    if sys.platform == "darwin":
        try:
            boot_file = os.stat("/var/run/syslog.pid", follow_symlinks=False)
        except OSError as exc:
            raise ProcessBoundaryError("BOOT_IDENTITY_UNAVAILABLE") from exc
        birth = int(getattr(boot_file, "st_birthtime", 0) * 1_000_000_000)
        if birth <= 0:
            raise ProcessBoundaryError("BOOT_IDENTITY_UNAVAILABLE")
        return f"darwin:{boot_file.st_dev}:{boot_file.st_ino}:{birth}"
    raise ProcessBoundaryError("BOOT_IDENTITY_UNSUPPORTED")


def _read_exact(
    stream: BinaryIO | socket.socket,
    size: int,
    *,
    empty_reason: str,
    partial_reason: str,
) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        try:
            if isinstance(stream, socket.socket):
                chunk = stream.recv(size - len(chunks))
            else:
                chunk = stream.read(size - len(chunks))
        except TimeoutError as exc:
            raise IPCProtocolError("IPC_TIMEOUT") from exc
        if not chunk:
            reason = empty_reason if not chunks else partial_reason
            raise IPCProtocolError(reason)
        chunks.extend(chunk)
    return bytes(chunks)


def _kill_and_wait_unadmitted(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired as exc:
        raise ProcessBoundaryError("UNADMITTED_CHILD_NOT_REAPED") from exc


def _signal_exact_child(child: ManagedChild) -> int | None:
    try:
        os.kill(child.identity.pid, signal.SIGKILL)
    except ProcessLookupError:
        return None
    return int(signal.SIGKILL)


class _ProcBSDInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _read_darwin_identity(pid: int) -> ProcessIdentity | None:
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    proc_pidinfo = libproc.proc_pidinfo
    proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    proc_pidinfo.restype = ctypes.c_int
    info = _ProcBSDInfo()
    size = ctypes.sizeof(info)
    result = proc_pidinfo(pid, 3, 0, ctypes.byref(info), size)
    if result <= 0:
        error = ctypes.get_errno()
        if error in {0, errno.ESRCH}:
            return None
        raise ProcessBoundaryError(f"PROC_PIDINFO_FAILED:{error}")
    if result != size or info.pbi_pid != pid:
        raise ProcessBoundaryError("PROC_PIDINFO_IDENTITY_INVALID")
    try:
        sid = os.getsid(pid)
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return None
    return ProcessIdentity(
        pid=pid,
        ppid=int(info.pbi_ppid),
        pgid=pgid,
        sid=sid,
        start_token=f"darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}",
    )


def _read_linux_identity(pid: int) -> ProcessIdentity | None:
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        raw = stat_path.read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    right_parenthesis = raw.rfind(")")
    if right_parenthesis < 0:
        raise ProcessBoundaryError("PROC_STAT_IDENTITY_INVALID")
    fields = raw[right_parenthesis + 2 :].split()
    if len(fields) < 20:
        raise ProcessBoundaryError("PROC_STAT_IDENTITY_INVALID")
    return ProcessIdentity(
        pid=pid,
        ppid=int(fields[1]),
        pgid=int(fields[2]),
        sid=int(fields[3]),
        start_token=f"linux:{fields[19]}",
    )


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != _TRUSTED_BOOTSTRAP_ARGUMENT:
        raise ProcessBoundaryError("TRUSTED_BOOTSTRAP_ARGUMENTS_INVALID")
    _trusted_bootstrap_main(sys.argv[2:])
