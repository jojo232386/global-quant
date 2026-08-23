"""Hand-derived contracts for the fail-closed pre-2024 price V1."""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import zipfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gmaq_data import DataLayerError, verify_snapshot
from gmaq_data import pre2024


DAY = 86_400_000
MINUTE = 60_000
JAN_2023 = 1_672_531_200_000


def row(ms: int, *, price: str = "100", volume: str = "100000", quote: str = "10000000", interval: int = DAY) -> list[str]:
    opened = pre2024.Decimal(price)
    return [
        str(ms), price, str(opened + 1), str(opened - 1), str(opened + pre2024.Decimal("0.5")), volume,
        str(ms + interval - 1), quote, "1", "5", "5", "0",
    ]


def _write_zip(path: pathlib.Path, rows: list[list[str]], *, extra_member: bool = False) -> str:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(path.with_suffix(".csv").name, "\n".join(",".join(item) for item in rows) + "\n")
        if extra_member:
            archive.writestr("unexpected.csv", "x\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reference_rows(rows: list[list[str]]) -> str:
    return "".join(
        json.dumps({"open_time_utc_ms": int(r[0]), "open": r[1], "high": r[2], "low": r[3], "close": r[4]}) + "\n"
        for r in rows
    )


def _fixture_archive(
    tmp_path: pathlib.Path, *, gap: bool = False, extra_member: bool = False, repairs: bool = False
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    raw, reference, candidates = tmp_path / "raw", tmp_path / "reference", tmp_path / "candidates.json"
    raw.mkdir(); reference.mkdir()
    entries, repair_missing, repair_volume = [], [], []
    for symbol, bump in (("BTCUSDT", "0"), ("ETHUSDT", "100")):
        price = pre2024.Decimal("100") + pre2024.Decimal(bump)
        full = [row(JAN_2023 + offset * DAY, price=str(price), quote=str(price * pre2024.Decimal("100000"))) for offset in range(90)]
        if repairs and symbol == "BTCUSDT":
            full[0] = row(JAN_2023, price="100", volume="1", quote="144000")
        source_rows = list(full)
        if gap and symbol == "ETHUSDT":
            source_rows.pop(45)
        if repairs and symbol == "ETHUSDT":
            missing = source_rows.pop(45)
            repair_path = raw / "repairs" / "ETHUSDT-2023-02-15-1d.zip"
            repair_path.parent.mkdir(exist_ok=True)
            repair_missing.append({"symbol": symbol, "date": "2023-02-15", "kind": "1d", "path": str(repair_path), "sha256": _write_zip(repair_path, [missing])})
        for month in ("2023-01", "2023-02", "2023-03"):
            selected = [item for item in source_rows if pre2024._month_for_ms(int(item[0])) == month]
            path = raw / f"{symbol}-{month}.zip"
            entries.append({
                "key": f"data/{symbol}/1d/{symbol}-1d-{month}.zip", "kind": "kline", "month": month,
                "path": str(path), "run_id": pre2024.EXPECTED_RUN_ID, "sha256": _write_zip(path, selected, extra_member=extra_member and symbol == "BTCUSDT" and month == "2023-01"),
                "status": "cached", "symbol": symbol,
            })
        (reference / f"{symbol}-1d.jsonl").write_text(_reference_rows(full))
    if repairs:
        minute_rows = [row(JAN_2023 + offset * MINUTE, price="100", volume="1", quote="100", interval=MINUTE) for offset in range(1440)]
        minute_path = raw / "repairs" / "BTCUSDT-2023-01-01-1m.zip"
        repair_volume.append({"symbol": "BTCUSDT", "date": "2023-01-01", "kind": "1m", "path": str(minute_path), "sha256": _write_zip(minute_path, minute_rows)})
        (raw / "repair-manifest.json").write_text(json.dumps({
            "run_id": pre2024.EXPECTED_REPAIR_RUN_ID,
            "monthly_files_mutated": False,
            "curated_1m_fields": False,
            "missing_1d": repair_missing,
            "volume_validation_1m": repair_volume,
        }, sort_keys=True))
    (raw / "canonical-manifest.json").write_text(json.dumps({"run_id": pre2024.EXPECTED_RUN_ID, "entries": entries}, sort_keys=True))
    (raw / "audit-report.json").write_text(json.dumps({"canonical_run_id": pre2024.EXPECTED_RUN_ID, "kline_verdict": "PASS", "symbols_audited": 2}, sort_keys=True))
    candidates.write_text(json.dumps({"first_bar_by_symbol": {"BTCUSDT": "2023-01", "ETHUSDT": "2023-01"}}, sort_keys=True))
    return raw, candidates, reference


def test_validate_symbol_clean_and_failure_modes() -> None:
    rows = [row(JAN_2023), row(JAN_2023 + DAY), row(JAN_2023 + 2 * DAY)]
    clean, problems = pre2024.validate_symbol(rows)
    assert not problems and len(clean) == 3 and clean[0]["quote_volume"] == "10000000"
    assert any("duplicate" in item for item in pre2024.validate_symbol([row(JAN_2023), row(JAN_2023)])[1])
    assert any("gap" in item for item in pre2024.validate_symbol([row(JAN_2023), row(JAN_2023 + 2 * DAY)])[1])
    broken = row(JAN_2023); broken[2] = "99"
    assert any("OHLC" in item for item in pre2024.validate_symbol([broken])[1])
    broken = row(JAN_2023); broken[7] = "999999999"
    assert any("quote-volume" in item for item in pre2024.validate_symbol([broken])[1])


def test_month_boundaries_and_pit_are_completed_then_next_month_effective() -> None:
    assert pre2024.month_ends(0, 3 * DAY) == [2_678_400_000]
    high = [{"open_time_utc_ms": JAN_2023 + i * DAY, "quote_volume": "10000000"} for i in range(90)]
    low = [{"open_time_utc_ms": JAN_2023 + i * DAY, "quote_volume": "100000"} for i in range(90)]
    universe = pre2024.build_pit_universe({"AAAUSDT": high, "BBBUSDT": low}, JAN_2023, JAN_2023 + 89 * DAY)
    april = next(item for item in universe if item["effective_month_start_utc_ms"] == JAN_2023 + 90 * DAY)
    assert april == {"effective_month_start_utc_ms": JAN_2023 + 90 * DAY, "completed_bars": 90, "symbols": ["AAAUSDT"]}

    # A stale/delisted series must not remain eligible forever merely because
    # its last historical 90 observations passed the floor.
    may_start = JAN_2023 + 120 * DAY
    stale = pre2024.build_pit_universe(
        {"AAAUSDT": high}, JAN_2023, may_start)
    may = next(item for item in stale if item["effective_month_start_utc_ms"] == may_start)
    assert may["symbols"] == []


def test_reference_requires_equal_timestamp_sets_over_full_overlap(tmp_path: pathlib.Path) -> None:
    raw, _, reference = _fixture_archive(tmp_path)
    canonical = json.loads((raw / "canonical-manifest.json").read_text())
    validated = {symbol: pre2024.validate_symbol(rows)[0] for symbol, rows in pre2024.load_symbol_rows(raw, canonical).items()}
    path = reference / "BTCUSDT-1d.jsonl"
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:45] + lines[46:]) + "\n")
    with pytest.raises(DataLayerError, match="timestamp set differs"):
        pre2024.cross_check_reference(validated, reference_dir=reference)


