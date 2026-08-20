"""Read-only binding of admission evidence to a clean committed candidate."""

from __future__ import annotations

import pathlib
import subprocess


def committed_candidate_sha(root: pathlib.Path) -> str:
    """Return HEAD only when Git cannot hide tracked or untracked changes."""

    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=root, text=True
    ).strip()
    if dirty:
        raise RuntimeError("candidate worktree must be clean and committed")

    tracked = subprocess.check_output(["git", "ls-files", "-v"], cwd=root, text=True)
    if any(line and line[0] != "H" for line in tracked.splitlines()):
        raise RuntimeError("candidate worktree contains hidden-index flags")

    staged = subprocess.run(
        ["git", "diff-index", "--cached", "--quiet", "HEAD", "--"],
        cwd=root,
        check=False,
    )
    if staged.returncode != 0:
        raise RuntimeError("candidate index differs from HEAD")
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
