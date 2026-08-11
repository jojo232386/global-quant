"""Fixed production entrypoint for one credential-bearing process generation.

The module deliberately has no eager project imports.  Under the production
``python -I -S credential_session.py`` invocation it first loads the trusted
process-boundary blob by its exact sibling path and re-attests the bootstrap.
Only then are the exact source and virtual-environment import paths installed.
No ``site.main()`` or ``.pth`` file is evaluated.

Credentials are entered through the child TTY, installed as IPC/process
canaries, and never leave this process.  The credential-free supervisor owns
authorization and all lifecycle decisions; this child only obeys its typed
``SESSION_INIT`` capability and exact command stream.
"""

from __future__ import annotations

import getpass
import importlib
import importlib.util
import json
import os
import resource
import secrets
import stat
import sys
from collections.abc import Callable, Mapping, MutableSequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

_CHILD_SCHEMA = "gate1b.credential-child.v1"
_PRE_EXIT_SCHEMA = "gate1b.credential-child-pre-exit.v1"
_PROCESS_MODULE_NAME = "global_quant.gate1b.process_boundary"
_EXECUTION_JOURNAL_NAME = "request-ledger.json"
_PRE_EXIT_NAME = "child-pre-exit.json"
_MAX_SECRET_SCAN_BYTES = 16 * 1024 * 1024
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


class CredentialSessionError(RuntimeError):
    """A sanitized fail-closed credential-child boundary error."""


@dataclass(frozen=True, slots=True)
class _RuntimeComponents:
    demo_credentials_type: type[Any]
    transport_type: type[Any]
    build_transport: Callable[..., Any]
    session_type: type[Any]
    load_persisted_intent: Callable[[Path], Any]


def _source_root(entrypoint: Path) -> Path:
    candidate = Path(entrypoint).resolve()
    if candidate.name != "credential_session.py" or candidate.parent.name != "gate1b":
        raise CredentialSessionError("CREDENTIAL_ENTRYPOINT_PATH_INVALID")
    root = candidate.parents[2]
    if not root.is_dir():
        raise CredentialSessionError("CREDENTIAL_SOURCE_ROOT_UNAVAILABLE")
    return root


def _load_trusted_process_boundary(*, entrypoint: Path | None = None) -> Any:
    """Load the exact sibling boundary blob and perform the first project call."""

    child_path = Path(__file__) if entrypoint is None else Path(entrypoint)
    process_path = child_path.resolve().with_name("process_boundary.py")
    if not process_path.is_file() or process_path.is_symlink():
        raise CredentialSessionError("TRUSTED_PROCESS_BOUNDARY_UNAVAILABLE")
    root = _source_root(child_path)
    root_text = str(root)
    if root_text not in sys.path:
        # ``credential_child_bootstrap`` lazily imports its stdlib-only durable
        # authority types.  The source root is therefore needed for that exact
        # call even though ``-I`` correctly discarded PYTHONPATH.
        sys.path.insert(0, root_text)

    existing = sys.modules.get(_PROCESS_MODULE_NAME)
    if existing is not None:
        existing_path = getattr(existing, "__file__", None)
        if existing_path is None or Path(existing_path).resolve() != process_path:
            raise CredentialSessionError("TRUSTED_PROCESS_BOUNDARY_IDENTITY_MISMATCH")
        module = existing
    else:
        spec = importlib.util.spec_from_file_location(_PROCESS_MODULE_NAME, process_path)
        if spec is None or spec.loader is None:
            raise CredentialSessionError("TRUSTED_PROCESS_BOUNDARY_UNAVAILABLE")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_PROCESS_MODULE_NAME] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(_PROCESS_MODULE_NAME, None)
            raise

    bootstrap = getattr(module, "credential_child_bootstrap", None)
    if not callable(bootstrap):
        raise CredentialSessionError("TRUSTED_PROCESS_BOOTSTRAP_UNAVAILABLE")
    return bootstrap()