def test_last_pre2024_day_accepts_all_1440_utc_minutes(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "december-1m.zip"
    rows = [row(pre2024.LAST_ALLOWED_OPEN_MS + offset * MINUTE, volume="1", quote="100", interval=MINUTE) for offset in range(1440)]
    _write_zip(path, rows)
    assert len(pre2024._read_zip_csv(path, interval_ms=MINUTE, expected_month="2023-12")) == 1440


def test_builder_is_deterministic_and_binds_archive_extended_labels(tmp_path: pathlib.Path) -> None:
    raw, candidates, reference = _fixture_archive(tmp_path)
    warehouse = tmp_path / "warehouse"
    first = pre2024.build_pre2024_v1(data_root=warehouse, raw_dir=raw, candidates_path=candidates, reference_dir=reference)
    second = pre2024.build_pre2024_v1(data_root=warehouse, raw_dir=raw, candidates_path=candidates, reference_dir=reference)
    assert first["curated_id"] == second["curated_id"] and first["summary"]["dataset"] == "pre2024-usdm-archive-extended-1d"
    record = verify_snapshot(warehouse, first["curated_id"], expected_dataset=pre2024.DATASET, minimum_stage="curated")
    assert record["integrity_verdict"] == "VERIFIED" and "exploration-only" in first["summary"]["labels"]
    raw_record = verify_snapshot(warehouse, first["raw_id"], expected_dataset=pre2024.DATASET)
    assert {"canonical_manifest", "audit_report", "candidates", "source_index"} <= {item["role"] for item in raw_record["files"]}


def test_repair_evidence_is_bound_and_minute_rows_never_curated(tmp_path: pathlib.Path) -> None:
    raw, candidates, reference = _fixture_archive(tmp_path, repairs=True)
    result = pre2024.build_pre2024_v1(data_root=tmp_path / "warehouse", raw_dir=raw, candidates_path=candidates, reference_dir=reference)
    raw_record = verify_snapshot(tmp_path / "warehouse", result["raw_id"])
    curated_record = verify_snapshot(tmp_path / "warehouse", result["curated_id"], minimum_stage="curated")
    assert "repair_manifest" in {item["role"] for item in raw_record["files"]}
    assert all("1m" not in item["relpath"] for item in curated_record["files"])


@pytest.mark.parametrize("gap,extra_member,match", [(True, False, "quarantine is nonzero"), (False, True, "exactly one CSV")])
def test_bad_archive_fails_before_any_registry_write(tmp_path: pathlib.Path, gap: bool, extra_member: bool, match: str) -> None:
    raw, candidates, reference = _fixture_archive(tmp_path, gap=gap, extra_member=extra_member)
    warehouse = tmp_path / "warehouse"
    with pytest.raises(DataLayerError, match=match):
        pre2024.build_pre2024_v1(data_root=warehouse, raw_dir=raw, candidates_path=candidates, reference_dir=reference)
    assert not warehouse.exists()


def test_jsonl_bytes_and_schema_are_canonical() -> None:
    assert pre2024._jsonl_bytes([{"b": 1, "a": 2}]) == pre2024._jsonl_bytes([{"a": 2, "b": 1}])
    _, schema_id = pre2024.load_schema(ROOT, pre2024.SCHEMA_NAME)
    assert len(schema_id) == 64
