from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from global_quant.gate1b.runtime_binding import (
    RuntimeBindingError,
    verify_runtime_binding,
    verify_runtime_unchanged,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path, *, annotated: bool = True) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Gate Test")
    _git(root, "config", "user.email", "gate@example.invalid")
    protocol = root / "protocols" / "NT_GATE_1B_V1_6.md"
    entrypoint = root / "scripts" / "run.py"
    module = root / "src" / "global_quant" / "gate1b" / "worker.py"
    protocol.parent.mkdir(parents=True)
    entrypoint.parent.mkdir(parents=True)
    module.parent.mkdir(parents=True)
    protocol.write_text("frozen protocol\n", encoding="ascii")
    entrypoint.write_text("from global_quant.gate1b import worker\n", encoding="ascii")
    module.write_text("VALUE = 1\n", encoding="ascii")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "protocol")
    protocol_commit = _git(root, "rev-parse", "HEAD^{commit}")
    if annotated:
        _git(root, "tag", "-a", "nt-gate-1b-v1.6-protocol", "-m", "freeze")
    else:
        _git(root, "tag", "nt-gate-1b-v1.6-protocol")
    tag_object = _git(root, "rev-parse", "refs/tags/nt-gate-1b-v1.6-protocol")
    (root / "README.md").write_text("runtime\n", encoding="ascii")
    _git(root, "add", "README.md")
    _git(root, "commit", "-qm", "runtime")
    return root, {
        "runtime_commit": _git(root, "rev-parse", "HEAD^{commit}"),
        "protocol_commit": protocol_commit,
        "protocol_tag_object": tag_object,
        "protocol_sha256": hashlib.sha256(protocol.read_bytes()).hexdigest(),
    }


def _verify(root: Path, binding: dict[str, str]):
    return verify_runtime_binding(
        root,
        expected_runtime_commit=binding["runtime_commit"],
        expected_protocol_commit=binding["protocol_commit"],
        expected_protocol_tag_object=binding["protocol_tag_object"],
        expected_protocol_sha256=binding["protocol_sha256"],
        required_source_paths=(
            root / "scripts" / "run.py",
            root / "src" / "global_quant" / "gate1b" / "worker.py",
        ),
    )


def test_clean_linked_compatible_repo_produces_exact_source_snapshot(tmp_path: Path) -> None:
    root, binding = _repo(tmp_path)

    snapshot = _verify(root, binding)

    assert snapshot.runtime_commit == binding["runtime_commit"]
    assert snapshot.protocol_commit == binding["protocol_commit"]
    assert snapshot.protocol_tag_object == binding["protocol_tag_object"]
    assert snapshot.protocol_sha256 == binding["protocol_sha256"]
    assert snapshot.branch in {"main", "master"}
    assert tuple(item.relative_path for item in snapshot.sources) == (
        "scripts/run.py",
        "src/global_quant/gate1b/worker.py",
    )
    assert all(len(item.git_blob) == 40 and len(item.sha256) == 64 for item in snapshot.sources)


def test_tracked_dirty_runtime_fails_closed(tmp_path: Path) -> None:
    root, binding = _repo(tmp_path)
    (root / "scripts" / "run.py").write_text("changed\n", encoding="ascii")

    with pytest.raises(RuntimeBindingError, match="RUNTIME_TRACKED_DIRTY"):
        _verify(root, binding)


def test_untracked_import_shadow_fails_closed(tmp_path: Path) -> None:
    root, binding = _repo(tmp_path)
    (root / "src" / "global_quant" / "gate1b" / "shadow.py").write_text(
        "SECRET = 1\n", encoding="ascii"
    )

    with pytest.raises(RuntimeBindingError, match="RUNTIME_UNTRACKED_FILES_PRESENT"):
        _verify(root, binding)


def test_lightweight_protocol_tag_is_rejected(tmp_path: Path) -> None:
    root, binding = _repo(tmp_path, annotated=False)

    with pytest.raises(RuntimeBindingError, match="PROTOCOL_TAG_NOT_ANNOTATED"):
        _verify(root, binding)


@pytest.mark.parametrize(
    ("field", "replacement", "reason"),
    [
        ("runtime_commit", "f" * 40, "RUNTIME_COMMIT_MISMATCH"),
        ("protocol_commit", "e" * 40, "PROTOCOL_COMMIT_MISMATCH"),
        ("protocol_tag_object", "d" * 40, "PROTOCOL_TAG_OBJECT_MISMATCH"),
        ("protocol_sha256", "c" * 64, "PROTOCOL_SHA256_MISMATCH"),
    ],
)
def test_caller_binding_cannot_replace_recomputed_git_facts(
    tmp_path: Path,
    field: str,
    replacement: str,
    reason: str,
) -> None:
    root, binding = _repo(tmp_path)
    binding[field] = replacement

    with pytest.raises(RuntimeBindingError, match=reason):
        _verify(root, binding)


