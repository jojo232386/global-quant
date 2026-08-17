import hashlib
import importlib.util
import json
import pathlib
import sys
import time
import types

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
STRATEGY_DIR = ROOT / "user_data" / "strategies"
GATE_PATH = STRATEGY_DIR / "gmaq_entry_gate.py"
STRATEGY_PATH = STRATEGY_DIR / "LiveExecutionCanaryStrategy.py"
CANDIDATE_SHA = "a" * 40
RUN_ID = "dryrun-test-0001"
AUTHORIZATION_ID = "demo-auth-0001"


def canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def record_sha(value: dict) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load_gate():
    spec = importlib.util.spec_from_file_location("gmaq_entry_gate_test", GATE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_valid_gate(tmp_path: pathlib.Path, now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"dry_run": True, "exchange": {"key": "", "secret": ""}}) + "\n"
    )
    config_sha = hashlib.sha256(config_path.read_bytes()).hexdigest()
    refs = {
        "environment": "dry_run",
        "candidate_sha": CANDIDATE_SHA,
        "config_sha256": config_sha,
        "run_id": RUN_ID,
        "authorization_id": AUTHORIZATION_ID,
        "authorization_scope": "DEMO_DRY_RUN_ENTRY",
        "issued_at_epoch": now - 10,
        "expires_at_epoch": now + 300,
    }
    first = {
        "seq": 1,
        "ts_utc": "2027-01-15T00:00:00",
        "actor": "gmaq-control",
        "event": "runtime-bind",
        "verdict": "DISARMED",
        "refs": {},
        "prev_sha": "",
    }
    second = {
        "seq": 2,
        "ts_utc": "2027-01-15T00:00:01",
        "actor": "gmaq-control",
        "event": "preflight",
        "verdict": "PASS",
        "refs": {},
        "prev_sha": record_sha(first),
    }
    arm_record = {
        "seq": 3,
        "ts_utc": "2027-01-15T00:00:02",
        "actor": "gmaq-control",
        "event": "arm",
        "verdict": "ARMED",
        "refs": refs,
        "prev_sha": record_sha(second),
    }
    audit_path = tmp_path / "manifest.jsonl"
    audit_path.write_text("\n".join(canonical(record) for record in (first, second, arm_record)) + "\n")
    state = {
        "schema_version": 1,
        "state": "ARMED",
        "environment": "dry_run",
        "candidate_sha": CANDIDATE_SHA,
        "config_sha256": config_sha,
        "run_id": RUN_ID,
        "preflight_verdict": "PASS",
        "authorization_id": AUTHORIZATION_ID,
        "authorization_scope": "DEMO_DRY_RUN_ENTRY",
        "issued_at_epoch": now - 10,
        "expires_at_epoch": now + 300,
        "authorization_audit_seq": 3,
        "authorization_audit_sha": record_sha(arm_record),
    }
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state) + "\n")
    return {
        "state_path": state_path,
        "audit_path": audit_path,
        "config_path": config_path,
        "config_sha": config_sha,
        "state": state,
        "now": now,
    }


def evaluate(gate, artifact: dict, **overrides):
    values = {
        "state_path": artifact["state_path"],
        "audit_path": artifact["audit_path"],
        "config_path": artifact["config_path"],
        "expected_environment": "dry_run",
        "expected_candidate_sha": CANDIDATE_SHA,
        "expected_config_sha256": artifact["config_sha"],
        "expected_run_id": RUN_ID,
        "now_epoch": artifact["now"],
    }
    values.update(overrides)
    return gate.evaluate_entry_gate(**values)


