import hashlib
import json
import pathlib
import shutil
import sqlite3

import pytest

from gmaq_data import DataLayerError, status_summary, verify_snapshot
import gmaq_data.layer as layer
from gmaq_data.layer import InputFile, canonical_json_bytes, create_snapshot, load_schema
from gmaq_data.tsmom import migrate_tsmom


ROOT = pathlib.Path(__file__).resolve().parents[1]
DAY_MS = 86_400_000
EIGHT_HOURS_MS = 28_800_000
START_MS = 1_704_067_200_000  # 2024-01-01 UTC
END_MS = START_MS + 2 * DAY_MS


def write_jsonl(path: pathlib.Path, rows: list[dict]) -> str:
    payload = b"".join(canonical_json_bytes(row) for row in rows)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def build_source(path: pathlib.Path) -> pathlib.Path:
    path.mkdir()
    symbols = {}
    for symbol, base in (("BTCUSDT", 40_000.0), ("ETHUSDT", 2_000.0)):
        bars = []
        marks = []
        funding = []
        for offset, timestamp in enumerate(range(START_MS, END_MS, DAY_MS)):
            opened = base + offset * 10
            bars.append(
                {
                    "open_time_utc_ms": timestamp,
                    "open": str(opened),
                    "high": str(opened + 20),
                    "low": str(opened - 20),
                    "close": str(opened + 5),
                    "volume": "100",
                }
            )
        for offset, timestamp in enumerate(range(START_MS, END_MS, EIGHT_HOURS_MS)):
            opened = base + offset
            marks.append(
                {
                    "open_time_utc_ms": timestamp,
                    "open": str(opened),
                    "high": str(opened + 2),
                    "low": str(opened - 2),
                    "close": str(opened + 1),
                }
            )
            funding.append(
                {
                    "symbol": symbol,
                    "fundingTime": timestamp + 4,
                    "fundingRate": "0.0001",
                    "markPrice": str(opened),
                    "markPriceSource": "fapi_8h_mark_kline_open_fallback",
                    "rateType": "Regular",
                }
            )
        kline_name = f"{symbol}-1d.jsonl"
        funding_name = f"{symbol}-funding.jsonl"
        mark_name = f"{symbol}-mark-8h.jsonl"
        kline_sha = write_jsonl(path / kline_name, bars)
        funding_sha = write_jsonl(path / funding_name, funding)
        mark_sha = write_jsonl(path / mark_name, marks)
        symbols[symbol] = {
            "bars": len(bars),
            "end_boundary_open": base + 100,
            "end_boundary_source": "excluded_incomplete_candle_open_only",
            "end_boundary_utc_ms": END_MS,
            "first_bar_utc_ms": START_MS,
            "last_bar_utc_ms": END_MS - DAY_MS,
            "first_funding_utc_ms": START_MS + 4,
            "last_funding_utc_ms": END_MS - EIGHT_HOURS_MS + 4,
            "funding_mark_price_fallback_records": len(funding),
            "funding_path": f"legacy/{funding_name}",
            "funding_records": len(funding),
            "funding_request_window_complete": True,
            "funding_sha256": funding_sha,
            "klines_path": f"legacy/{kline_name}",
            "klines_sha256": kline_sha,
            "mark_8h_bars": len(marks),
            "mark_8h_path": f"legacy/{mark_name}",
            "mark_8h_sha256": mark_sha,
        }
    manifest = {
        "credential_scope": "public_endpoints_only",
        "end_exclusive": "2024-01-03",
        "fetched_at_utc": "2024-01-03T00:01:00Z",
        "interval": "1d",
        "preregistration_sha256": "0" * 64,
        "schema_version": 1,
        "source_funding": "https://fapi.binance.com/fapi/v1/fundingRate",
        "source_klines": "https://fapi.binance.com/fapi/v1/klines",
        "source_mark_klines": "https://fapi.binance.com/fapi/v1/markPriceKlines",
        "start_inclusive": "2024-01-01",
        "study_id": "synthetic-contract-fixture",
        "symbols": symbols,
    }
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    manifest["manifest_payload_sha256"] = hashlib.sha256(payload.encode()).hexdigest()
    (path / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return path


def test_schema_identity_is_stable_across_key_order() -> None:
    schema, schema_id = load_schema(ROOT, "gmaq-tsmom-public-v1.json")
    reordered = dict(reversed(list(schema.items())))
    assert hashlib.sha256(canonical_json_bytes(reordered)).hexdigest() == schema_id


def test_migration_is_immutable_verified_and_deterministic(tmp_path: pathlib.Path) -> None:
    source = build_source(tmp_path / "source")
    warehouse = tmp_path / "warehouse"
    first = migrate_tsmom(source_dir=source, data_root=warehouse, repo_root=ROOT)
    second = migrate_tsmom(source_dir=source, data_root=warehouse, repo_root=ROOT)
    assert first["raw_snapshot_id"] == second["raw_snapshot_id"]
    assert first["validated_snapshot_id"] == second["validated_snapshot_id"]
    assert first["curated_snapshot_id"] == second["curated_snapshot_id"]
    verified = verify_snapshot(
        warehouse,
        first["curated_snapshot_id"],
        expected_dataset="btceth-weekly-tsmom",
        minimum_stage="curated",
    )
    assert verified["integrity_verdict"] == "VERIFIED"
    assert verified["quality_verdict"] == "PASS"
    assert pathlib.Path(verified["artifact_path"]).is_relative_to(warehouse)
    summary = status_summary(warehouse)
    assert summary["verdict"] == "PASS"
    assert summary["curated_available"] is True


def test_registry_and_snapshot_files_reject_mutation(tmp_path: pathlib.Path) -> None:
    source = build_source(tmp_path / "source")
    warehouse = tmp_path / "warehouse"
    result = migrate_tsmom(source_dir=source, data_root=warehouse, repo_root=ROOT)
    connection = sqlite3.connect(warehouse / "registry.sqlite")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE snapshots SET quality_verdict='FAIL' WHERE snapshot_id=?",
            (result["curated_snapshot_id"],),
        )
    connection.close()

    record = verify_snapshot(warehouse, result["curated_snapshot_id"])
    data_file = next(
        pathlib.Path(record["artifact_path"]) / item["relpath"]
        for item in record["files"]
        if item["relpath"].endswith(".jsonl")
    )
    data_file.chmod(0o644)
    data_file.write_bytes(data_file.read_bytes() + b"{}\n")
    with pytest.raises(DataLayerError, match="checksum mismatch"):
        verify_snapshot(warehouse, result["curated_snapshot_id"])
    assert status_summary(warehouse)["verdict"] == "FAIL"


