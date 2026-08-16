import importlib.util
import pathlib
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
    for command in ("preflight", "health", "reconcile", "audit", "kill"):
        assert f'"{command}"' in text or f"'{command}'" in text, f"missing command: {command}"
    assert 'add_parser("preflight"' in text
    assert 'add_parser("health"' in text
    assert 'add_parser("reconcile"' in text
    assert 'add_parser("kill"' in text
    assert "__name__" in text


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
    kill_source = text[text.index("def kill") :]
    assert "api_login" not in kill_source
    assert "docker" in kill_source
