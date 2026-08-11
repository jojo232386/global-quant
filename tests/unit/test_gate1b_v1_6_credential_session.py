"""Mechanical tests for the process-bound Gate 1B credential child."""

from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from global_quant.gate1b.credential_session import (
    CredentialSessionError,
    _activate_isolated_import_paths,
    _load_runtime_components,
    _loaded_project_module_paths,
    _read_hidden_credentials,
    _write_child_pre_exit,
    run_credential_child,
)

FAKE_KEY = "fake-demo-api-key-0123456789abcdef"
FAKE_SECRET = "fake-demo-api-secret-0123456789abcdef"
FAKE_PEM = "-----BEGIN PRIVATE KEY-----\nfake-private-key-material\n-----END PRIVATE KEY-----"


def _entrypoint() -> Path:
    return Path(__file__).resolve().parents[2] / "src/global_quant/gate1b/credential_session.py"


def test_production_entrypoint_bootstraps_under_isolated_no_site_first() -> None:
    project_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(_entrypoint())],
        cwd=project_root,
        env={},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode != 0
    assert "TRUSTED_BOOTSTRAP_REQUIRED" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def test_entrypoint_has_no_eager_project_import_or_site_main() -> None:
    tree = ast.parse(_entrypoint().read_text(encoding="utf-8"))
    imports: list[str] = []
    site_main_calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.append(node.module)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "site"
            and node.func.attr == "main"
        ):
            site_main_calls.append(node)

    assert not [name for name in imports if name == "site" or name.startswith("global_quant")]
    assert site_main_calls == []

    main_guard = next(
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
    )
    first_call = next(node for node in ast.walk(main_guard.body[0]) if isinstance(node, ast.Call))
    assert isinstance(first_call.func, ast.Name)
    assert first_call.func.id == "_load_trusted_process_boundary"