def test_required_source_must_be_tracked_under_exact_project_root(tmp_path: Path) -> None:
    root, binding = _repo(tmp_path)
    outside = tmp_path / "worker.py"
    outside.write_text("VALUE = 1\n", encoding="ascii")

    with pytest.raises(RuntimeBindingError, match="RUNTIME_SOURCE_OUTSIDE_PROJECT"):
        verify_runtime_binding(
            root,
            expected_runtime_commit=binding["runtime_commit"],
            expected_protocol_commit=binding["protocol_commit"],
            expected_protocol_tag_object=binding["protocol_tag_object"],
            expected_protocol_sha256=binding["protocol_sha256"],
            required_source_paths=(outside,),
        )


def test_ignored_parent_symlink_cannot_alias_a_tracked_project_module(tmp_path: Path) -> None:
    root, _binding = _repo(tmp_path)
    (root / ".gitignore").write_text("alias\n", encoding="ascii")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-qm", "ignore runtime alias")
    binding = {
        "runtime_commit": _git(root, "rev-parse", "HEAD^{commit}"),
        "protocol_commit": _git(root, "rev-parse", "refs/tags/nt-gate-1b-v1.6-protocol^{commit}"),
        "protocol_tag_object": _git(root, "rev-parse", "refs/tags/nt-gate-1b-v1.6-protocol"),
        "protocol_sha256": hashlib.sha256(
            (root / "protocols" / "NT_GATE_1B_V1_6.md").read_bytes()
        ).hexdigest(),
    }
    (root / "alias").symlink_to(root / "src" / "global_quant" / "gate1b")

    with pytest.raises(RuntimeBindingError, match="RUNTIME_SOURCE_SYMLINK"):
        verify_runtime_binding(
            root,
            expected_runtime_commit=binding["runtime_commit"],
            expected_protocol_commit=binding["protocol_commit"],
            expected_protocol_tag_object=binding["protocol_tag_object"],
            expected_protocol_sha256=binding["protocol_sha256"],
            required_source_paths=(root / "alias" / "worker.py",),
        )


def test_post_snapshot_rejects_loaded_project_module_outside_checkout(tmp_path: Path) -> None:
    root, binding = _repo(tmp_path)
    before = _verify(root, binding)
    outside = tmp_path / "global_quant" / "gate1b" / "evil.py"
    outside.parent.mkdir(parents=True)
    outside.write_text("VALUE = 1\n", encoding="ascii")

    with pytest.raises(RuntimeBindingError, match="RUNTIME_SOURCE_OUTSIDE_PROJECT"):
        verify_runtime_unchanged(
            before,
            loaded_project_module_paths=(
                root / "src" / "global_quant" / "gate1b" / "worker.py",
                outside,
            ),
        )


def test_post_snapshot_rejects_empty_or_incomplete_child_module_report(tmp_path: Path) -> None:
    root, binding = _repo(tmp_path)
    before = _verify(root, binding)

    with pytest.raises(RuntimeBindingError, match="LOADED_PROJECT_MODULE_SET_INCOMPLETE"):
        verify_runtime_unchanged(before, loaded_project_module_paths=())


def test_post_snapshot_rechecks_head_tree_cleanliness_and_loaded_modules(tmp_path: Path) -> None:
    root, binding = _repo(tmp_path)
    before = _verify(root, binding)
    extra = root / "src" / "global_quant" / "gate1b" / "extra.py"
    extra.write_text("VALUE = 2\n", encoding="ascii")
    _git(root, "add", "src/global_quant/gate1b/extra.py")
    _git(root, "commit", "-qm", "unexpected runtime move")

    with pytest.raises(RuntimeBindingError, match="RUNTIME_COMMIT_MISMATCH"):
        verify_runtime_unchanged(
            before,
            loaded_project_module_paths=(
                root / "src" / "global_quant" / "gate1b" / "worker.py",
                extra,
            ),
        )


