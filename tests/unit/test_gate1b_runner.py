from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import global_quant.gate1b.runner as runner


def test_build_only_does_not_read_environment_credentials(tmp_path) -> None:
    exit_code, evidence_path = runner.run_build_only(tmp_path)
    payload = json.loads(evidence_path.read_text())

    assert exit_code == 0
    assert payload["status"] == "READY"
    assert payload["mode"] == "BUILD_ONLY"
    assert payload["network_accessed"] is False
    assert payload["credentials_read"] is False


def test_legacy_signed_preflight_surface_is_absent() -> None:
    source = Path(runner.__file__).read_text()

    assert not hasattr(runner, "run_preflight")
    for forbidden_name in (
        "load_demo_credentials",
        "run_signed_preflight",
        "DEMO_KEY_NAME",
        "DEMO_SECRET_NAME",
        '"--preflight"',
        '"--confirm-demo-only"',
    ):
        assert forbidden_name not in source


def test_main_rejects_preflight_before_environment_access(monkeypatch) -> None:
    class EnvironmentTrap(dict[str, str]):
        @staticmethod
        def _reject_credential(name: str) -> None:
            if name.startswith("BINANCE_"):
                raise AssertionError(f"legacy preflight accessed environment: {name}")

        def get(self, name: str, default=None):
            self._reject_credential(name)
            return super().get(name, default)

        def __getitem__(self, name: str) -> str:
            self._reject_credential(name)
            return super().__getitem__(name)

        def __contains__(self, name: object) -> bool:
            if isinstance(name, str):
                self._reject_credential(name)
            return super().__contains__(name)

    monkeypatch.setattr(runner.os, "environ", EnvironmentTrap())

    with pytest.raises(SystemExit) as exc_info:
        runner.main(["--preflight"])

    assert exc_info.value.code == 2


def test_demo_script_rejects_preflight_without_credential_evidence(tmp_path) -> None:
    api_key = "runner-preflight-api-key-canary"
    api_secret = "runner-preflight-api-secret-canary"
    evidence_dir = tmp_path / "evidence"
    completed = subprocess.run(
        [
            str(Path(os.sys.executable)),
            str(runner.PROJECT_ROOT / "scripts" / "run_gate_1b_demo.py"),
            "--preflight",
            "--evidence-dir",
            str(evidence_dir),
        ],
        cwd=runner.PROJECT_ROOT,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(runner.PROJECT_ROOT / "src"),
            "BINANCE_DEMO_API_KEY": api_key,
            "BINANCE_DEMO_API_SECRET": api_secret,
        },
        check=False,
        capture_output=True,
        text=True,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 2
    assert "error:" in output
    assert "--build-only" in output
    assert api_key not in output
    assert api_secret not in output
    assert not evidence_dir.exists()


def test_default_evidence_and_config_hash_bind_gate1b_v1_4() -> None:
    evidence_dir = runner.default_evidence_dir()
    expected_protocol = runner.PROJECT_ROOT / "protocols" / "NT_GATE_1B_V1_4.md"

    assert "gate1b-v1.4-" in evidence_dir.name
    assert expected_protocol.is_file()
    assert isinstance(Path(evidence_dir), Path)
    assert runner._config_hash() == runner._digest_files(
        (
            runner.PROJECT_ROOT / "src/global_quant/gate1b/config.py",
            runner.PROJECT_ROOT / "src/global_quant/gate1b/runtime.py",
            runner.PROJECT_ROOT / "src/global_quant/gate1b/safety.py",
            runner.PROJECT_ROOT / "src/global_quant/gate1b/credential_prompt.py",
            runner.PROJECT_ROOT / "scripts/run_gate_1b_prompted.py",
            expected_protocol,
        ),
    )
