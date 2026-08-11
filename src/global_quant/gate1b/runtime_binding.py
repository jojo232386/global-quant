"""Credential-free committed-runtime binding for Gate 1B v1.6.

The verifier uses only local Git object data.  It accepts no working-tree file
as authoritative until that file is tracked and its current bytes hash to the
exact ``HEAD`` blob.  The same operation is run before credential admission and
after process exit, extending the required source set with every project module
path reported by the fixed credential entrypoint.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Self

_PROTOCOL_TAG = "nt-gate-1b-v1.6-protocol"
_PROTOCOL_RELATIVE_PATH = Path("protocols/NT_GATE_1B_V1_6.md")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_EXECUTABLE = "/usr/bin/git"
_PROJECT_SOURCE_PREFIX = "src/global_quant"
_IMPORT_CAPABLE_SUFFIXES = frozenset(
    {".cfg", ".dylib", ".ini", ".json", ".py", ".pyi", ".so", ".toml", ".yaml", ".yml"}
)


class RuntimeBindingError(RuntimeError):
    """The live checkout cannot be bound to the claimed committed runtime."""


@dataclass(frozen=True, slots=True)
class SourceBinding:
    relative_path: str
    git_blob: str
    sha256: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    def __post_init__(self) -> None:
        if (
            type(self.relative_path) is not str
            or not self.relative_path
            or self.relative_path.startswith("/")
            or type(self.git_blob) is not str
            or _COMMIT.fullmatch(self.git_blob) is None
            or type(self.sha256) is not str
            or _SHA256.fullmatch(self.sha256) is None
            or any(
                type(value) is not int or value < 0
                for value in (
                    self.device,
                    self.inode,
                    self.size,
                    self.mtime_ns,
                    self.ctime_ns,
                )
            )
        ):
            raise RuntimeBindingError("RUNTIME_SOURCE_BINDING_INVALID")


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    project_root: Path
    runtime_commit: str
    runtime_tree: str
    branch: str
    protocol_commit: str
    protocol_tag_object: str
    protocol_sha256: str
    protocol_source: SourceBinding
    required_project_modules: tuple[str, ...]
    sources: tuple[SourceBinding, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.project_root, Path)
            or _COMMIT.fullmatch(self.runtime_commit) is None
            or _COMMIT.fullmatch(self.runtime_tree) is None
            or type(self.branch) is not str
            or not self.branch
            or _COMMIT.fullmatch(self.protocol_commit) is None
            or _COMMIT.fullmatch(self.protocol_tag_object) is None
            or _SHA256.fullmatch(self.protocol_sha256) is None
            or type(self.protocol_source) is not SourceBinding
            or self.protocol_source.relative_path != _PROTOCOL_RELATIVE_PATH.as_posix()
            or type(self.required_project_modules) is not tuple
            or tuple(sorted(set(self.required_project_modules))) != self.required_project_modules
            or any(
                not path.startswith(f"{_PROJECT_SOURCE_PREFIX}/")
                for path in self.required_project_modules
            )
            or not self.sources
            or tuple(sorted(self.sources, key=lambda item: item.relative_path)) != self.sources
            or len({item.relative_path for item in self.sources}) != len(self.sources)
        ):
            raise RuntimeBindingError("RUNTIME_SNAPSHOT_INVALID")

    @classmethod
    def build(
        cls,
        *,
        project_root: Path,
        runtime_commit: str,
        runtime_tree: str,
        branch: str,
        protocol_commit: str,
        protocol_tag_object: str,
        protocol_sha256: str,
        protocol_source: SourceBinding,
        required_project_modules: tuple[str, ...],
        sources: tuple[SourceBinding, ...],
    ) -> Self:
        return cls(
            project_root=project_root,
            runtime_commit=runtime_commit,
            runtime_tree=runtime_tree,
            branch=branch,
            protocol_commit=protocol_commit,
            protocol_tag_object=protocol_tag_object,
            protocol_sha256=protocol_sha256,
            protocol_source=protocol_source,
            required_project_modules=required_project_modules,
            sources=sources,
        )


def _run_git(
    root: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    environment = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    try:
        return subprocess.run(
            [_GIT_EXECUTABLE, *arguments],
            cwd=root,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=check,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeBindingError("RUNTIME_GIT_STATE_INVALID") from exc


def _git_text(root: Path, *arguments: str) -> str:
    try:
        return _run_git(root, *arguments).stdout.decode("ascii").strip()
    except UnicodeError as exc:
        raise RuntimeBindingError("RUNTIME_GIT_STATE_INVALID") from exc


def _validated_root(project_root: Path) -> Path:
    if not isinstance(project_root, Path):
        raise RuntimeBindingError("PROJECT_ROOT_INVALID")
    try:
        root = project_root.resolve(strict=True)
        entry = root.stat(follow_symlinks=False)
    except OSError as exc:
        raise RuntimeBindingError("PROJECT_ROOT_INVALID") from exc
    if not stat.S_ISDIR(entry.st_mode):
        raise RuntimeBindingError("PROJECT_ROOT_INVALID")
    try:
        top = Path(_git_text(root, "rev-parse", "--show-toplevel")).resolve(strict=True)
    except OSError as exc:
        raise RuntimeBindingError("PROJECT_ROOT_INVALID") from exc
    if top != root:
        raise RuntimeBindingError("PROJECT_ROOT_MISMATCH")
    return root


def _validate_expected_binding(
    *,
    runtime_commit: str,
    protocol_commit: str,
    protocol_tag_object: str,
    protocol_sha256: str,
) -> None:
    if (
        type(runtime_commit) is not str
        or _COMMIT.fullmatch(runtime_commit) is None
        or type(protocol_commit) is not str
        or _COMMIT.fullmatch(protocol_commit) is None
        or type(protocol_tag_object) is not str
        or _COMMIT.fullmatch(protocol_tag_object) is None
        or type(protocol_sha256) is not str
        or _SHA256.fullmatch(protocol_sha256) is None
    ):
        raise RuntimeBindingError("EXPECTED_RUNTIME_BINDING_INVALID")


def _assert_clean(root: Path) -> None:
    dirty = _run_git(
        root,
        "diff",
        "--quiet",
        "--no-ext-diff",
        "HEAD",
        "--",
        check=False,
    )
    if dirty.returncode == 1:
        raise RuntimeBindingError("RUNTIME_TRACKED_DIRTY")
    if dirty.returncode != 0:
        raise RuntimeBindingError("RUNTIME_GIT_STATE_INVALID")
    untracked = _run_git(root, "ls-files", "--others", "--exclude-standard", "-z")
    if untracked.stdout:
        raise RuntimeBindingError("RUNTIME_UNTRACKED_FILES_PRESENT")


def _assert_no_untracked_project_source(root: Path) -> None:
    """Ignored evidence is allowed, ignored import-capable project code is not."""

    untracked = _run_git(root, "ls-files", "--others", "-z", "--", _PROJECT_SOURCE_PREFIX)
    for raw_path in untracked.stdout.split(b"\0"):
        if not raw_path:
            continue
        try:
            relative = Path(raw_path.decode("utf-8"))
        except UnicodeError as exc:
            raise RuntimeBindingError("RUNTIME_UNTRACKED_PROJECT_SOURCE") from exc
        if relative.suffix.casefold() in _IMPORT_CAPABLE_SUFFIXES:
            raise RuntimeBindingError("RUNTIME_UNTRACKED_PROJECT_SOURCE")


def _tracked_project_sources(root: Path) -> tuple[Path, ...]:
    """Bind the entire import-capable project package, not a caller-selected subset."""

    tracked = _run_git(root, "ls-files", "-z", "--", _PROJECT_SOURCE_PREFIX)
    paths: list[Path] = []
    for raw_path in tracked.stdout.split(b"\0"):
        if not raw_path:
            continue
        try:
            relative = Path(raw_path.decode("utf-8"))
        except UnicodeError as exc:
            raise RuntimeBindingError("RUNTIME_GIT_STATE_INVALID") from exc
        if relative.suffix.casefold() in _IMPORT_CAPABLE_SUFFIXES:
            paths.append(root / relative)
    return tuple(paths)


def _source_binding(root: Path, source_path: Path) -> SourceBinding:
    if not isinstance(source_path, Path):
        raise RuntimeBindingError("RUNTIME_SOURCE_PATH_INVALID")
    _assert_no_symlink_below_root(root, source_path)
    try:
        lexical = _lexical_source_path(root, source_path)
        resolved = lexical.resolve(strict=True)
        relative = resolved.relative_to(root)
        entry = resolved.stat(follow_symlinks=False)
    except ValueError as exc:
        raise RuntimeBindingError("RUNTIME_SOURCE_OUTSIDE_PROJECT") from exc
    except OSError as exc:
        raise RuntimeBindingError("RUNTIME_SOURCE_UNAVAILABLE") from exc
    if lexical.is_symlink() or not stat.S_ISREG(entry.st_mode):
        raise RuntimeBindingError("RUNTIME_SOURCE_NOT_REGULAR")
    relative_text = relative.as_posix()
    tracked = _run_git(
        root,
        "ls-files",
        "--error-unmatch",
        "--",
        relative_text,
        check=False,
    )
    if tracked.returncode != 0:
        raise RuntimeBindingError("RUNTIME_SOURCE_NOT_TRACKED")
    disk_blob = _git_text(root, "hash-object", "--", relative_text)
    try:
        head_blob = _git_text(root, "rev-parse", f"HEAD:{relative_text}")
    except RuntimeBindingError as exc:
        raise RuntimeBindingError("RUNTIME_SOURCE_NOT_TRACKED") from exc
    if disk_blob != head_blob:
        raise RuntimeBindingError("RUNTIME_SOURCE_BYTES_CHANGED")
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise RuntimeBindingError("RUNTIME_SOURCE_UNAVAILABLE") from exc
    # Recheck the path after reading, closing the ordinary replacement window.
    try:
        after = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise RuntimeBindingError("RUNTIME_SOURCE_UNAVAILABLE") from exc
    if (entry.st_dev, entry.st_ino, entry.st_size, entry.st_mtime_ns, entry.st_ctime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise RuntimeBindingError("RUNTIME_SOURCE_PATH_RACE")
    # This reproduces the repository's SHA-1 Git object identity; SHA-256 above
    # remains the evidence integrity digest.
    raw_blob = hashlib.sha1(f"blob {len(raw)}\0".encode("ascii") + raw).hexdigest()
    if raw_blob != head_blob:
        raise RuntimeBindingError("RUNTIME_SOURCE_BYTES_CHANGED")
    return SourceBinding(
        relative_path=relative_text,
        git_blob=head_blob,
        sha256=hashlib.sha256(raw).hexdigest(),
        device=entry.st_dev,
        inode=entry.st_ino,
        size=entry.st_size,
        mtime_ns=entry.st_mtime_ns,
        ctime_ns=entry.st_ctime_ns,
    )


def _assert_no_symlink_below_root(root: Path, source_path: Path) -> None:
    """Require the reported path itself to be under root and symlink-free."""

    candidate = _lexical_source_path(root, source_path)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        # A path through an out-of-tree symlink is not the tracked module path,
        # even if its eventual target happens to resolve inside the checkout.
        raise RuntimeBindingError("RUNTIME_SOURCE_OUTSIDE_PROJECT") from exc
    if not relative.parts:
        raise RuntimeBindingError("RUNTIME_SOURCE_OUTSIDE_PROJECT")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise RuntimeBindingError("RUNTIME_SOURCE_SYMLINK")


def _lexical_source_path(root: Path, source_path: Path) -> Path:
    """Normalize only macOS' fixed /var and /tmp aliases, never arbitrary aliases."""

    candidate = Path(os.path.abspath(source_path))
    try:
        candidate.relative_to(root)
        return candidate
    except ValueError:
        pass
    for alias, canonical in (
        (Path("/var"), Path("/private/var")),
        (Path("/tmp"), Path("/private/tmp")),
    ):
        try:
            suffix = candidate.relative_to(alias)
        except ValueError:
            continue
        mapped = canonical / suffix
        try:
            mapped.relative_to(root)
        except ValueError:
            continue
        return mapped
    raise RuntimeBindingError("RUNTIME_SOURCE_OUTSIDE_PROJECT")


