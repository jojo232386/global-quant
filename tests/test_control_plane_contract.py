import importlib.util
import hashlib
import json
import pathlib
import threading
import time
from importlib.machinery import SourceFileLoader


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTROL_PLANE = ROOT / "configs" / "CONTROL_PLANE.md"
SCRIPT = ROOT / "scripts" / "gmaq-control"


def load_control() -> object:
    loader = SourceFileLoader("gmaq_control", str(SCRIPT))
    spec = importlib.util.spec_from_loader("gmaq_control", loader)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_control_plane_spec_covers_required_surface() -> None:
    text = CONTROL_PLANE.read_text()
    for section in (
        "Armed state model",
        "Order lifecycle state machine",
        "Unique client-order identity",
        "Reconciliation",
        "Audit manifest",
        "Health metrics",
        "Alerts",
        "Independent kill switch",
        "Dry-run constraints",
    ):
        assert f"## " in text and section in text, f"missing section: {section}"
    assert "UNKNOWN_OUTCOME" in text
    assert "no self-healing" in text
    assert "does not authorize live trading" in text
    assert "DRY_RUN_ONLY = TRUE" in text


def test_control_script_exposes_all_commands() -> None:
    text = SCRIPT.read_text()
    for command in (
        "bind-runtime",
        "preflight",
        "arm",
        "disarm",
        "pause",
        "recover",
        "state",
        "health",
        "reconcile",
        "audit",
        "exit",
        "kill",
    ):
        assert f'"{command}"' in text or f"'{command}'" in text, f"missing command: {command}"
    assert 'add_parser("preflight"' in text
    assert 'add_parser("arm"' in text
    assert 'add_parser("disarm"' in text
    assert 'add_parser("health"' in text
    assert 'add_parser("reconcile"' in text
    assert 'add_parser("exit"' in text
    assert 'add_parser("kill"' in text
    assert "__name__" in text


def test_exit_is_controlled_close_with_zero_position_proof() -> None:
    text = SCRIPT.read_text()
    assert '"/api/v1/forceexit"' in text
    assert '"tradeid"' in text
    assert '"ZERO_POSITIONS_AND_ORDERS"' in text
    assert "snapshot_is_zero_proof" in text
    assert "bot_open_trades" in text
    assert "bot_db_snapshot" in text
    assert 'verdict="TIMEOUT"' in text
    assert "zero proof not obtained within 120s" in text
    # Exit is not the kill switch: it goes through the bot API by design.
    exit_source = text[text.index("def exit_all") : text.index("def main")]
    assert "api_login" in exit_source
    assert "docker" not in exit_source


def test_status_is_open_trades_and_orders_are_nested() -> None:
    text = SCRIPT.read_text()
    status_source = text[text.index("def bot_open_trades") : text.index("def bot_trades")]
    assert '"/api/v1/status"' in status_source
    assert "open-trades view" in status_source
    assert "def bot_open_orders" not in text
    assert 'raw_trade.get("orders", [])' in text


def test_client_order_id_format_and_uniqueness() -> None:
    control = load_control()
    seen = set()
    for _ in range(5000):
        ident = control.client_order_id("ETH/USDT:USDT")
        assert ident.startswith("gmaq-dryrun-eth-usdt-usdt-")
        assert ident not in seen
        seen.add(ident)
    assert len(seen) == 5000


def test_armed_state_transitions_are_strict() -> None:
    control = load_control()
    allowed = [
        ("DISARMED", "PREFLIGHTING"),
        ("PREFLIGHTING", "PREFLIGHT_PASS"),
        ("PREFLIGHTING", "DISARMED"),
        ("PREFLIGHT_PASS", "ARMED"),
        ("ARMED", "PAUSED"),
        ("PAUSED", "ARMED"),
        ("ARMED", "KILLED"),
        ("PAUSED", "KILLED"),
        ("KILLED", "DISARMED"),
    ]
    for current, target in allowed:
        assert control.allowed_transition(current, target) is True
    for current, target in (
        ("DISARMED", "ARMED"),
        ("PREFLIGHT_PASS", "DISARMED"),
        ("KILLED", "ARMED"),
        ("PAUSED", "PREFLIGHTING"),
        ("ARMED", "PREFLIGHTING"),
    ):
        assert control.allowed_transition(current, target) is False


