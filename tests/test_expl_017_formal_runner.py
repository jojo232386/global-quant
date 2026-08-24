from __future__ import annotations

import pathlib
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research" / "exploration"))
import expl_017_formal_runner as runner  # noqa: E402


def test_runner_verifies_freeze_and_refuses_a_run_without_independent_approval(tmp_path):
    freeze = runner.load_freeze()
    assert freeze["formal_run_id"] == "EXPL-017-FORMAL-003"
    with pytest.raises(runner.FormalRunnerError, match="approval"):
        runner.run(tmp_path, tmp_path / "formal-result.json")
