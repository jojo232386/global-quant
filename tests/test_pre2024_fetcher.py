"""Contract tests for the pre-2024 archive fetcher's closing behaviors.

Expectations are hand-derived; the network layer is replaced by an
injectable fake. Pins: cached re-verification against the OFFICIAL
checksum with atomic replacement, 404-vs-retryable HTTP semantics,
canonical manifest last-write-wins, full-month default for fetch, and
the completeness audit verdicts.
"""

from __future__ import annotations

import hashlib
import io
import importlib.util
import json
import pathlib
import sys
import urllib.error
import zipfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "research" / "data" / "fetch_pre2024_archive.py"

sys.path.insert(0, str(MODULE_PATH.parent))
spec = importlib.util.spec_from_file_location("fetch_pre2024_archive", MODULE_PATH)
fx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fx)


def make_candidates(tmp_path: pathlib.Path) -> pathlib.Path:
    p = tmp_path / "pre2024-candidates.json"
    p.write_text(json.dumps({
        "current_included": 1, "archive_only_included": 1,
        "current_symbols_frozen_at_enumeration": [],
        "first_bar_by_symbol": {"AAAUSDT": "2023-01", "BBBUSDT": "2023-03"},
    }))
    return p


def make_server(tmp_path: pathlib.Path) -> tuple[dict, pathlib.Path]:
    """Fake object store: url -> bytes; files land under tmp/site."""
    site = tmp_path / "site"
    site.mkdir()

    def put(key: str, blob: bytes) -> None:
        dest = site / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        dest.with_suffix(dest.suffix + ".CHECKSUM").write_text(
            hashlib.sha256(blob).hexdigest() + "\n")

    def get(url: str) -> bytes:
        key = url.split("/data.binance.vision/", 1)[1]
        path = site / key
        if not path.exists():
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        return path.read_bytes()

    return {"put": put, "get": get}, site


def make_zip(name: str, rows: list[list[str]], *, header: bool = False) -> bytes:
    body_rows = []
    if header:
        body_rows.append([
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "count", "taker_buy_volume",
            "taker_buy_quote_volume", "ignore",
        ])
    body_rows.extend(rows)
    payload = "\n".join(",".join(row) for row in body_rows).encode() + b"\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, payload)
    return buffer.getvalue()


def kline_row(timestamp: int, *, volume: str = "10",
              quote: str = "1000") -> list[str]:
    return [str(timestamp), "100", "110", "90", "105", volume,
            str(timestamp + fx.DAY_MS - 1), quote, "1", "5", "500", "0"]


def test_cached_file_reverified_and_atomically_replaced(tmp_path):
    server, _ = make_server(tmp_path)
    good = b"kline-good"
    server["put"]("data/futures/um/monthly/klines/AAAUSDT/1d/AAAUSDT-1d-2023-01.zip", good)
    out = tmp_path / "raw"
    dest = out / "klines" / "AAAUSDT" / "kline-2023-01.zip"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"stale-corrupted")  # cached but wrong vs official ck

    records = fx.fetch(make_candidates(tmp_path), out, only={"AAAUSDT"},
                       limit=0, months_filter={"2023-01"}, run_id="t1",
                       get=server["get"])
    row = [r for r in records if r["kind"] == "kline"][0]
    assert row["status"] == "replaced"
    assert dest.read_bytes() == good
    assert not list(dest.parent.glob("*.tmp"))  # no partial leftovers


def test_404_missing_but_persistent_5xx_fails_the_run(tmp_path):
    server, _ = make_server(tmp_path)
    # no kline objects at all: the FIRST checksum GET raises 404 -> missing.
    # then make the funding checksum raise 503 forever
    real_get = server["get"]

    def flaky(url: str) -> bytes:
        if "AAAUSDT-fundingRate-2023-02" in url:
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", None, None)
        return real_get(url)

    server["put"]("data/futures/um/monthly/klines/AAAUSDT/1d/AAAUSDT-1d-2023-02.zip", b"k")
    out = tmp_path / "raw"
    with pytest.raises(RuntimeError, match="fetch run FAIL"):
        fx.fetch(make_candidates(tmp_path), out, only={"AAAUSDT"}, limit=0,
                 months_filter={"2023-02"}, run_id="t2", get=flaky)
    # but a plain 404-only symbol records missing without failing
    out2 = tmp_path / "raw2"
    recs = fx.fetch(make_candidates(tmp_path), out2, only={"BBBUSDT"}, limit=0,
                    months_filter={"2023-03"}, run_id="t3", get=real_get)
    assert all(r["status"] == "missing" for r in recs)