def verify_runtime_binding(
    project_root: Path,
    *,
    expected_runtime_commit: str,
    expected_protocol_commit: str,
    expected_protocol_tag_object: str,
    expected_protocol_sha256: str,
    required_source_paths: tuple[Path, ...],
) -> RuntimeSnapshot:
    """Recompute the exact clean checkout, protocol, and source identities."""

    _validate_expected_binding(
        runtime_commit=expected_runtime_commit,
        protocol_commit=expected_protocol_commit,
        protocol_tag_object=expected_protocol_tag_object,
        protocol_sha256=expected_protocol_sha256,
    )
    if type(required_source_paths) is not tuple or not required_source_paths:
        raise RuntimeBindingError("RUNTIME_SOURCE_SET_REQUIRED")
    root = _validated_root(project_root)
    tag_ref = f"refs/tags/{_PROTOCOL_TAG}"
    if _git_text(root, "cat-file", "-t", tag_ref) != "tag":
        raise RuntimeBindingError("PROTOCOL_TAG_NOT_ANNOTATED")
    actual_head = _git_text(root, "rev-parse", "HEAD^{commit}")
    actual_tree = _git_text(root, "rev-parse", "HEAD^{tree}")
    actual_branch = _git_text(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    actual_tag_object = _git_text(root, "rev-parse", tag_ref)
    actual_protocol_commit = _git_text(root, "rev-parse", f"{tag_ref}^{{commit}}")
    _assert_clean(root)
    _assert_no_untracked_project_source(root)
    ancestor = _run_git(
        root,
        "merge-base",
        "--is-ancestor",
        actual_protocol_commit,
        actual_head,
        check=False,
    )
    if ancestor.returncode == 1:
        raise RuntimeBindingError("PROTOCOL_TAG_NOT_ANCESTOR")
    if ancestor.returncode != 0:
        raise RuntimeBindingError("RUNTIME_GIT_STATE_INVALID")
    if actual_head != expected_runtime_commit:
        raise RuntimeBindingError("RUNTIME_COMMIT_MISMATCH")
    if actual_protocol_commit != expected_protocol_commit:
        raise RuntimeBindingError("PROTOCOL_COMMIT_MISMATCH")
    if actual_tag_object != expected_protocol_tag_object:
        raise RuntimeBindingError("PROTOCOL_TAG_OBJECT_MISMATCH")

    protocol_path = root / _PROTOCOL_RELATIVE_PATH
    try:
        protocol_entry = protocol_path.lstat()
        if not stat.S_ISREG(protocol_entry.st_mode):
            raise RuntimeBindingError("PROTOCOL_FILE_NOT_REGULAR")
        current_protocol = protocol_path.read_bytes()
    except RuntimeBindingError:
        raise
    except OSError as exc:
        raise RuntimeBindingError("PROTOCOL_FILE_UNAVAILABLE") from exc
    tagged_protocol = _run_git(
        root,
        "show",
        f"{tag_ref}:{_PROTOCOL_RELATIVE_PATH.as_posix()}",
    ).stdout
    if current_protocol != tagged_protocol:
        raise RuntimeBindingError("PROTOCOL_BYTES_CHANGED_AFTER_FREEZE")
    actual_protocol_sha256 = hashlib.sha256(current_protocol).hexdigest()
    if actual_protocol_sha256 != expected_protocol_sha256:
        raise RuntimeBindingError("PROTOCOL_SHA256_MISMATCH")
    protocol_source = _source_binding(root, protocol_path)

    sources_by_path: dict[str, SourceBinding] = {}
    required_project_modules: set[str] = set()
    for source_path in required_source_paths:
        required_binding = _source_binding(root, source_path)
        if required_binding.relative_path.startswith(f"{_PROJECT_SOURCE_PREFIX}/"):
            required_project_modules.add(required_binding.relative_path)
    all_sources = (*required_source_paths, *_tracked_project_sources(root))
    for source_path in all_sources:
        binding = _source_binding(root, source_path)
        if binding.relative_path in sources_by_path:
            if sources_by_path[binding.relative_path] != binding:
                raise RuntimeBindingError("RUNTIME_SOURCE_DUPLICATE")
            continue
        sources_by_path[binding.relative_path] = binding
    return RuntimeSnapshot.build(
        project_root=root,
        runtime_commit=actual_head,
        runtime_tree=actual_tree,
        branch=actual_branch,
        protocol_commit=actual_protocol_commit,
        protocol_tag_object=actual_tag_object,
        protocol_sha256=actual_protocol_sha256,
        protocol_source=protocol_source,
        required_project_modules=tuple(sorted(required_project_modules)),
        sources=tuple(sorted(sources_by_path.values(), key=lambda item: item.relative_path)),
    )


def verify_runtime_unchanged(
    before: RuntimeSnapshot,
    *,
    loaded_project_module_paths: tuple[Path, ...],
) -> RuntimeSnapshot:
    """Repeat the binding after reap and include every child-reported module."""

    if type(before) is not RuntimeSnapshot:
        raise RuntimeBindingError("RUNTIME_SNAPSHOT_REQUIRED")
    if type(loaded_project_module_paths) is not tuple:
        raise RuntimeBindingError("LOADED_PROJECT_MODULE_SET_INVALID")
    if not loaded_project_module_paths:
        raise RuntimeBindingError("LOADED_PROJECT_MODULE_SET_INCOMPLETE")
    loaded: dict[str, Path] = {}
    for path in loaded_project_module_paths:
        binding = _source_binding(before.project_root, path)
        if binding.relative_path in loaded:
            raise RuntimeBindingError("LOADED_PROJECT_MODULE_DUPLICATE")
        loaded[binding.relative_path] = path
    statically_required_modules = set(before.required_project_modules)
    if not statically_required_modules.issubset(loaded):
        raise RuntimeBindingError("LOADED_PROJECT_MODULE_SET_INCOMPLETE")
    required_by_relative = {
        source.relative_path: before.project_root / source.relative_path
        for source in before.sources
    }
    required_by_relative.update(loaded)
    required = tuple(required_by_relative[name] for name in sorted(required_by_relative))
    after = verify_runtime_binding(
        before.project_root,
        expected_runtime_commit=before.runtime_commit,
        expected_protocol_commit=before.protocol_commit,
        expected_protocol_tag_object=before.protocol_tag_object,
        expected_protocol_sha256=before.protocol_sha256,
        required_source_paths=required,
    )
    if (
        after.runtime_tree != before.runtime_tree
        or after.branch != before.branch
        or after.protocol_source != before.protocol_source
        or any(source not in after.sources for source in before.sources)
    ):
        raise RuntimeBindingError("RUNTIME_CHANGED_DURING_SESSION")
    return after
