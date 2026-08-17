import importlib.util
import json
import pathlib
from importlib.machinery import SourceFileLoader


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gmaq-control"


def load_control(name: str = "gmaq_control_reconcile_test"):
    loader = SourceFileLoader(name, str(SCRIPT))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def matching_payloads():
    rest = [
        {
            "trade_id": 7,
            "pair": "ETH/USDT:USDT",
            "is_open": True,
            "is_short": False,
            "amount": 0.01,
            "open_rate": 2500.0,
            "orders": [
                {
                    "pair": "ETH/USDT:USDT",
                    "order_id": "dry-run-entry-7",
                    "status": "open",
                    "remaining": 0.006,
                    "amount": 0.01,
                    "safe_price": 2500.0,
                    "filled": 0.004,
                    "ft_order_side": "buy",
                    "is_open": True,
                }
            ],
        }
    ]
    database = {
        "trades": [
            {
                "trade_id": 7,
                "pair": "ETH/USDT:USDT",
                "is_open": True,
                "is_short": False,
                "amount": 0.01,
                "open_rate": 2500.0,
            }
        ],
        "orders": [
            {
                "trade_id": 7,
                "pair": "ETH/USDT:USDT",
                "order_id": "dry-run-entry-7",
                "status": "open",
                "remaining": 0.006,
                "amount": 0.01,
                "price": 2500.0,
                "filled": 0.004,
                "side": "buy",
                "is_open": True,
            }
        ],
    }
    return rest, database


def test_nested_partial_order_matches_by_identity_and_fields() -> None:
    control = load_control()
    rest, database = matching_payloads()
    result = control.compare_snapshots(rest, database)
    assert result["verdict"] == "MATCH"
    assert result["open_trades"] == 1
    assert result["open_orders"] == 1
    assert result["partial_orders"] == 1
    assert result["matches_database"] is True
    assert result["unknown_outcomes"] == []
    assert len(result["snapshot_sha256"]) == 64


def test_order_field_mismatch_and_orphan_open_order_are_not_count_matches() -> None:
    control = load_control()
    rest, database = matching_payloads()
    database["orders"][0]["filled"] = 0.003
    result = control.compare_snapshots(rest, database)
    assert result["verdict"] == "MISMATCH"
    assert result["mismatches"] == ["order_fields:dry-run-entry-7"]

    rest, database = matching_payloads()
    database["orders"].append(
        {
            "trade_id": 99,
            "pair": "ETH/USDT:USDT",
            "order_id": "orphan-open-order",
            "status": "open",
            "remaining": 1.0,
            "amount": 1.0,
            "price": 2500.0,
            "filled": 0.0,
            "side": "buy",
            "is_open": True,
        }
    )
    result = control.compare_snapshots(rest, database)
    assert result["verdict"] == "MISMATCH"
    assert "order_identity:orphan-open-order" in result["mismatches"]


def test_duplicate_or_unknown_order_state_is_quarantined() -> None:
    control = load_control()
    rest, database = matching_payloads()
    rest[0]["orders"].append(dict(rest[0]["orders"][0]))
    result = control.compare_snapshots(rest, database)
    assert result["verdict"] == "UNKNOWN"
    assert "duplicate REST order_id" in result["unknown_outcomes"][0]

    rest, database = matching_payloads()
    rest[0]["orders"][0]["status"] = "maybe-filled"
    result = control.compare_snapshots(rest, database)
    assert result["verdict"] == "UNKNOWN"
    assert "status is unknown" in result["unknown_outcomes"][0]


def test_zero_proof_requires_rest_db_match_and_no_unknowns() -> None:
    control = load_control()
    result = control.compare_snapshots([], {"trades": [], "orders": []})
    assert result["verdict"] == "MATCH"
    assert control.snapshot_is_zero_proof(result) is True

    result["unknown_outcomes"] = ["ambiguous"]
    assert control.snapshot_is_zero_proof(result) is False
    result["unknown_outcomes"] = []
    result["open_orders"] = 1
    assert control.snapshot_is_zero_proof(result) is False


def test_reconcile_mismatch_disarms_and_writes_real_top_level_verdict(tmp_path, monkeypatch) -> None:
    control = load_control("gmaq_control_reconcile_disarm_test")
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    monkeypatch.setattr(control, "AUDIT_DIR", audit_dir)
    monkeypatch.setattr(control, "AUDIT_PATH", audit_dir / "manifest.jsonl")
    monkeypatch.setattr(control, "AUDIT_LOCK_PATH", audit_dir / ".manifest.lock")
    monkeypatch.setattr(control, "STATE_PATH", audit_dir / "state.json")
    monkeypatch.setattr(control, "BINDING_PATH", audit_dir / "runtime-binding.json")
    monkeypatch.setattr(control, "read_env", lambda: {"GMAQ_API_USERNAME": "set", "GMAQ_API_PASSWORD": "set"})
    monkeypatch.setattr(control, "api_login", lambda env: "opaque-token")
    monkeypatch.setattr(control, "dispatch_alert", lambda *args, **kwargs: {"sent": False})
    rest, database = matching_payloads()
    database["orders"][0]["remaining"] = 0.005
    monkeypatch.setattr(control, "bot_open_trades", lambda token: rest)
    monkeypatch.setattr(control, "bot_db_snapshot", lambda env: database)

    result = control.reconcile()
    assert result["verdict"] == "MISMATCH"
    state = json.loads(control.STATE_PATH.read_text())
    assert state["state"] == "DISARMED"
    assert state["reason"] == "reconcile_mismatch"
    chain = control.read_audit_chain()
    assert chain[0]["event"] == "reconcile"
    assert chain[0]["verdict"] == "MISMATCH"
    assert control.audit_chain_valid(chain)[0] is True