def test_isolated_paths_are_exact_and_do_not_execute_pth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv = tmp_path / "venv"
    executable = venv / "bin/python"
    executable.parent.mkdir(parents=True)
    executable.touch()
    site_packages = venv / "lib/python3.12/site-packages"
    site_packages.mkdir(parents=True)
    marker = tmp_path / "pth-executed"
    (site_packages / "malicious.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    source_root = tmp_path / "repo/src"
    entrypoint = source_root / "global_quant/gate1b/credential_session.py"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.touch()
    import_path = ["stdlib-a", str(source_root), "stdlib-b"]

    actual = _activate_isolated_import_paths(
        executable=executable,
        entrypoint=entrypoint,
        version=(3, 12),
        import_path=import_path,
    )

    assert actual == (source_root, site_packages)
    assert import_path == [str(source_root), str(site_packages), "stdlib-a", "stdlib-b"]
    assert marker.exists() is False


class _HardDeadline:
    def __init__(self, trace: list[object]) -> None:
        self.trace = trace

    def assert_intact(self) -> None:
        self.trace.append("deadline")


def test_every_runtime_project_import_has_immediate_deadline_reattestation() -> None:
    trace: list[object] = []
    bootstrap = SimpleNamespace(hard_deadline=_HardDeadline(trace))
    modules = {
        "global_quant.gate1b.safety": SimpleNamespace(DemoCredentials=object),
        "global_quant.gate1b.credential_transport": SimpleNamespace(
            ProcessBoundCredentialTransport=object,
            build_production_credential_transport=lambda *a, **k: None,
        ),
        "global_quant.gate1b.credential_execution_session": SimpleNamespace(
            CredentialExecutionSession=object,
        ),
        "global_quant.gate1b.durable_intent": SimpleNamespace(
            load_persisted_intent=lambda path: path,
        ),
    }

    def importer(name: str):
        trace.append(("import", name))
        return modules[name]

    components = _load_runtime_components(bootstrap, importer=importer)

    assert components.demo_credentials_type is object
    assert trace == [
        "deadline",
        ("import", "global_quant.gate1b.safety"),
        "deadline",
        ("import", "global_quant.gate1b.credential_transport"),
        "deadline",
        ("import", "global_quant.gate1b.credential_execution_session"),
        "deadline",
        ("import", "global_quant.gate1b.durable_intent"),
    ]


class _Capability:
    def __init__(self, value: str) -> None:
        self.value = value


class _Channel:
    def __init__(self, trace: list[object], forbidden: tuple[str, ...]) -> None:
        self.trace = trace
        self.forbidden = forbidden
        self.messages: list[tuple[str, dict[str, object]]] = []

    def send(self, kind: str, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, sort_keys=True)
        assert all(value not in encoded for value in self.forbidden)
        self.trace.append(("send", kind))
        self.messages.append((kind, payload))


class _Bootstrap:
    def __init__(self, trace: list[object], *, capability: str = "PRIMARY") -> None:
        self.trace = trace
        self.generation = 1 if capability == "PRIMARY" else 2
        self.capability = _Capability(capability)
        self.hard_deadline = _HardDeadline(trace)
        self.channel = _Channel(trace, (FAKE_KEY, FAKE_SECRET, FAKE_PEM))
        self.guard_values: tuple[str, ...] = ()
        self.io_authority = object()

    def install_credential_guard(self, *values: str) -> None:
        self.trace.append("guard")
        self.guard_values = values

    def assert_network_ready(self) -> None:
        assert self.guard_values

    def issue_io_authority(self) -> object:
        assert self.guard_values
        self.trace.append("io-authority")
        return self.io_authority


class _Credentials:
    def __init__(self, *, api_key: str, api_secret: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret


class _Transport:
    def __init__(self, trace: list[object]) -> None:
        self.trace = trace
        self.closed = False

    def execute_pre_intent(self, reservation: object) -> tuple[str, object]:
        return ("pre-intent", reservation)

    def close(self) -> None:
        self.trace.append("close")
        self.closed = True


def _owner_only_session_root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    journal = root / "request-ledger.json"
    descriptor = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    return root, journal


def _components(trace: list[object], journal: Path, *, finish: bool = True):
    class Session:
        def __init__(
            self,
            *,
            bootstrap: object,
            transport: object,
            verified_intent_resolver: object,
        ) -> None:
            assert isinstance(transport, _Transport)
            assert callable(verified_intent_resolver)
            trace.append(("session", bootstrap.capability.value))
            self.execution_journal_path = journal
            self.finished = False

        def run(self) -> None:
            trace.append("run")
            self.finished = finish

    def build(credentials: _Credentials, *, io_authority: object):
        assert credentials.api_key == FAKE_KEY
        assert credentials.api_secret in {FAKE_SECRET, FAKE_PEM}
        assert io_authority is not None
        trace.append("transport")
        return _Transport(trace)

    return SimpleNamespace(
        demo_credentials_type=_Credentials,
        transport_type=_Transport,
        build_transport=build,
        session_type=Session,
        load_persisted_intent=lambda path: ("intent", path),
    )


def _prompt(values: tuple[str, ...], trace: list[object]):
    pending = iter(values)

    def read(label: str) -> str:
        trace.append(("prompt", label))
        return next(pending)

    return read


def test_hmac_guard_precedes_ready_and_composes_typed_session(
    tmp_path: Path,
) -> None:
    trace: list[object] = []
    root, journal = _owner_only_session_root(tmp_path)
    bootstrap = _Bootstrap(trace)

    def runtime_loader(candidate: object):
        assert candidate.guard_values == (FAKE_KEY, FAKE_SECRET)
        trace.append("runtime-load")
        return _components(trace, journal)

    code = run_credential_child(
        bootstrap,
        prompt_secret=_prompt(("hmac", FAKE_KEY, FAKE_SECRET), trace),
        environ={},
        input_is_tty=True,
        core_dump_guard=lambda: trace.append("core-guard"),
        runtime_loader=runtime_loader,
    )

    assert code == 0
    assert trace.index("guard") < trace.index("runtime-load")
    assert trace.index("guard") < trace.index(("send", "CREDENTIAL_READY"))
    assert ("session", "PRIMARY") in trace
    assert trace[-1] == "close"
    assert bootstrap.channel.messages == [
        (
            "CREDENTIAL_READY",
            {
                "schema_version": "gate1b.credential-child.v1",
                "status": "READY",
                "generation": 1,
                "capability": "PRIMARY",
                "guard_installed": True,
            },
        )
    ]
    artifact = root / "child-pre-exit.json"
    payload = json.loads(artifact.read_text(encoding="ascii"))
    loaded_project_modules = payload.pop("loaded_project_modules")
    assert loaded_project_modules == sorted(set(loaded_project_modules))
    assert "src/global_quant/gate1b/credential_session.py" in loaded_project_modules
    assert payload == {
        "capability": "PRIMARY",
        "generation": 1,
        "local_exit_pending": True,
        "redaction_status": "VERIFIED",
        "schema_version": "gate1b.credential-child-pre-exit.v1",
        "session_finished": True,
        "status": "CHILD_COMPLETE",
    }
    assert "PASS" not in artifact.read_text(encoding="ascii")
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600


def test_ed25519_mode_prompts_hidden_absolute_path_and_uses_strict_reader(
    tmp_path: Path,
) -> None:
    trace: list[object] = []
    key_file = tmp_path / "owner-key.pem"
    key_file.write_text(FAKE_PEM, encoding="ascii")
    key_file.chmod(0o600)
    bootstrap = _Bootstrap(trace)

    def importer(name: str):
        assert name == "global_quant.gate1b.credential_prompt"
        trace.append(("import", name))

        def strict_reader(path: Path) -> str:
            assert path == key_file
            trace.append(("read-key", path))
            return FAKE_PEM

        return SimpleNamespace(read_ed25519_private_key=strict_reader)

    credentials = _read_hidden_credentials(
        bootstrap,
        prompt_secret=_prompt(("ed25519", FAKE_KEY, str(key_file)), trace),
        importer=importer,
        environ={},
        input_is_tty=True,
    )

    assert credentials == (FAKE_KEY, FAKE_PEM, (FAKE_KEY, FAKE_PEM, str(key_file)))
    import_index = trace.index(("import", "global_quant.gate1b.credential_prompt"))
    assert trace[import_index - 1] == "deadline"


def test_recovery_child_prompts_again_but_session_capability_stays_recovery(
    tmp_path: Path,
) -> None:
    trace: list[object] = []
    _, journal = _owner_only_session_root(tmp_path)
    bootstrap = _Bootstrap(trace, capability="RECOVERY")
    code = run_credential_child(
        bootstrap,
        prompt_secret=_prompt(("hmac", FAKE_KEY, FAKE_SECRET), trace),
        environ={},
        input_is_tty=True,
        core_dump_guard=lambda: None,
        runtime_loader=lambda _bootstrap: _components(trace, journal),
    )

    assert code == 0
    assert len([item for item in trace if isinstance(item, tuple) and item[0] == "prompt"]) == 3
    assert ("session", "RECOVERY") in trace
    assert bootstrap.channel.messages[0][1]["capability"] == "RECOVERY"


@pytest.mark.parametrize(
    ("environ", "input_is_tty", "reason"),
    [
        ({"BINANCE_DEMO_API_KEY": "present"}, True, "CREDENTIAL_ENVIRONMENT_MUST_BE_EMPTY"),
        ({}, False, "INTERACTIVE_TERMINAL_REQUIRED"),
    ],
)
def test_unsafe_boundary_rejected_before_prompt_or_ipc(
    environ: dict[str, str], input_is_tty: bool, reason: str
) -> None:
    trace: list[object] = []
    bootstrap = _Bootstrap(trace)
    with pytest.raises(CredentialSessionError, match=reason):
        _read_hidden_credentials(
            bootstrap,
            prompt_secret=_prompt(("hmac", FAKE_KEY, FAKE_SECRET), trace),
            environ=environ,
            input_is_tty=input_is_tty,
        )
    assert not [item for item in trace if isinstance(item, tuple) and item[0] == "send"]


def test_child_pre_exit_requires_finished_session_and_exact_owner_only_root(
    tmp_path: Path,
) -> None:
    root, journal = _owner_only_session_root(tmp_path)
    session = SimpleNamespace(execution_journal_path=journal, finished=False)
    with pytest.raises(CredentialSessionError, match="SESSION_NOT_FINISHED"):
        _write_child_pre_exit(
            session=session,
            generation=1,
            capability="PRIMARY",
            forbidden_values=(FAKE_KEY, FAKE_SECRET),
        )
    assert not (root / "child-pre-exit.json").exists()

    session.finished = True
    root.chmod(0o755)
    with pytest.raises(CredentialSessionError, match="EVIDENCE_ROOT_PERMISSIONS_INVALID"):
        _write_child_pre_exit(
            session=session,
            generation=1,
            capability="PRIMARY",
            forbidden_values=(FAKE_KEY, FAKE_SECRET),
        )


def test_child_pre_exit_is_create_only_and_never_overwrites(
    tmp_path: Path,
) -> None:
    root, journal = _owner_only_session_root(tmp_path)
    destination = root / "child-pre-exit.json"
    destination.write_text("retained", encoding="ascii")
    destination.chmod(0o600)
    session = SimpleNamespace(execution_journal_path=journal, finished=True)

    with pytest.raises(CredentialSessionError, match="CHILD_PRE_EXIT_ALREADY_EXISTS"):
        _write_child_pre_exit(
            session=session,
            generation=1,
            capability="PRIMARY",
            forbidden_values=(FAKE_KEY, FAKE_SECRET),
        )

    assert destination.read_text(encoding="ascii") == "retained"


def test_child_pre_exit_rejects_temporary_path_inode_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, journal = _owner_only_session_root(tmp_path)
    session = SimpleNamespace(execution_journal_path=journal, finished=True)
    real_link = os.link

    def substitute_then_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        follow_symlinks: bool,
    ) -> None:
        os.unlink(source, dir_fd=src_dir_fd)
        descriptor = os.open(
            source,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=src_dir_fd,
        )
        try:
            os.write(descriptor, b'{"status":"attacker-substitution"}\n')
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", substitute_then_link)

    with pytest.raises(
        CredentialSessionError,
        match="CHILD_PRE_EXIT_PUBLISH_IDENTITY_MISMATCH",
    ):
        _write_child_pre_exit(
            session=session,
            generation=1,
            capability="PRIMARY",
            forbidden_values=(FAKE_KEY, FAKE_SECRET),
        )

    assert not (root / "child-pre-exit.json").exists()


def test_child_pre_exit_rejects_raw_or_json_escaped_canary_in_wal(
    tmp_path: Path,
) -> None:
    root, journal = _owner_only_session_root(tmp_path)
    unicode_path = "/private/tmp/密钥.pem"
    journal.write_text(
        json.dumps({"accidental": unicode_path}, ensure_ascii=True),
        encoding="ascii",
    )
    session = SimpleNamespace(execution_journal_path=journal, finished=True)

    with pytest.raises(CredentialSessionError, match="CREDENTIAL_IN_CHILD_EVIDENCE"):
        _write_child_pre_exit(
            session=session,
            generation=1,
            capability="PRIMARY",
            forbidden_values=(FAKE_KEY, FAKE_SECRET, unicode_path),
        )

    assert not (root / "child-pre-exit.json").exists()


def test_loaded_project_module_report_rejects_shadow_module_outside_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shadow = tmp_path / "shadow.py"
    shadow.write_text("VALUE = 1\n", encoding="ascii")
    module = ModuleType("global_quant.gate1b.shadow")
    module.__file__ = str(shadow)
    monkeypatch.setitem(sys.modules, module.__name__, module)

    with pytest.raises(
        CredentialSessionError,
        match="LOADED_PROJECT_MODULE_PATH_INVALID",
    ):
        _loaded_project_module_paths()