def _activate_isolated_import_paths(
    *,
    executable: Path,
    entrypoint: Path,
    version: tuple[int, int],
    import_path: MutableSequence[str],
) -> tuple[Path, Path]:
    """Install two exact paths without importing ``site`` or evaluating ``.pth``."""

    executable = Path(executable)
    if (
        not executable.is_absolute()
        or len(version) != 2
        or any(type(item) is not int or item <= 0 for item in version)
    ):
        raise CredentialSessionError("ISOLATED_PYTHON_IDENTITY_INVALID")
    source_root = _source_root(Path(entrypoint))
    venv_root = executable.parent.parent
    site_packages = venv_root / "lib" / f"python{version[0]}.{version[1]}" / "site-packages"
    if not site_packages.is_dir() or site_packages.is_symlink():
        raise CredentialSessionError("VENV_SITE_PACKAGES_UNAVAILABLE")

    exact = (str(source_root), str(site_packages))
    retained = [entry for entry in import_path if entry not in exact]
    import_path[:] = [*exact, *retained]
    return source_root, site_packages


def _load_runtime_components(
    bootstrap: Any,
    *,
    importer: Callable[[str], ModuleType] = importlib.import_module,
) -> _RuntimeComponents:
    """Import each later project/network component behind a fresh timer proof."""

    names = (
        "global_quant.gate1b.safety",
        "global_quant.gate1b.credential_transport",
        "global_quant.gate1b.credential_execution_session",
        "global_quant.gate1b.durable_intent",
    )
    loaded: list[Any] = []
    for name in names:
        bootstrap.hard_deadline.assert_intact()
        loaded.append(importer(name))
    safety, transport, execution_session, durable_intent = loaded
    return _RuntimeComponents(
        demo_credentials_type=safety.DemoCredentials,
        transport_type=transport.ProcessBoundCredentialTransport,
        build_transport=transport.build_production_credential_transport,
        session_type=execution_session.CredentialExecutionSession,
        load_persisted_intent=durable_intent.load_persisted_intent,
    )


def _validate_credential_boundary(
    *,
    environ: Mapping[str, str],
    input_is_tty: bool,
) -> None:
    if _CREDENTIAL_ENVIRONMENT_NAMES.intersection(environ):
        raise CredentialSessionError("CREDENTIAL_ENVIRONMENT_MUST_BE_EMPTY")
    if input_is_tty is not True:
        raise CredentialSessionError("INTERACTIVE_TERMINAL_REQUIRED")


def _disable_core_dumps() -> None:
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (OSError, ValueError) as exc:
        raise CredentialSessionError("CORE_DUMP_GUARD_UNAVAILABLE") from exc


def _read_hidden_credentials(
    bootstrap: Any,
    *,
    prompt_secret: Callable[[str], str] = getpass.getpass,
    importer: Callable[[str], ModuleType] = importlib.import_module,
    environ: Mapping[str, str] | None = None,
    input_is_tty: bool | None = None,
) -> tuple[str, str, tuple[str, ...]]:
    """Read HMAC or Ed25519 material entirely through the child TTY."""

    parent_environment = dict(os.environ) if environ is None else environ
    tty = os.isatty(0) if input_is_tty is None else input_is_tty
    _validate_credential_boundary(environ=parent_environment, input_is_tty=tty)

    mode = prompt_secret("Credential mode [hmac|ed25519] (hidden): ")
    if mode not in {"hmac", "ed25519"}:
        raise CredentialSessionError("UNSUPPORTED_DEMO_KEY_TYPE")
    api_key = prompt_secret("Demo API key (hidden): ")
    if not api_key:
        raise CredentialSessionError("EMPTY_DEMO_CREDENTIAL")

    if mode == "hmac":
        api_secret = prompt_secret("Demo API secret (hidden): ")
        if not api_secret:
            raise CredentialSessionError("EMPTY_DEMO_CREDENTIAL")
        return api_key, api_secret, (api_key, api_secret)

    raw_path = prompt_secret("Ed25519 owner-only absolute key path (hidden): ")
    if not raw_path or "\0" in raw_path:
        raise CredentialSessionError("ED25519_PRIVATE_KEY_PATH_INVALID")
    normalized = Path(os.path.abspath(raw_path))
    if not Path(raw_path).is_absolute() or str(normalized) != raw_path:
        raise CredentialSessionError("ED25519_PRIVATE_KEY_PATH_INVALID")
    bootstrap.hard_deadline.assert_intact()
    prompt_module = importer("global_quant.gate1b.credential_prompt")
    try:
        api_secret = prompt_module.read_ed25519_private_key(normalized)
    except BaseException:
        raise CredentialSessionError("ED25519_PRIVATE_KEY_REJECTED") from None
    if type(api_secret) is not str or not api_secret:
        raise CredentialSessionError("ED25519_PRIVATE_KEY_REJECTED")
    return api_key, api_secret, (api_key, api_secret, raw_path)


