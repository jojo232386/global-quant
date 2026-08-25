from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "research" / "data" / "forward_pit_capture.py"
STATUS_PATH = ROOT / "research" / "process" / "forward-pit-capture-status.json"
SPEC = importlib.util.spec_from_file_location("forward_pit_capture_test", PATH)
assert SPEC and SPEC.loader
collector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = collector
SPEC.loader.exec_module(collector)


def _response(value: object, status: int = 200):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return collector.HttpResponse(status, raw, None)


def test_capture_is_append_only_and_preserves_raw_bytes(tmp_path: Path) -> None:
    exchange = {
        "serverTime": 1700000000000,
        "symbols": [
            {"symbol": "AAAUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "BBBUSD", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USD"},
            {"symbol": "CCCUSDT", "status": "BREAK", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
        ],
    }
    calls: list[str] = []

    def fake_request(url: str, timeout: float):
        calls.append(url)
        if url == collector.EXCHANGE_INFO_URL:
            return _response(exchange)
        if url == collector.PRICE_URL:
            return _response([{"symbol": "AAAUSDT", "price": "1"}])
        if url == collector.PREMIUM_URL:
            return _response([
                {
                    "symbol": "AAAUSDT",
                    "time": 1700000000100,
                    "nextFundingTime": 1700003600000,
                    "lastFundingRate": "0.1",
                }
            ])
        if "openInterest" in url:
            return _response({"symbol": "AAAUSDT", "time": 1700000000200, "openInterest": "2"})
        raise AssertionError(url)

    out = tmp_path / "capture"
    first = collector.capture_once(out, max_symbols=1, oi_delay_seconds=0, request=fake_request, sleep=lambda _: None)
    second = collector.capture_once(out, max_symbols=1, oi_delay_seconds=0, request=fake_request, sleep=lambda _: None)
    assert first["records_appended"] == 4
    assert second["records_appended"] == 4
    assert first["open_interest_symbol_offset"] == 0
    assert first["open_interest_symbols_requested"] == ["AAAUSDT"]
    assert calls.count(collector.EXCHANGE_INFO_URL) == 2

    files = list(out.glob("forward-pit-*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert len(rows) == 8
    assert [row["event_type"] for row in rows[:4]] == ["instrument_status", "price", "funding_premium", "open_interest"]
    instrument = rows[0]
    raw = base64.b64decode(instrument["raw_payload_base64"])
    assert raw == json.dumps(exchange, sort_keys=True, separators=(",", ":")).encode()
    assert instrument["raw_payload_sha256"] == hashlib.sha256(raw).hexdigest()
    assert instrument["source_event_timestamps_utc_ms"] == [1700000000000]
    assert rows[2]["source_event_timestamps_utc_ms"] == [1700000000100]
    assert rows[2]["source_scheduled_timestamps_utc_ms"] == [1700003600000]
    assert rows[3]["source_event_timestamps_utc_ms"] == [1700000000200]
    assert all(row["source_scheduled_timestamps_utc_ms"] == [] for row in (rows[0], rows[1], rows[3]))
    assert all(row["local_arrival_utc"].endswith("Z") for row in rows)


def test_capture_records_endpoint_error_without_hiding_it(tmp_path: Path) -> None:
    def fake_request(url: str, timeout: float):
        if url == collector.EXCHANGE_INFO_URL:
            return collector.HttpResponse(None, None, "request_error:URLError")
        return _response([])

    result = collector.capture_once(tmp_path / "capture", oi_delay_seconds=0, request=fake_request, sleep=lambda _: None)
    assert result["records_appended"] == 3
    rows = [json.loads(line) for path in (tmp_path / "capture").glob("*.jsonl") for line in path.read_text().splitlines()]
    assert rows[0]["event_type"] == "instrument_status"
    assert rows[0]["error_status"] == "request_error:URLError"
    assert rows[0]["raw_payload_base64"] is None
    assert rows[0]["raw_payload_sha256"] is None
    assert not [row for row in rows if row["event_type"] == "open_interest"]


def test_symbol_offset_rotates_bounded_open_interest_capture(tmp_path: Path) -> None:
    exchange = {
        "symbols": [
            {"symbol": symbol, "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"}
            for symbol in ("AAAUSDT", "BBBUSDT", "CCCUSDT")
        ]
    }
    calls: list[str] = []

    def fake_request(url: str, timeout: float):
        calls.append(url)
        return _response(exchange if url == collector.EXCHANGE_INFO_URL else [])

    result = collector.capture_once(
        tmp_path / "capture",
        max_symbols=2,
        symbol_offset=2,
        oi_delay_seconds=0,
        request=fake_request,
        sleep=lambda _: None,
    )
    assert result["open_interest_symbols_requested"] == ["CCCUSDT", "AAAUSDT"]
    oi_calls = [url for url in calls if "openInterest" in url]
    assert oi_calls[0].endswith("symbol=CCCUSDT")
    assert oi_calls[1].endswith("symbol=AAAUSDT")


def test_collector_has_no_runtime_or_credential_surface() -> None:
    text = PATH.read_text()
    for forbidden in ("api_key", "api_secret", "listenKey", "/fapi/v1/order", "websocket"):
        assert forbidden.lower() not in text.lower()
    assert "DEFAULT_OUT_DIR" in text
    assert "gmaq-forward-pit-capture" in text


def test_capture_status_binds_the_deployed_source_without_alpha_promotion() -> None:
    status = json.loads(STATUS_PATH.read_text())
    digest = hashlib.sha256(PATH.read_bytes()).hexdigest()
    assert status["tracked_source_sha256"] == digest
    assert status["installed_copy_sha256"] == digest
    assert status["credentials_used"] is False
    assert status["account_endpoints_used"] is False
    assert status["orders_submitted"] is False
    assert status["alpha_prerequisite"] is False
    assert status["background_first_cycle"]["verdict"] == "CAPTURED_RESEARCH_ONLY"
    assert status["background_first_cycle"]["records_appended"] == 23
    assert status["research_tier_effect"].startswith("Prospective raw evidence only")