def test_canonical_manifest_last_write_wins(tmp_path):
    server, _ = make_server(tmp_path)
    blob = b"k"
    server["put"]("data/futures/um/monthly/klines/AAAUSDT/1d/AAAUSDT-1d-2023-01.zip", blob)
    out = tmp_path / "raw"
    fx.fetch(make_candidates(tmp_path), out, only={"AAAUSDT"}, limit=0,
             months_filter={"2023-01"}, run_id="first", get=server["get"])
    # second run (cached) must override the entry with the SAME key
    fx.fetch(make_candidates(tmp_path), out, only={"AAAUSDT"}, limit=0,
             months_filter={"2023-01"}, run_id="second", get=server["get"])
    doc = json.loads((out / "canonical-manifest.json").read_text())
    keys = {(e["symbol"], e["month"], e["kind"]) for e in doc["entries"]}
    assert keys == {("AAAUSDT", "2023-01", "kline"), ("AAAUSDT", "2023-01", "funding")}
    # run log has history, canonical has exactly one entry per key
    log_lines = (out / "fetch-manifest.jsonl").read_text().splitlines()
    assert len(log_lines) > len(doc["entries"])


def test_fetch_without_month_filter_covers_full_range(tmp_path):
    server, _ = make_server(tmp_path)
    for m in ("2023-01", "2023-02", "2023-03"):
        server["put"](f"data/futures/um/monthly/klines/AAAUSDT/1d/AAAUSDT-1d-{m}.zip", b"k")
    out = tmp_path / "raw"
    records = fx.fetch(make_candidates(tmp_path), out, only={"AAAUSDT"},
                       limit=0, months_filter=None, run_id="t4",
                       get=server["get"])
    kline_months = {r["month"] for r in records
                    if r["kind"] == "kline" and r["status"] != "missing"}
    assert kline_months == {"2023-01", "2023-02", "2023-03"}


def test_checksum_mismatch_fails_fetch_immediately(tmp_path):
    server, _ = make_server(tmp_path)
    # corrupt the OFFICIAL checksum for the funding object
    key = "data/futures/um/monthly/fundingRate/AAAUSDT/AAAUSDT-fundingRate-2023-01.zip"
    server["put"](key, b"payload")
    # rewrite the stored CHECKSUM to a wrong digest
    site = tmp_path / "site"
    (site / (key + ".CHECKSUM")).write_text("0" * 64 + "\n")
    server["put"]("data/futures/um/monthly/klines/AAAUSDT/1d/AAAUSDT-1d-2023-01.zip", b"k")
    out = tmp_path / "raw"
    with pytest.raises(RuntimeError, match="official checksum"):
        fx.fetch(make_candidates(tmp_path), out, only={"AAAUSDT"}, limit=0,
                 months_filter={"2023-01"}, run_id="t-cm", get=server["get"])


def test_smoke_gate_fails_on_quote_volume_invariant_violation():
    base = {"months": {"2023-01": {"sha256_ok": True}},
            "cross_check": {"overlap_days": 30, "decimal_mismatches": 0,
                            "quote_volume_invariant_violations": 0}}
    assert fx.smoke_gate_ok(base) is True
    bad = {"months": {"2023-01": {"sha256_ok": True}},
           "cross_check": {"overlap_days": 30, "decimal_mismatches": 0,
                           "quote_volume_invariant_violations": 2}}
    assert fx.smoke_gate_ok(bad) is False