def test_bad_source_is_quarantined_and_cannot_advance(tmp_path: pathlib.Path) -> None:
    source = build_source(tmp_path / "source")
    target = source / "BTCUSDT-1d.jsonl"
    target.write_bytes(target.read_bytes() + canonical_json_bytes({"open_time_utc_ms": END_MS}))
    warehouse = tmp_path / "warehouse"
    with pytest.raises(DataLayerError, match="checksum mismatch"):
        migrate_tsmom(source_dir=source, data_root=warehouse, repo_root=ROOT)
    summary = status_summary(warehouse)
    assert summary["quarantine_count"] == 1
    assert summary["curated_available"] is False


def test_source_manifest_path_traversal_is_rejected(tmp_path: pathlib.Path) -> None:
    source = build_source(tmp_path / "source")
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["symbols"]["BTCUSDT"]["klines_path"] = "../BTCUSDT-1d.jsonl"
    manifest.pop("manifest_payload_sha256")
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    manifest["manifest_payload_sha256"] = hashlib.sha256(payload.encode()).hexdigest()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    with pytest.raises(DataLayerError, match="path mismatch"):
        migrate_tsmom(source_dir=source, data_root=tmp_path / "warehouse", repo_root=ROOT)


def test_empty_snapshot_role_is_rejected_without_creating_a_registry(
    tmp_path: pathlib.Path,
) -> None:
    warehouse = tmp_path / "warehouse"
    with pytest.raises(DataLayerError, match="non-empty roles"):
        create_snapshot(
            data_root=warehouse,
            dataset="contract-test",
            stage="raw",
            schema_id="0" * 64,
            files=[InputFile("", "payload.json", payload=b"{}\n")],
            source_metadata={},
            checks={},
            quality_verdict="UNASSESSED",
            cross_source_verdict="UNVERIFIED_SINGLE_VENUE",
        )
    assert not warehouse.exists()