def test_audit_chain_is_hash_linked() -> None:
    control = load_control()
    first = {
        "seq": 1,
        "ts_utc": "2026-08-16T00:00:00",
        "actor": "test",
        "event": "first",
        "verdict": "OK",
        "refs": {},
        "prev_sha": "",
    }
    second = {
        "seq": 2,
        "ts_utc": "2026-08-16T00:00:01",
        "actor": "test",
        "event": "second",
        "verdict": "OK",
        "refs": {},
        "prev_sha": control.sha256_text(control.serialize(first)),
    }
    valid, reason = control.audit_chain_valid([first, second])
    assert valid, reason
    broken = dict(second, prev_sha="0" * 64)
    valid, reason = control.audit_chain_valid([first, broken])
    assert not valid
    assert "broken" in reason


def test_committed_config_passes_credential_free_check() -> None:
    control = load_control()
    config = control.load_config()
    assert control.config_is_credential_free(config) is True
    assert config["dry_run"] is True
    assert config["exchange"]["key"] == ""
    assert config["exchange"]["secret"] == ""


def test_committed_strategy_has_inner_protections() -> None:
    control = load_control()
    present, missing = control.strategy_has_protections()
    assert present, missing
    assert missing == []


def test_strategy_entry_path_calls_fail_closed_gate() -> None:
    text = (ROOT / "user_data" / "strategies" / "LiveExecutionCanaryStrategy.py").read_text()
    assert "def confirm_trade_entry(" in text
    assert "decision_from_environment()" in text
    assert "return decision.allowed" in text


def test_alerts_route_fail_verdicts_and_never_leak_credentials() -> None:
    text = SCRIPT.read_text()
    for marker in (
        "GMAQ_ALERT_WEBHOOK_URL",
        "GMAQ_TELEGRAM_BOT_TOKEN",
        "GMAQ_TELEGRAM_CHAT_ID",
        "https://api.telegram.org",
        '"alert-test"',
    ):
        assert marker in text, f"missing alert marker: {marker}"
    # Alert payloads are built from the event and verdict only.
    alert_source = text[text.index("def deliver_alert") : text.index("def dispatch_alert")]
    assert "GMAQ_API_PASSWORD" not in alert_source
    assert "GMAQ_API_USERNAME" not in alert_source
    # Every fail verdict in the registry triggers a dispatch somewhere.
    assert 'if verdict in ALERT_FAIL_VERDICTS' in text
    # Audit records alert delivery; a failed delivery is recorded, not silent.
    dispatch_source = text[text.index("def dispatch_alert") : text.index("def read_audit_chain")]
    assert '"alert"' in dispatch_source
    # alert-test fails closed when channels are configured but dead.
    assert "configured alert channels failed to deliver" in text
    assert "NO_CHANNELS" in text


def test_preflight_is_fail_closed_on_unknown_inputs() -> None:
    text = SCRIPT.read_text()
    assert "CLOCK_OFFSET_LIMIT_S" in text
    assert "exchange time unreachable" in text
    assert "login failed or credentials missing" in text
    assert "docker compose unavailable" in text
    # FAIL/UNKNOWN verdicts must exit non-zero, not just ok=False.
    assert "fail_verdicts" in text
    assert '"FAIL", "UNKNOWN", "UNHEALTHY", "BROKEN", "MISMATCH"' in text
    # Kill switch never depends on the bot API.
    kill_source = text[text.index("def kill") : text.index("def exit_all")]
    assert "api_login" not in kill_source
    assert "docker" in kill_source