def test_repair_mode_discovers_and_binds_daily_evidence(tmp_path):
    server, _ = make_server(tmp_path)
    out = tmp_path / "raw"
    out.mkdir()
    symbol = "AAAUSDT"
    day0 = 1_704_067_200_000
    day1 = day0 + fx.DAY_MS
    day2 = day1 + fx.DAY_MS

    # Monthly source has one missing day and a corrupt base-volume field on
    # day0; its OHLC and quote volume remain the values to validate.
    monthly = make_zip(
        f"{symbol}-1d-2024-01.zip",
        [kline_row(day0, volume="1", quote="144000"), kline_row(day2)],
        header=True,
    )
    monthly_path = out / "klines" / symbol / "kline-2024-01.zip"
    monthly_path.parent.mkdir(parents=True)
    monthly_path.write_bytes(monthly)
    out.joinpath("canonical-manifest.json").write_text(json.dumps({
        "run_id": "acquisition-attempt-001",
        "entries": [{
            "symbol": symbol, "month": "2024-01", "kind": "kline",
            "status": "cached", "path": str(monthly_path),
            "sha256": hashlib.sha256(monthly).hexdigest(),
        }],
    }))

    missing_key = (
        f"{fx.DAILY_KLINE_PREFIX}{symbol}/1d/{symbol}-1d-2024-01-02.zip")
    server["put"](missing_key, make_zip(
        f"{symbol}-1d-2024-01-02.csv", [kline_row(day1)], header=True))
    minute_rows = []
    for offset in range(1440):
        timestamp = day0 + offset * fx.MINUTE_MS
        minute_rows.append([
            str(timestamp), "100", "110", "90", "105", "1",
            str(timestamp + fx.MINUTE_MS - 1), "100", "1", "0.5", "50", "0",
        ])
    validation_key = (
        f"{fx.DAILY_KLINE_PREFIX}{symbol}/1m/{symbol}-1m-2024-01-01.zip")
    server["put"](validation_key, make_zip(
        f"{symbol}-1m-2024-01-01.csv", minute_rows, header=True))

    manifest = fx.repair_kline_archive(
        out, run_id="repair-test", get=server["get"])
    assert manifest["monthly_files_mutated"] is False
    assert manifest["curated_1m_fields"] is False
    assert {(entry["kind"], entry["date"]) for entry in manifest["missing_1d"]} == {
        ("1d", "2024-01-02"),
    }
    assert {(entry["kind"], entry["date"])
            for entry in manifest["volume_validation_1m"]} == {
        ("1m", "2024-01-01"),
    }
    all_entries = manifest["missing_1d"] + manifest["volume_validation_1m"]
    assert all(pathlib.Path(entry["path"]).is_file() for entry in all_entries)
    persisted = json.loads((out / "repair-manifest.json").read_text())
    assert persisted["run_id"] == "repair-test"


def test_audit_first_month_and_funding_verdicts(tmp_path):
    out = tmp_path / "raw"
    out.mkdir()
    entries = []
    # AAAUSDT: frozen first 2023-01 but data starts 2023-02 -> leading gap
    for m in ("2023-02", "2023-03"):
        entries.append({"symbol": "AAAUSDT", "month": m, "kind": "kline",
                        "status": "ok"})
    # BBBUSDT: klines continuous from frozen first, funding has mid-gap
    for m in ("2023-03", "2023-04", "2023-05"):
        entries.append({"symbol": "BBBUSDT", "month": m, "kind": "kline",
                        "status": "ok"})
    for m in ("2023-03", "2023-05"):
        entries.append({"symbol": "BBBUSDT", "month": m, "kind": "funding",
                        "status": "ok"})
    out.joinpath("canonical-manifest.json").write_text(
        json.dumps({"run_id": "t-a", "entries": entries}))
    report = fx.audit(out, make_candidates(tmp_path))
    assert report["kline_verdict"] == "FAIL"  # AAA leading month missing
    assert any("frozen first month" in p for p in report["kline_problems"])
    assert any("BBBUSDT" in p and "missing funding" in p
               for p in report["funding_problems"])
    assert report["funding_verdict"] == "FAIL"
    assert report["verdict"] == "FAIL"


def test_audit_kline_pass_with_funding_fail_advances_price_cards(tmp_path):
    out = tmp_path / "raw"
    out.mkdir()
    # candidates file with a single symbol so the split verdict is clean
    spec_path = tmp_path / "candidates-only-aaa.json"
    spec_path.write_text(json.dumps({
        "current_symbols_frozen_at_enumeration": [],
        "first_bar_by_symbol": {"AAAUSDT": "2023-01"},
    }))
    entries = []
    # klines: continuous from the frozen first month, post-delisting tail
    for m in ("2023-01", "2023-02", "2023-03"):
        entries.append({"symbol": "AAAUSDT", "month": m, "kind": "kline",
                        "status": "ok"})
    # funding: 2023-02 missing (kline-active) -> FAIL; 2023-04 is extra
    for m in ("2023-01", "2023-03", "2023-04"):
        entries.append({"symbol": "AAAUSDT", "month": m, "kind": "funding",
                        "status": "ok"})
    out.joinpath("canonical-manifest.json").write_text(
        json.dumps({"run_id": "t-split", "entries": entries}))
    report = fx.audit(out, spec_path)
    # the actual price-card advance condition: klines clean, funding not
    assert report["kline_verdict"] == "PASS"
    assert report["funding_verdict"] == "FAIL"
    assert report["verdict"] == "FAIL"
    assert any("missing funding" in p and "2023-02" in p
               for p in report["funding_problems"])
    # the extra funding month is recorded as a warning, not evidence
    assert any("beyond kline lifecycle" in w and "2023-04" in w
               for w in report["warnings"])
    assert report["per_symbol"]["AAAUSDT"]["extra_funding_months"] == ["2023-04"]