def test_unbound_source_cannot_execute_between_pre_and_post_snapshots(tmp_path: Path) -> None:
    root, binding = _repo(tmp_path)
    worker = root / "src" / "global_quant" / "gate1b" / "worker.py"
    original = worker.read_bytes()
    before = _verify(root, binding)

    worker.write_bytes(b"VALUE = 'unbound execution'\n")
    assert hashlib.sha256(worker.read_bytes()).hexdigest() not in {
        source.sha256 for source in before.sources
    }
    worker.write_bytes(original)

    with pytest.raises(RuntimeBindingError, match="RUNTIME_CHANGED_DURING_SESSION"):
        verify_runtime_unchanged(
            before,
            loaded_project_module_paths=(worker,),
        )


def test_omitted_tracked_project_module_fails_closed(tmp_path: Path) -> None:
    root, binding = _repo(tmp_path)
    extra = root / "src" / "global_quant" / "gate1b" / "extra.py"
    extra.write_text("VALUE = 2\n", encoding="ascii")
    _git(root, "add", "src/global_quant/gate1b/extra.py")
    _git(root, "commit", "-qm", "add transitive module")
    binding["runtime_commit"] = _git(root, "rev-parse", "HEAD^{commit}")

    before = _verify(root, binding)

    assert "src/global_quant/gate1b/extra.py" in {source.relative_path for source in before.sources}


def test_ignored_untracked_project_source_fails_closed(tmp_path: Path) -> None:
    root, _binding = _repo(tmp_path)
    (root / ".gitignore").write_text("src/global_quant/gate1b/shadow.py\n", encoding="ascii")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-qm", "ignore shadow")
    binding = {
        "runtime_commit": _git(root, "rev-parse", "HEAD^{commit}"),
        "protocol_commit": _git(root, "rev-parse", "refs/tags/nt-gate-1b-v1.6-protocol^{commit}"),
        "protocol_tag_object": _git(root, "rev-parse", "refs/tags/nt-gate-1b-v1.6-protocol"),
        "protocol_sha256": hashlib.sha256(
            (root / "protocols" / "NT_GATE_1B_V1_6.md").read_bytes()
        ).hexdigest(),
    }
    (root / "src" / "global_quant" / "gate1b" / "shadow.py").write_text(
        "VALUE = 'ignored import shadow'\n",
        encoding="ascii",
    )

    with pytest.raises(RuntimeBindingError, match="RUNTIME_UNTRACKED_PROJECT_SOURCE"):
        _verify(root, binding)


def test_protocol_must_be_regular_and_bound_to_head_blob(tmp_path: Path) -> None:
    root, binding = _repo(tmp_path)
    protocol = root / "protocols" / "NT_GATE_1B_V1_6.md"
    outside = tmp_path / "same-protocol.md"
    outside.write_bytes(protocol.read_bytes())
    protocol.unlink()
    protocol.symlink_to(outside)
    _git(root, "add", "protocols/NT_GATE_1B_V1_6.md")
    _git(root, "commit", "-qm", "replace protocol with symlink")
    binding["runtime_commit"] = _git(root, "rev-parse", "HEAD^{commit}")

    with pytest.raises(RuntimeBindingError, match="PROTOCOL_FILE_NOT_REGULAR"):
        _verify(root, binding)


def test_ambient_path_cannot_select_git_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, binding = _repo(tmp_path)
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    marker = tmp_path / "shim-executed"
    shim = shim_dir / "git"
    shim.write_text(
        f"#!/bin/sh\ntouch {marker}\nexit 93\n",
        encoding="ascii",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    _verify(root, binding)

    assert not marker.exists()


def test_macos_var_or_tmp_alias_is_canonicalized_without_allowing_in_tree_alias(
    tmp_path: Path,
) -> None:
    root, binding = _repo(tmp_path)
    worker = root / "src" / "global_quant" / "gate1b" / "worker.py"
    canonical = str(worker)
    if canonical.startswith("/private/var/"):
        aliased = Path(canonical.replace("/private/var/", "/var/", 1))
    elif canonical.startswith("/private/tmp/"):
        aliased = Path(canonical.replace("/private/tmp/", "/tmp/", 1))
    else:
        pytest.skip("test requires the macOS canonical /private alias")

    snapshot = verify_runtime_binding(
        root,
        expected_runtime_commit=binding["runtime_commit"],
        expected_protocol_commit=binding["protocol_commit"],
        expected_protocol_tag_object=binding["protocol_tag_object"],
        expected_protocol_sha256=binding["protocol_sha256"],
        required_source_paths=(root / "scripts" / "run.py", aliased),
    )

    assert "src/global_quant/gate1b/worker.py" in {
        source.relative_path for source in snapshot.sources
    }
