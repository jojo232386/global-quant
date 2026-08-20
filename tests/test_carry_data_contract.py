import hashlib
import json
import pathlib

import pytest

from gmaq_data import DataLayerError, migrate_carry, verify_snapshot
from gmaq_data.layer import canonical_json_bytes, sha256_file


ROOT = pathlib.Path(__file__).resolve().parents[1]
START_MS = 1_704_067_200_000
STEP = 28_800_000
END_MS = START_MS + 3 * STEP
STUDY_ID = "study-2026-08-20-btceth-spot-perp-carry"


def write_jsonl(path: pathlib.Path, rows: list[dict]) -> str:
    payload = b"".join(canonical_json_bytes(row) for row in rows)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def build_source(path: pathlib.Path) -> pathlib.Path:
    path.mkdir()
    symbols = {}
    for symbol, base in (("BTCUSDT", 40_000.0), ("ETHUSDT", 2_000.0)):
        spot, marks, funding = [], [], []
        for offset, timestamp in enumerate(range(START_MS, END_MS, STEP)):
            opened = base + offset
            spot.append({
                "open_time_utc_ms": timestamp, "open": str(opened),
                "high": str(opened + 2), "low": str(opened - 2),
                "close": str(opened + 1), "volume": "100",
            })
            marks.append({
                "open_time_utc_ms": timestamp, "open": str(opened),
                "high": str(opened + 2), "low": str(opened - 2),
                "close": str(opened + 1),
            })
            funding.append({
                "symbol": symbol, "fundingTime": timestamp + 4,
                "fundingRate": "0.0001", "markPrice": str(opened),
                "markPriceSource": "fapi_8h_mark_kline_open_fallback",
            })
        names = {
            "spot": f"{symbol}-spot-8h.jsonl", "mark": f"{symbol}-mark-8h.jsonl",
            "funding": f"{symbol}-funding.jsonl",
        }
        hashes = {
            "spot": write_jsonl(path / names["spot"], spot),
            "mark": write_jsonl(path / names["mark"], marks),
            "funding": write_jsonl(path / names["funding"], funding),
        }
        symbols[symbol] = {
            "spot_8h_path": names["spot"], "spot_8h_sha256": hashes["spot"],
            "spot_8h_bars": 3, "first_spot_utc_ms": START_MS, "last_spot_utc_ms": END_MS - STEP,
            "mark_8h_path": names["mark"], "mark_8h_sha256": hashes["mark"], "mark_8h_bars": 3,
            "funding_path": names["funding"], "funding_sha256": hashes["funding"],
            "funding_records": 3, "first_funding_utc_ms": START_MS + 4,
            "last_funding_utc_ms": END_MS - STEP + 4,
            "funding_mark_price_fallback_records": 3,
            "funding_request_window_complete": True,
        }
    prereg = ROOT / "research" / "backtests" / STUDY_ID / "preregistration.md"
    manifest = {
        "schema_version": 1, "study_id": STUDY_ID,
        "fetched_at_utc": "2024-01-02T00:01:00Z",
        "source_spot_klines": "https://api.binance.com/api/v3/klines",
        "source_mark_klines": "https://fapi.binance.com/fapi/v1/markPriceKlines",
        "source_funding": "https://fapi.binance.com/fapi/v1/fundingRate",
        "start_inclusive": "2024-01-01", "end_exclusive": "2024-01-02",
        "interval": "8h", "symbols": symbols,
        "preregistration_sha256": sha256_file(prereg),
        "credential_scope": "public_endpoints_only",
    }
    unhashed = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    manifest["manifest_payload_sha256"] = hashlib.sha256(unhashed.encode()).hexdigest()
    (path / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return path


def test_carry_migration_replays_to_same_verified_curated_id(tmp_path: pathlib.Path) -> None:
    source = build_source(tmp_path / "source")
    warehouse = tmp_path / "warehouse"
    first = migrate_carry(source_dir=source, data_root=warehouse, repo_root=ROOT)
    second = migrate_carry(source_dir=source, data_root=warehouse, repo_root=ROOT)
    assert first == second
    record = verify_snapshot(
        warehouse, first["curated_snapshot_id"],
        expected_dataset="btceth-spot-perp-carry", minimum_stage="curated",
    )
    assert record["integrity_verdict"] == "VERIFIED"
    assert record["quality_verdict"] == "PASS"


def test_carry_bad_source_quarantines_fail_closed(tmp_path: pathlib.Path) -> None:
    source = build_source(tmp_path / "source")
    target = source / "BTCUSDT-spot-8h.jsonl"
    target.write_bytes(target.read_bytes() + b"{}\n")
    with pytest.raises(DataLayerError, match="checksum mismatch"):
        migrate_carry(source_dir=source, data_root=tmp_path / "warehouse", repo_root=ROOT)


def test_carry_manifest_traversal_is_rejected(tmp_path: pathlib.Path) -> None:
    source = build_source(tmp_path / "source")
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["symbols"]["BTCUSDT"]["spot_8h_path"] = "../BTCUSDT-spot-8h.jsonl"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    with pytest.raises(DataLayerError, match="path mismatch"):
        migrate_carry(source_dir=source, data_root=tmp_path / "warehouse", repo_root=ROOT)