def _canonical_json(value: Mapping[str, object]) -> bytes:
    try:
        return (
            json.dumps(
                dict(value),
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CredentialSessionError("CHILD_PRE_EXIT_INVALID") from exc


def _assert_no_forbidden(encoded: bytes, forbidden_values: tuple[str, ...]) -> None:
    for value in forbidden_values:
        if type(value) is not str or not value:
            raise CredentialSessionError("CREDENTIAL_CANARY_INVALID")
        escaped = json.dumps(value, ensure_ascii=True)[1:-1].encode("ascii")
        if value.encode("utf-8") in encoded or escaped in encoded:
            raise CredentialSessionError("CREDENTIAL_IN_CHILD_EVIDENCE")


def _write_all(descriptor: int, encoded: bytes) -> None:
    offset = 0
    while offset < len(encoded):
        written = os.write(descriptor, encoded[offset:])
        if written <= 0:
            raise CredentialSessionError("CHILD_PRE_EXIT_WRITE_FAILED")
        offset += written


def _validate_owner_file(metadata: os.stat_result, reason: str) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise CredentialSessionError(reason)


def _loaded_project_module_paths() -> tuple[str, ...]:
    """Report every currently loaded project module by canonical tracked path."""

    project_root = Path(__file__).resolve().parents[3]
    package_root = project_root / "src" / "global_quant"
    loaded: set[str] = set()
    for module_name, module in tuple(sys.modules.items()):
        project_named = module_name == "global_quant" or module_name.startswith("global_quant.")
        raw_path = getattr(module, "__file__", None)
        if type(raw_path) is not str or not raw_path:
            if project_named:
                raise CredentialSessionError("LOADED_PROJECT_MODULE_PATH_INVALID")
            continue
        candidate = Path(raw_path)
        if candidate.suffix in {".pyc", ".pyo"}:
            try:
                candidate = Path(importlib.util.source_from_cache(str(candidate)))
            except (NotImplementedError, ValueError):
                raise CredentialSessionError("LOADED_PROJECT_MODULE_PATH_INVALID") from None
        try:
            canonical = candidate.resolve(strict=True)
            canonical.relative_to(package_root)
            relative = canonical.relative_to(project_root).as_posix()
        except (OSError, ValueError):
            if project_named:
                raise CredentialSessionError("LOADED_PROJECT_MODULE_PATH_INVALID") from None
            continue
        loaded.add(relative)
    credential_entrypoint = "src/global_quant/gate1b/credential_session.py"
    if credential_entrypoint not in loaded:
        raise CredentialSessionError("LOADED_PROJECT_MODULE_SET_INCOMPLETE")
    return tuple(sorted(loaded))


def _scan_evidence_canaries(directory_fd: int, forbidden_values: tuple[str, ...]) -> None:
    for name in os.listdir(directory_fd):
        if name.startswith(".child-pre-exit-"):
            continue
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise CredentialSessionError("EVIDENCE_SECRET_SCAN_FAILED") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_SECRET_SCAN_BYTES:
                raise CredentialSessionError("EVIDENCE_SECRET_SCAN_FAILED")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(64 * 1024, _MAX_SECRET_SCAN_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > _MAX_SECRET_SCAN_BYTES:
                    raise CredentialSessionError("EVIDENCE_SECRET_SCAN_FAILED")
            _assert_no_forbidden(b"".join(chunks), forbidden_values)
        finally:
            os.close(descriptor)


def _write_child_pre_exit(
    *,
    session: Any,
    generation: int,
    capability: str,
    forbidden_values: tuple[str, ...],
) -> Path:
    """Publish the sole child artifact create-only after ``SESSION_FINISHED``."""

    if getattr(session, "finished", None) is not True:
        raise CredentialSessionError("SESSION_NOT_FINISHED")
    journal_path = getattr(session, "execution_journal_path", None)
    if not isinstance(journal_path, Path):
        raise CredentialSessionError("SESSION_JOURNAL_PATH_UNAVAILABLE")
    if (
        not journal_path.is_absolute()
        or str(journal_path.absolute()) != str(journal_path)
        or journal_path.name != _EXECUTION_JOURNAL_NAME
    ):
        raise CredentialSessionError("SESSION_JOURNAL_PATH_INVALID")
    if type(generation) is not int or generation <= 0 or capability not in {"PRIMARY", "RECOVERY"}:
        raise CredentialSessionError("CHILD_PRE_EXIT_INVALID")

    evidence_root = journal_path.parent
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        directory_fd = os.open(evidence_root, flags)
    except OSError as exc:
        raise CredentialSessionError("EVIDENCE_ROOT_UNAVAILABLE") from exc
    temporary_name: str | None = None
    try:
        root_metadata = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != os.getuid()
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
        ):
            raise CredentialSessionError("EVIDENCE_ROOT_PERMISSIONS_INVALID")
        try:
            journal_metadata = os.stat(
                _EXECUTION_JOURNAL_NAME,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise CredentialSessionError("SESSION_JOURNAL_UNAVAILABLE") from exc
        _validate_owner_file(journal_metadata, "SESSION_JOURNAL_PERMISSIONS_INVALID")
        try:
            os.stat(_PRE_EXIT_NAME, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise CredentialSessionError("CHILD_PRE_EXIT_ALREADY_EXISTS")

        _scan_evidence_canaries(directory_fd, forbidden_values)
        encoded = _canonical_json(
            {
                "capability": capability,
                "generation": generation,
                "local_exit_pending": True,
                "loaded_project_modules": list(_loaded_project_module_paths()),
                "redaction_status": "VERIFIED",
                "schema_version": _PRE_EXIT_SCHEMA,
                "session_finished": True,
                "status": "CHILD_COMPLETE",
            }
        )
        _assert_no_forbidden(encoded, forbidden_values)

        temporary_name = f".child-pre-exit-{secrets.token_hex(16)}.tmp"
        create_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(temporary_name, create_flags, 0o600, dir_fd=directory_fd)
        try:
            os.fchmod(descriptor, 0o600)
            source_metadata = os.fstat(descriptor)
            _validate_owner_file(source_metadata, "CHILD_PRE_EXIT_PERMISSIONS_INVALID")
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
            try:
                os.link(
                    temporary_name,
                    _PRE_EXIT_NAME,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise CredentialSessionError("CHILD_PRE_EXIT_ALREADY_EXISTS") from exc
            except OSError as exc:
                raise CredentialSessionError("CHILD_PRE_EXIT_PUBLISH_FAILED") from exc
            published_metadata = os.stat(
                _PRE_EXIT_NAME,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if (published_metadata.st_dev, published_metadata.st_ino) != (
                source_metadata.st_dev,
                source_metadata.st_ino,
            ) or published_metadata.st_size != len(encoded):
                with suppress(OSError):
                    os.unlink(_PRE_EXIT_NAME, dir_fd=directory_fd)
                raise CredentialSessionError("CHILD_PRE_EXIT_PUBLISH_IDENTITY_MISMATCH")
        finally:
            os.close(descriptor)
        os.fsync(directory_fd)
        os.unlink(temporary_name, dir_fd=directory_fd)
        temporary_name = None
        os.fsync(directory_fd)

        current_root = os.stat(evidence_root, follow_symlinks=False)
        if (current_root.st_dev, current_root.st_ino) != (
            root_metadata.st_dev,
            root_metadata.st_ino,
        ):
            raise CredentialSessionError("EVIDENCE_ROOT_IDENTITY_CHANGED")
        final_metadata = os.stat(_PRE_EXIT_NAME, dir_fd=directory_fd, follow_symlinks=False)
        _validate_owner_file(final_metadata, "CHILD_PRE_EXIT_PERMISSIONS_INVALID")
    finally:
        if temporary_name is not None:
            with suppress(OSError):
                os.unlink(temporary_name, dir_fd=directory_fd)
        os.close(directory_fd)
    return evidence_root / _PRE_EXIT_NAME


def run_credential_child(
    bootstrap: Any,
    *,
    prompt_secret: Callable[[str], str] = getpass.getpass,
    environ: Mapping[str, str] | None = None,
    input_is_tty: bool | None = None,
    core_dump_guard: Callable[[], None] = _disable_core_dumps,
    runtime_loader: Callable[[Any], _RuntimeComponents] = _load_runtime_components,
    credential_importer: Callable[[str], ModuleType] = importlib.import_module,
) -> int:
    """Run one primary or recovery-only typed credential child session."""

    api_key = ""
    api_secret = ""
    forbidden_values: tuple[str, ...] = ()
    transport: Any | None = None
    try:
        parent_environment = dict(os.environ) if environ is None else environ
        tty = os.isatty(0) if input_is_tty is None else input_is_tty
        _validate_credential_boundary(environ=parent_environment, input_is_tty=tty)
        core_dump_guard()
        api_key, api_secret, forbidden_values = _read_hidden_credentials(
            bootstrap,
            prompt_secret=prompt_secret,
            importer=credential_importer,
            environ=parent_environment,
            input_is_tty=tty,
        )
        bootstrap.install_credential_guard(*forbidden_values)

        components = runtime_loader(bootstrap)
        credentials = components.demo_credentials_type(
            api_key=api_key,
            api_secret=api_secret,
        )
        bootstrap.hard_deadline.assert_intact()
        io_authority = bootstrap.issue_io_authority()
        transport = components.build_transport(
            credentials,
            io_authority=io_authority,
        )
        if type(transport) is not components.transport_type:
            raise CredentialSessionError("PRODUCTION_TRANSPORT_TYPE_INVALID")

        bootstrap.channel.send(
            "CREDENTIAL_READY",
            {
                "schema_version": _CHILD_SCHEMA,
                "status": "READY",
                "generation": bootstrap.generation,
                "capability": bootstrap.capability.value,
                "guard_installed": True,
            },
        )

        def verified_intent_resolver(reference: Any) -> Any:
            path = getattr(reference, "intent_path", None)
            if not isinstance(path, Path):
                raise CredentialSessionError("INTENT_REFERENCE_INVALID")
            return components.load_persisted_intent(path)

        session = components.session_type(
            bootstrap=bootstrap,
            transport=transport,
            verified_intent_resolver=verified_intent_resolver,
        )
        session.run()
        if getattr(session, "finished", None) is not True:
            raise CredentialSessionError("SESSION_NOT_FINISHED")
        transport.close()
        transport = None
        _write_child_pre_exit(
            session=session,
            generation=bootstrap.generation,
            capability=bootstrap.capability.value,
            forbidden_values=forbidden_values,
        )
        return 0
    except BaseException:
        # Raw exceptions, responses, paths, and credential material are never
        # serialized.  EOF/exit is interpreted by the supervisor using the
        # durable dispatch frontier.
        return 1
    finally:
        if transport is not None:
            with suppress(BaseException):
                transport.close()
        api_key = ""
        api_secret = ""
        forbidden_values = ()


def _production_main(bootstrap: Any) -> int:
    if len(sys.argv) != 1:
        return 1
    return run_credential_child(bootstrap)


if __name__ == "__main__":
    _PRODUCTION_BOOTSTRAP = _load_trusted_process_boundary()
    _activate_isolated_import_paths(
        executable=Path(sys.executable),
        entrypoint=Path(__file__),
        version=(sys.version_info.major, sys.version_info.minor),
        import_path=sys.path,
    )
    _PRODUCTION_BOOTSTRAP.hard_deadline.assert_intact()
    raise SystemExit(_production_main(_PRODUCTION_BOOTSTRAP))
