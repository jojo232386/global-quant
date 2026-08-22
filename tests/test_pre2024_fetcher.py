"""Contract tests for the pre-2024 archive fetcher's closing behaviors.

Expectations are hand-derived; the network layer is replaced by an
injectable fake. Pins: cached re-verification against the OFFICIAL
checksum with atomic replacement, 404-vs-retryable HTTP semantics,
canonical manifest last-write-wins, full-month default for fetch, and
the completeness audit verdicts.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import sys
import urllib.error

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


def test_audit_verdicts(tmp_path):
    out = tmp_path / "raw"
    out.mkdir()
    entries = []
    # AAAUSDT: continuous 2023-01..03, ends early, NOT current -> post-delisting OK
    for m in ("2023-01", "2023-02", "2023-03"):
        entries.append({"symbol": "AAAUSDT", "month": m, "kind": "kline",
                        "status": "ok"})
    # BBBUSDT: gap in the middle -> FAIL
    for m in ("2023-03", "2023-05"):
        entries.append({"symbol": "BBBUSDT", "month": m, "kind": "kline",
                        "status": "ok"})
    out.joinpath("canonical-manifest.json").write_text(
        json.dumps({"run_id": "t5", "entries": entries}))
    report = fx.audit(out, make_candidates(tmp_path),
                      current_symbols=set())  # nothing currently trading
    assert report["verdict"] == "FAIL"
    assert any("BBBUSDT" in p and "mid-lifecycle" in p for p in report["problems"])
    # tail rule: currently trading symbol ending early must FAIL
    entries.extend({"symbol": "AAAUSDT", "month": m, "kind": "kline",
                    "status": "ok"} for m in ())
    report2 = fx.audit(out, make_candidates(tmp_path),
                       current_symbols={"AAAUSDT"})
    assert any("missing tail" in p for p in report2["problems"])
