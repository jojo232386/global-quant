import importlib.util
import json
import pathlib
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "control_room" / "server.py"
SERVER_SPEC = importlib.util.spec_from_file_location("gmaq_control_room_test", SERVER_PATH)
assert SERVER_SPEC and SERVER_SPEC.loader
server = importlib.util.module_from_spec(SERVER_SPEC)
SERVER_SPEC.loader.exec_module(server)


@pytest.fixture()
def dashboard(monkeypatch):
    snapshot = {
        "schema_version": 1,
        "read_only": True,
        "actions_enabled": False,
        "gate": {"state": "DISARMED"},
        "runtime": {
            "health": {"verdict": "OFFLINE", "checks": []},
            "reconciliation": {"verdict": "UNKNOWN", "unknown_outcomes": []},
        },
        "audit": {"verdict": "VERIFIED"},
        "config": {"credential_free": True},
        "research": {"promotion_verdict": "BLOCKED_UNVERIFIED_ARTIFACTS", "studies": []},
        "blockers": [],
        "evidence": [],
    }
    monkeypatch.setattr(server, "cached_snapshot", lambda force=False: snapshot)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.ControlRoomHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def request(url, method="GET", host=None):
    headers = {"Host": host} if host else {}
    return urllib.request.urlopen(
        urllib.request.Request(url, method=method, headers=headers), timeout=2
    )


def test_snapshot_is_read_only_and_security_headers_are_present(dashboard):
    with request(f"{dashboard}/api/snapshot") as response:
        payload = json.loads(response.read())
        assert payload["read_only"] is True
        assert payload["actions_enabled"] is False
        assert payload["gate"]["state"] == "DISARMED"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_all_mutating_http_methods_are_rejected(dashboard, method):
    with pytest.raises(urllib.error.HTTPError) as error:
        request(f"{dashboard}/api/snapshot", method=method)
    assert error.value.code == 405
    assert b"mutations are disabled" in error.value.read()


def test_non_local_host_header_is_rejected(dashboard):
    with pytest.raises(urllib.error.HTTPError) as error:
        request(f"{dashboard}/api/snapshot", host="example.invalid")
    assert error.value.code == 421


def test_dashboard_has_no_action_api_or_order_controls():
    html = (ROOT / "control_room" / "static" / "index.html").read_text()
    javascript = (ROOT / "control_room" / "static" / "app.js").read_text()
    assert "/api/action" not in html + javascript
    assert 'method: "POST"' not in javascript
    assert "无 Arm / 无下单入口" in html
    assert "research.current_pass_count" in javascript
    assert "VERIFIED_CURATED_V1" in javascript
    assert "RESEARCH_PASS_SHADOW_ONLY" in javascript
    assert "const currentPass = 0" not in javascript


def test_safe_config_snapshot_never_returns_exchange_credentials(monkeypatch):
    monkeypatch.setattr(
        server,
        "read_json_object",
        lambda path: {
            "dry_run": True,
            "stake_currency": "USDT",
            "exchange": {
                "key": "do-not-return-this-key",
                "secret": "do-not-return-this-secret",
                "pair_whitelist": ["BTC/USDT:USDT"],
            },
        },
    )
    result = server.safe_config_snapshot()
    serialized = json.dumps(result)
    assert "do-not-return" not in serialized
    assert "key" not in result
    assert "secret" not in result
    assert result["credential_free"] is False


def test_research_snapshot_reads_verified_curated_v1_results(monkeypatch) -> None:
    monkeypatch.setattr(server, "replayed_curated_v1", server.verified_curated_v1)
    snapshot = server.research_snapshot()
    studies = {item["study_id"]: item for item in snapshot["studies"]}
    carry = studies["study-2026-08-20-btceth-spot-perp-carry"]
    trend = studies["study-2026-08-20-btceth-volscaled-ls-tsmom"]
    assert carry["evidence_generation"] == "VERIFIED_CURATED_V1"
    assert carry["total_return"] == pytest.approx(-0.001776255692616413)
    assert carry["sharpe"] == pytest.approx(-0.38091367612650073)
    assert carry["max_drawdown"] == pytest.approx(0.004595250858907329)
    assert carry["trade_count"] == 9
    assert trend["evidence_generation"] == "VERIFIED_CURATED_V1"
    assert snapshot["current_pass_count"] == 0
    assert snapshot["promotion_verdict"] == "BLOCKED_NO_CURRENT_PASS"


def test_research_binding_requires_all_v1_sha_pins() -> None:
    valid = {
        "dataset_binding": {
            "data_layer_version": 1,
            "integrity_verdict": "VERIFIED",
            "stage": "curated",
            "quality_verdict": "PASS",
            "dataset": "btceth-test",
            "dataset_id": "a" * 64,
            "schema_id": "d" * 64,
            "snapshot_manifest_sha256": "b" * 64,
            "files": {"bars": "c" * 64},
        }
    }
    assert server.verified_curated_v1(valid) is True
    valid["dataset_binding"]["files"]["bars"] = "not-a-sha"
    assert server.verified_curated_v1(valid) is False