def test_symlink_source_manifest_is_rejected_before_read(
    tmp_path: pathlib.Path,
) -> None:
    source = build_source(tmp_path / "source")
    manifest = source / "manifest.json"
    real_manifest = source / "real-manifest.json"
    manifest.rename(real_manifest)
    manifest.symlink_to(real_manifest.name)
    with pytest.raises(DataLayerError, match="non-symlink"):
        migrate_tsmom(source_dir=source, data_root=tmp_path / "warehouse", repo_root=ROOT)


def test_registry_failure_removes_only_the_unregistered_artifact(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warehouse = tmp_path / "warehouse"
    original_connect = layer._connect
    calls = 0

    def flaky_connect(data_root: pathlib.Path, *, read_only: bool = False):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise sqlite3.OperationalError("injected registry failure")
        return original_connect(data_root, read_only=read_only)

    monkeypatch.setattr(layer, "_connect", flaky_connect)
    with pytest.raises(DataLayerError, match="registry commit failed"):
        create_snapshot(
            data_root=warehouse,
            dataset="contract-test",
            stage="raw",
            schema_id="0" * 64,
            files=[InputFile("payload", "payload.json", payload=b"{}\n")],
            source_metadata={},
            checks={},
            quality_verdict="UNASSESSED",
            cross_source_verdict="UNVERIFIED_SINGLE_VENUE",
        )
    assert not any((warehouse / "snapshots").rglob("snapshot.manifest.json"))


def test_replay_recovers_an_artifact_left_before_registry_commit(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    warehouse = tmp_path / "warehouse"
    original_connect = layer._connect
    calls = 0

    def interrupted_connect(data_root: pathlib.Path, *, read_only: bool = False):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise KeyboardInterrupt
        return original_connect(data_root, read_only=read_only)

    arguments = {
        "data_root": warehouse,
        "dataset": "contract-test",
        "stage": "raw",
        "schema_id": "0" * 64,
        "files": [InputFile("payload", "payload.json", payload=b"{}\n")],
        "source_metadata": {},
        "checks": {},
        "quality_verdict": "UNASSESSED",
        "cross_source_verdict": "UNVERIFIED_SINGLE_VENUE",
    }
    monkeypatch.setattr(layer, "_connect", interrupted_connect)
    with pytest.raises(KeyboardInterrupt):
        create_snapshot(**arguments)
    assert any((warehouse / "snapshots").rglob("snapshot.manifest.json"))

    monkeypatch.setattr(layer, "_connect", original_connect)
    snapshot_id = create_snapshot(**arguments)
    assert verify_snapshot(warehouse, snapshot_id)["integrity_verdict"] == "VERIFIED"


def test_symlinked_snapshot_file_cannot_verify(tmp_path: pathlib.Path) -> None:
    source = build_source(tmp_path / "source")
    warehouse = tmp_path / "warehouse"
    result = migrate_tsmom(source_dir=source, data_root=warehouse, repo_root=ROOT)
    record = verify_snapshot(warehouse, result["curated_snapshot_id"])
    artifact = pathlib.Path(record["artifact_path"])
    data_file = artifact / record["files"][0]["relpath"]
    same_bytes = tmp_path / "same-bytes.jsonl"
    shutil.copyfile(data_file, same_bytes)
    (artifact / "data").chmod(0o755)
    data_file.unlink()
    data_file.symlink_to(same_bytes)
    with pytest.raises(DataLayerError, match="escapes|missing or unsafe"):
        verify_snapshot(warehouse, result["curated_snapshot_id"])