def test_valid_audited_dry_run_authorization_allows_entry(tmp_path) -> None:
    gate = load_gate()
    artifact = write_valid_gate(tmp_path)
    decision = evaluate(gate, artifact)
    assert decision.allowed is True
    assert decision.reason == "authorized_dry_run_entry"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("state", "DISARMED", "state_not_armed"),
        ("candidate_sha", "b" * 40, "candidate_sha_mismatch"),
        ("config_sha256", "b" * 64, "config_sha_mismatch"),
        ("run_id", "dryrun-wrong-0001", "run_id_mismatch"),
        ("preflight_verdict", "FAIL", "preflight_not_passed"),
        ("authorization_scope", "LIVE_ENTRY", "authorization_scope_mismatch"),
    ],
)
def test_state_mismatch_fails_closed(tmp_path, field, value, reason) -> None:
    gate = load_gate()
    artifact = write_valid_gate(tmp_path)
    state = dict(artifact["state"], **{field: value})
    artifact["state_path"].write_text(json.dumps(state))
    decision = evaluate(gate, artifact)
    assert decision.allowed is False
    assert decision.reason == reason


def test_missing_corrupt_expired_and_broken_audit_fail_closed(tmp_path) -> None:
    gate = load_gate()
    artifact = write_valid_gate(tmp_path)

    artifact["state_path"].unlink()
    assert evaluate(gate, artifact).reason == "state_missing"

    artifact = write_valid_gate(tmp_path)
    artifact["state_path"].write_text("{")
    assert evaluate(gate, artifact).reason == "state_corrupt"

    artifact = write_valid_gate(tmp_path)
    assert evaluate(gate, artifact, now_epoch=artifact["now"] + 301).reason == "authorization_expired"

    artifact = write_valid_gate(tmp_path)
    lines = artifact["audit_path"].read_text().splitlines()
    second = json.loads(lines[1])
    second["prev_sha"] = "0" * 64
    lines[1] = canonical(second)
    artifact["audit_path"].write_text("\n".join(lines) + "\n")
    assert evaluate(gate, artifact).reason == "audit_chain_broken"


def test_freqtrade_callback_uses_gate_for_allow_and_reject(tmp_path, monkeypatch) -> None:
    artifact = write_valid_gate(tmp_path)
    monkeypatch.setenv("GMAQ_USER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GMAQ_GATE_ENVIRONMENT", "dry_run")
    monkeypatch.setenv("GMAQ_CANDIDATE_SHA", CANDIDATE_SHA)
    monkeypatch.setenv("GMAQ_CONFIG_SHA256", artifact["config_sha"])
    monkeypatch.setenv("GMAQ_RUN_ID", RUN_ID)
    monkeypatch.setenv("GMAQ_GATE_STATE_PATH", str(artifact["state_path"]))
    monkeypatch.setenv("GMAQ_AUDIT_PATH", str(artifact["audit_path"]))
    monkeypatch.setenv("GMAQ_CONFIG_PATH", str(artifact["config_path"]))

    freqtrade = types.ModuleType("freqtrade")
    persistence = types.ModuleType("freqtrade.persistence")
    strategy = types.ModuleType("freqtrade.strategy")
    pandas = types.ModuleType("pandas")
    persistence.Trade = type("Trade", (), {})
    strategy.IStrategy = type("IStrategy", (), {})
    pandas.DataFrame = type("DataFrame", (), {})
    sys.modules.update(
        {
            "freqtrade": freqtrade,
            "freqtrade.persistence": persistence,
            "freqtrade.strategy": strategy,
            "pandas": pandas,
        }
    )
    sys.path.insert(0, str(STRATEGY_DIR))
    try:
        spec = importlib.util.spec_from_file_location("gmaq_canary_gate_test", STRATEGY_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        instance = module.LiveExecutionCanaryStrategy()
        assert instance.confirm_trade_entry("ETH/USDT:USDT", "market", 1, 1, "GTC", None, None, "long") is True

        state = dict(artifact["state"], state="DISARMED")
        artifact["state_path"].write_text(json.dumps(state))
        assert instance.confirm_trade_entry("ETH/USDT:USDT", "market", 1, 1, "GTC", None, None, "long") is False
    finally:
        sys.path.remove(str(STRATEGY_DIR))