def test_research_binding_requires_real_registry_replay(monkeypatch) -> None:
    result = {
        "dataset_binding": {
            "data_layer_version": 1,
            "dataset": "btceth-test",
            "integrity_verdict": "VERIFIED",
            "stage": "curated",
            "quality_verdict": "PASS",
            "dataset_id": "a" * 64,
            "schema_id": "b" * 64,
            "snapshot_manifest_sha256": "c" * 64,
            "files": {"bars": "d" * 64},
        }
    }
    replay = {
        "integrity_verdict": "VERIFIED",
        "stage": "curated",
        "quality_verdict": "PASS",
        "snapshot_id": "a" * 64,
        "schema_id": "b" * 64,
        "manifest_sha256": "c" * 64,
        "files": [{"role": "bars", "sha256": "d" * 64}],
    }
    calls = []

    def verify(data_root, dataset_id, **kwargs):
        calls.append((data_root, dataset_id, kwargs))
        return replay

    monkeypatch.setattr(server, "verify_snapshot", verify)
    assert server.replayed_curated_v1(result) is True
    assert calls == [
        (
            server.DATA_ROOT,
            "a" * 64,
            {"expected_dataset": "btceth-test", "minimum_stage": "curated"},
        )
    ]

    replay["manifest_sha256"] = "0" * 64
    assert server.replayed_curated_v1(result) is False

    def unavailable(*args, **kwargs):
        raise server.DataLayerError("registry absent")

    monkeypatch.setattr(server, "verify_snapshot", unavailable)
    assert server.replayed_curated_v1(result) is False

    monkeypatch.setattr(server, "verify_snapshot", lambda *_args, **_kwargs: {"files": [{}]})
    assert server.replayed_curated_v1(result) is False


def test_self_declared_research_pass_is_blocked_without_registry(monkeypatch, tmp_path: pathlib.Path) -> None:
    study = tmp_path / "study-pass"
    study.mkdir()
    (study / "results.json").write_text(
        json.dumps(
            {
                "study_id": "study-pass",
                "verdict": "PASS",
                "cost_model": {"sha256": server.cost_model_sha()},
                "dataset_binding": {
                    "data_layer_version": 1,
                    "integrity_verdict": "VERIFIED",
                    "stage": "curated",
                    "quality_verdict": "PASS",
                    "dataset": "btceth-test",
                    "dataset_id": "a" * 64,
                    "schema_id": "e" * 64,
                    "snapshot_manifest_sha256": "b" * 64,
                    "files": {"bars": "c" * 64},
                },
                "oos": {"baseline": {"net_total_return": 0.1}},
            }
        )
    )
    monkeypatch.setattr(server, "RESEARCH_DIR", tmp_path)
    monkeypatch.setattr(
        server,
        "verify_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(server.DataLayerError("registry absent")),
    )
    snapshot = server.research_snapshot()
    assert snapshot["studies"][0]["evidence_generation"] == "COST_MODEL_MATCH_ONLY"
    assert snapshot["current_pass_count"] == 0
    assert snapshot["promotion_verdict"] == "BLOCKED_NO_CURRENT_PASS"

    monkeypatch.setattr(
        server,
        "verify_snapshot",
        lambda *args, **kwargs: {
            "integrity_verdict": "VERIFIED",
            "stage": "curated",
            "quality_verdict": "PASS",
            "snapshot_id": "a" * 64,
            "schema_id": "e" * 64,
            "manifest_sha256": "b" * 64,
            "files": [{"role": "bars", "sha256": "c" * 64}],
        },
    )
    snapshot = server.research_snapshot()
    assert snapshot["current_pass_count"] == 1
    assert snapshot["promotion_verdict"] == "RESEARCH_PASS_SHADOW_ONLY"


def test_git_error_is_not_reported_as_clean(monkeypatch):
    monkeypatch.setattr(server, "runtime_probe", lambda: {})
    monkeypatch.setattr(server, "audit_snapshot", lambda: {})
    monkeypatch.setattr(server, "research_snapshot", lambda: {})
    monkeypatch.setattr(server, "parse_blockers", lambda: [])
    monkeypatch.setattr(server, "evidence_snapshot", lambda: [])
    monkeypatch.setattr(server, "git_text", lambda *args: None)
    monkeypatch.setattr(server, "read_json_object", lambda path: {})
    assert server.build_snapshot()["repo"]["clean"] is False


def test_runtime_probe_refuses_non_local_api_before_building_credentials(monkeypatch):
    monkeypatch.setattr(
        server.CONTROL,
        "read_env",
        lambda: {
            "GMAQ_API_BASE": "https://example.invalid",
            "GMAQ_CONTAINER_NAME": server.EXPECTED_CONTAINER,
        },
    )
    monkeypatch.setattr(
        server.CONTROL,
        "api_login",
        lambda env: pytest.fail("credentials must not be built for a non-local API"),
    )
    result = server.runtime_probe()
    assert result["health"]["verdict"] == "UNKNOWN"
    assert result["reconciliation"]["unknown_outcomes"] == ["api_boundary_mismatch"]
