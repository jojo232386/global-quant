from __future__ import annotations

import json
from decimal import Decimal

import global_quant.gate1b.runner as runner
from global_quant.gate1b.preflight import AccountPreflight
from global_quant.gate1b.preflight import evaluate_account_preflight


def clean_snapshot() -> AccountPreflight:
    return AccountPreflight(
        can_trade=True,
        dual_side_position=False,
        wallet_balance=Decimal("10000"),
        nonzero_positions=(),
        open_regular_order_ids=(),
        open_algo_order_ids=(),
        server_time_skew_ms=3,
        trading_instruments=frozenset({"BTCUSDT", "ETHUSDT"}),
    )


def test_build_only_does_not_read_environment_credentials(tmp_path, monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("build-only read credentials")

    monkeypatch.setattr(runner, "load_demo_credentials", forbidden)

    exit_code, evidence_path = runner.run_build_only(tmp_path)
    payload = json.loads(evidence_path.read_text())

    assert exit_code == 0
    assert payload["status"] == "READY"
    assert payload["mode"] == "BUILD_ONLY"
    assert payload["network_accessed"] is False
    assert payload["credentials_read"] is False


def test_missing_demo_credentials_is_inconclusive_without_network(tmp_path) -> None:
    exit_code, evidence_path = runner.run_preflight(
        environ={},
        confirm_demo_only=True,
        evidence_dir=tmp_path,
    )
    payload = json.loads(evidence_path.read_text())

    assert exit_code == 2
    assert payload["status"] == "INCONCLUSIVE"
    assert payload["reason_codes"] == ["MISSING_DEMO_CREDENTIALS"]
    assert payload["network_accessed"] is False


def test_conflicting_credential_scope_stops_before_network(tmp_path) -> None:
    exit_code, evidence_path = runner.run_preflight(
        environ={
            "BINANCE_DEMO_API_KEY": "demo-key",
            "BINANCE_DEMO_API_SECRET": "demo-secret",
            "BINANCE_API_KEY": "live-key-must-never-be-read",
        },
        confirm_demo_only=True,
        evidence_dir=tmp_path,
    )
    payload = json.loads(evidence_path.read_text())

    assert exit_code == 1
    assert payload["status"] == "STOP"
    assert payload["reason_codes"] == ["CONFLICTING_CREDENTIAL_SCOPE"]
    assert payload["network_accessed"] is False
    assert "live-key-must-never-be-read" not in evidence_path.read_text()


def test_successful_preflight_writes_only_sanitized_evidence(tmp_path, monkeypatch) -> None:
    snapshot = clean_snapshot()

    async def fake_signed_preflight(credentials):
        assert credentials.api_key == "sensitive-demo-key"
        return snapshot, evaluate_account_preflight(snapshot)

    monkeypatch.setattr(runner, "run_signed_preflight", fake_signed_preflight)
    exit_code, evidence_path = runner.run_preflight(
        environ={
            "BINANCE_DEMO_API_KEY": "sensitive-demo-key",
            "BINANCE_DEMO_API_SECRET": "sensitive-demo-secret",
        },
        confirm_demo_only=True,
        evidence_dir=tmp_path,
    )
    encoded = evidence_path.read_text()
    payload = json.loads(encoded)

    assert exit_code == 0
    assert payload["status"] == "PASS"
    assert payload["network_accessed"] is True
    assert payload["credential_presence"] == {
        "BINANCE_DEMO_API_KEY": True,
        "BINANCE_DEMO_API_SECRET": True,
    }
    assert "sensitive-demo-key" not in encoded
    assert "sensitive-demo-secret" not in encoded