def test_bind_and_arm_outputs_are_accepted_by_strategy_gate(tmp_path, monkeypatch) -> None:
    control = load_control()
    gate_path = ROOT / "user_data" / "strategies" / "gmaq_entry_gate.py"
    loader = SourceFileLoader("gmaq_entry_gate_control_test", str(gate_path))
    spec = importlib.util.spec_from_loader("gmaq_entry_gate_control_test", loader)
    gate = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    import sys

    sys.modules[spec.name] = gate
    spec.loader.exec_module(gate)

    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"dry_run": True, "exchange": {"key": "", "secret": ""}}) + "\n")
    config_sha = hashlib.sha256(config_path.read_bytes()).hexdigest()
    candidate_sha = "c" * 40

    monkeypatch.setattr(control, "CONFIG_PATH", config_path)
    monkeypatch.setattr(control, "AUDIT_DIR", audit_dir)
    monkeypatch.setattr(control, "AUDIT_PATH", audit_dir / "manifest.jsonl")
    monkeypatch.setattr(control, "STATE_PATH", audit_dir / "state.json")
    monkeypatch.setattr(control, "BINDING_PATH", audit_dir / "runtime-binding.json")
    monkeypatch.setattr(
        control,
        "git_text",
        lambda *args: candidate_sha if args == ("rev-parse", "HEAD") else "",
    )

    bound = control.bind_runtime("dryrun-control-0001", candidate_sha, config_sha, "dry_run")
    assert bound["verdict"] == "BOUND_DISARMED"
    binding = json.loads(control.BINDING_PATH.read_text())
    control.write_state(
        "PREFLIGHT_PASS",
        **control.binding_fields(binding),
        preflight_verdict="PASS",
        preflight_expires_at_epoch=2_000_000_000,
    )
    monkeypatch.setattr(control.time, "time", lambda: 1_900_000_000)
    armed = control.arm("demo-control-0001", 300)
    assert armed["verdict"] == "ARMED"

    decision = gate.evaluate_entry_gate(
        state_path=control.STATE_PATH,
        audit_path=control.AUDIT_PATH,
        config_path=config_path,
        expected_environment="dry_run",
        expected_candidate_sha=candidate_sha,
        expected_config_sha256=config_sha,
        expected_run_id="dryrun-control-0001",
        now_epoch=1_900_000_001,
    )
    assert decision.allowed is True

    control.disarm("test_complete")
    decision = gate.evaluate_entry_gate(
        state_path=control.STATE_PATH,
        audit_path=control.AUDIT_PATH,
        config_path=config_path,
        expected_environment="dry_run",
        expected_candidate_sha=candidate_sha,
        expected_config_sha256=config_sha,
        expected_run_id="dryrun-control-0001",
        now_epoch=1_900_000_002,
    )
    assert decision.allowed is False
    assert decision.reason == "state_not_armed"


def test_concurrent_disarm_cannot_be_overwritten_by_inflight_arm(tmp_path, monkeypatch) -> None:
    control = load_control()
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    fields = {
        "environment": "dry_run",
        "candidate_sha": "a" * 40,
        "config_sha256": "b" * 64,
        "run_id": "dryrun-race-proof",
    }
    monkeypatch.setattr(control, "AUDIT_DIR", audit_dir)
    monkeypatch.setattr(control, "STATE_PATH", audit_dir / "state.json")
    monkeypatch.setattr(control, "BINDING_PATH", audit_dir / "runtime-binding.json")
    monkeypatch.setattr(
        control,
        "validated_runtime_binding",
        lambda: ({"schema_version": 1, **fields}, None),
    )
    monkeypatch.setattr(control.time, "time", lambda: 1_900_000_000)
    control.write_state(
        "PREFLIGHT_PASS",
        **fields,
        preflight_verdict="PASS",
        preflight_expires_at_epoch=2_000_000_000,
    )

    arm_inside_audit = threading.Event()
    release_arm = threading.Event()

    def fake_append(event, actor, refs=None, verdict="OK"):
        if event == "arm":
            arm_inside_audit.set()
            assert release_arm.wait(2)
            return {"ok": True, "verdict": "APPENDED", "seq": 2, "record_sha": "c" * 64}
        return {"ok": True, "verdict": "APPENDED", "seq": 1, "record_sha": "d" * 64}

    monkeypatch.setattr(control, "append_audit", fake_append)
    outcomes = {}
    arm_thread = threading.Thread(
        target=lambda: outcomes.setdefault("arm", control.arm("authorization-race-proof", 300))
    )
    disarm_thread = threading.Thread(
        target=lambda: outcomes.setdefault("disarm", control.disarm("operator_emergency"))
    )
    arm_thread.start()
    assert arm_inside_audit.wait(2)
    disarm_thread.start()
    time.sleep(0.05)
    assert disarm_thread.is_alive(), "disarm should wait for the in-flight serialized transition"
    release_arm.set()
    arm_thread.join(2)
    disarm_thread.join(2)

    assert outcomes["arm"]["verdict"] == "ARMED"
    assert outcomes["disarm"]["verdict"] == "DISARMED"
    assert json.loads(control.STATE_PATH.read_text())["state"] == "DISARMED"
