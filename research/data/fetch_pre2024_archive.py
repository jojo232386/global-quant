#!/usr/bin/env python3
"""Archive fetcher for the pre-2024 universe rule (PRE2024_UNIVERSE_RULE_DRAFT).

Modes:
  enumerate  - list per-symbol monthly 1d kline objects on the official
               archive, build the extended candidate set (current TRADING
               PERPETUAL USDT plus archive-only USDT-like symbols whose
               earliest monthly file is <= 2023-06).
  smoke      - download one symbol x a few months: verify archive
               checksums, parse rows, Decimal-compare overlapping bars
               against curated 88d9ff34 (no floats anywhere).
  fetch      - full download for every candidate (NOT run in this commit;
               wall clock 1-3 h).

Security posture: https only, hard allowlisted hosts, resolved IPs must
be public (blocks loopback/private/link-local/metadata ranges), redirects
are refused (no redirect-based bypass), XML parsing rejects DTD/ENTITY
declarations and caps input size. No credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import ipaddress
import json
import socket
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from decimal import Decimal
from pathlib import Path

ARCHIVE_HOST = "s3.ap-northeast-1.amazonaws.com"
ARCHIVE_BASE = f"https://{ARCHIVE_HOST}/data.binance.vision"
INFO_HOST = "fapi.binance.com"
INFO_URL = f"https://{INFO_HOST}/fapi/v1/exchangeInfo"
KLINE_PREFIX = "data/futures/um/monthly/klines/"  # .../{SYMBOL}/{interval}/{SYMBOL}-{interval}-{Y}-{M}.zip
FUNDING_PREFIX = "data/futures/um/monthly/fundingRate/"  # .../{SYMBOL}/{SYMBOL}-fundingRate-{Y}-{M}.zip
TRAIN_END_MONTH = "2023-06"   # first monthly file must be <= this
ALLOWED_HOSTS = {ARCHIVE_HOST, INFO_HOST}
MAX_XML_BYTES = 8 * 1024 * 1024
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise RuntimeError(f"redirect refused: {newurl}")


_OPENER = urllib.request.build_opener(_NoRedirect)


def _assert_public_resolution(host: str) -> None:
    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    # Block loopback / RFC1918 private / link-local / unique-local /
    # unspecified. 198.18.0.0/15 (benchmark range) is the local proxy's
    # fake-IP DNS mode on this machine: traffic egresses via the user's
    # own proxy, and the destination stays pinned by the host allowlist
    # and the no-redirect policy, so it is permitted with that note.
    proxy_fake_ip = ipaddress.ip_network("198.18.0.0/15")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip in proxy_fake_ip:
            continue
        if not ip.is_global or ip.is_link_local or ip.is_unspecified:
            raise RuntimeError(f"blocked resolved address for {host}: {ip}")


RETRYABLE_HTTP = {429, 500, 502, 503, 504}


def http_get(url: str, retries: int = 3) -> bytes:
    import time
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise RuntimeError(f"non-https scheme refused: {parsed.scheme}")
    host = parsed.hostname or ""
    if host not in ALLOWED_HOSTS:
        raise RuntimeError(f"host not allowlisted: {host}")
    _assert_public_resolution(host)
    req = urllib.request.Request(url, headers={"User-Agent": "gmaq-pre2024-fetch/1.0"})
    for attempt in range(retries):
        try:
            with _OPENER.open(req, timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in RETRYABLE_HTTP and attempt < retries - 1:
                wait = 2 ** attempt
                print(f"  http {exc.code}; retry in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise  # 404 and exhausted retries surface to the caller
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt
            print(f"  transient fetch error ({exc}); retry in {wait}s",
                  file=sys.stderr)
            time.sleep(wait)


def parse_xml(body: bytes) -> ET.Element:
    if len(body) > MAX_XML_BYTES:
        raise RuntimeError(f"XML input too large: {len(body)}")
    lowered = body.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise RuntimeError("XML with DTD/ENTITY declarations refused")
    return ET.fromstring(body)


def list_keys(prefix: str) -> list[str]:
    """All object keys under prefix (paginated S3 listing; without a
    delimiter S3 returns no NextMarker, so the last key continues)."""
    keys: list[str] = []
    marker: str | None = None
    while True:
        q = f"delimiter=&prefix={urllib.parse.quote(prefix)}&max-keys=1000"
        if marker:
            q += "&marker=" + urllib.parse.quote(marker)
        root = parse_xml(http_get(f"{ARCHIVE_BASE}?{q}"))
        page = [k for c in root.iter(NS + "Contents")
                if (k := c.findtext(NS + "Key"))]
        keys.extend(page)
        truncated = root.findtext(NS + "IsTruncated") == "true"
        marker = root.findtext(NS + "NextMarker") or (page[-1] if page else None)
        if not truncated or not marker:
            return keys


def list_symbol_dirs(prefix: str) -> set[str]:
    """Immediate child directory names under prefix (delimiter listing)."""
    names: set[str] = set()
    marker: str | None = None
    while True:
        q = f"delimiter=/&prefix={urllib.parse.quote(prefix)}&max-keys=1000"
        if marker:
            q += "&marker=" + urllib.parse.quote(marker)
        root = parse_xml(http_get(f"{ARCHIVE_BASE}?{q}"))
        for cp in root.iter(NS + "CommonPrefixes"):
            for p in cp.iter(NS + "Prefix"):
                names.add((p.text or "").rstrip("/").rsplit("/", 1)[-1])
        truncated = root.findtext(NS + "IsTruncated") == "true"
        marker = root.findtext(NS + "NextMarker")
        if not truncated or not marker:
            return names


def current_perp_usdt() -> set[str]:
    info = json.loads(http_get(INFO_URL))
    return {
        s["symbol"] for s in info["symbols"]
        if s.get("contractType") == "PERPETUAL"
        and s.get("status") == "TRADING"
        and s.get("quoteAsset") == "USDT"
    }


def usdt_like(symbol: str) -> bool:
    """Naming heuristic for archive-only symbols (rule's recorded caveat:
    stock perps ending in USDT pass the name check but are removed by the
    first-bar <= 2023-06 rule since they launched later)."""
    return symbol.endswith("USDT") and "SETTLED" not in symbol


def month_of(key: str) -> str | None:
    # .../klines/BTCUSDT/1d/BTCUSDT-1d-2023-06.zip -> "2023-06".
    # .zip only: the sibling .zip.CHECKSUM files must not parse as months.
    name = key.rsplit("/", 1)[-1]
    if not name.endswith(".zip"):
        return None
    parts = name[:-4].split("-")
    if len(parts) == 4 and parts[1] == "1d" and len(parts[2]) == 4 and parts[2].isdigit():
        return f"{parts[2]}-{parts[3]}"
    return None


def enumerate_candidates() -> dict:
    symbols = list_symbol_dirs(KLINE_PREFIX)
    current = current_perp_usdt()
    print(f"archive symbols: {len(symbols)}; current TRADING PERP USDT: {len(current)}")
    candidates: dict[str, str] = {}
    checked = 0
    for sym in sorted(symbols):
        if sym not in current and not usdt_like(sym):
            continue
        months = [m for m in (month_of(k) for k in list_keys(f"{KLINE_PREFIX}{sym}/1d/")) if m]
        if not months:
            continue
        first = min(months)
        if first <= TRAIN_END_MONTH:
            candidates[sym] = first
        checked += 1
        if checked % 100 == 0:
            print(f"  checked {checked}, candidates so far {len(candidates)}", file=sys.stderr)
    out = {"current_included": len(current & set(candidates)),
           "archive_only_included": len(set(candidates) - current),
           "current_symbols_frozen_at_enumeration": sorted(current),
           "first_bar_by_symbol": candidates}
    Path(__file__).parent.joinpath("pre2024-candidates.json").write_text(
        json.dumps(out, indent=2, sort_keys=True))
    print(f"candidates: {len(candidates)} "
          f"(current {out['current_included']}, archive-only {out['archive_only_included']})")
    return out


def verify_sha256(data: bytes, checksum_line: str) -> bool:
    expected = checksum_line.split()[0].strip()
    return hashlib.sha256(data).hexdigest() == expected


def parse_zip_rows(zip_bytes: bytes) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        name = zf.namelist()[0]
        text = zf.read(name).decode()
    rows = [line.split(",") for line in text.splitlines() if line.strip()]
    if rows and rows[0][0].strip().lower() == "open_time":  # header row
        rows = rows[1:]
    return rows


def smoke(symbol: str, months: list[str], curated_path: Path) -> dict:
    report: dict = {"symbol": symbol, "months": {}, "cross_check": {}}
    all_rows: dict[str, list[str]] = {}  # open_time -> row
    for month in months:
        key = f"{KLINE_PREFIX}{symbol}/1d/{symbol}-1d-{month}.zip"
        blob = http_get(f"{ARCHIVE_BASE}/{key}")
        ck = http_get(f"{ARCHIVE_BASE}/{key}.CHECKSUM").decode().strip()
        ok = verify_sha256(blob, ck)
        rows = parse_zip_rows(blob)
        report["months"][month] = {
            "rows": len(rows), "sha256_ok": ok,
            "columns": len(rows[0]) if rows else 0,
            "first_field": rows[0][:6] if rows else [],
        }
        for r in rows:
            all_rows[r[0]] = r
    # Decimal cross-check against curated 88d9ff34 (string values, no floats).
    # The reference dataset carries OHLC only — quote volume is NOT
    # cross-dataset comparable; it is checked per bar by the range
    # invariant volume*low <= quote <= volume*high.
    curated = {}
    for line in curated_path.read_text().splitlines():
        row = json.loads(line)
        curated[str(row["open_time_utc_ms"])] = row
    overlap = set(all_rows) & set(curated)
    mismatches = []
    invariant_violations = []
    for ts in sorted(overlap):
        a, c = all_rows[ts], curated[ts]
        checks = [(Decimal(a[1]), Decimal(c["open"])),
                  (Decimal(a[2]), Decimal(c["high"])),
                  (Decimal(a[3]), Decimal(c["low"])),
                  (Decimal(a[4]), Decimal(c["close"]))]
        if any(x != y for x, y in checks):
            mismatches.append({"ts": ts, "archive": a[:5],
                               "curated": [c["open"], c["high"], c["low"], c["close"]]})
    for ts, r in all_rows.items():
        vol, low, high, quote = Decimal(r[5]), Decimal(r[3]), Decimal(r[2]), Decimal(r[7])
        if not (vol * low <= quote <= vol * high):
            invariant_violations.append({"ts": ts, "row": r[:8]})
    report["cross_check"] = {
        "fields_compared_cross_dataset": ["open", "high", "low", "close"],
        "quote_volume": "not cross-dataset comparable (reference lacks the "
                        "field); validated by range invariant only",
        "curated_rows": len(curated), "overlap_days": len(overlap),
        "decimal_mismatches": len(mismatches), "examples": mismatches[:3],
        "quote_volume_invariant_violations": len(invariant_violations),
    }
    print(json.dumps(report, indent=2)[:2000])
    return report


def month_range(first: str, last: str = "2023-12") -> list[str]:
    fy, fm = (int(x) for x in first.split("-"))
    ly, lm = (int(x) for x in last.split("-"))
    out = []
    y, m = fy, fm
    while (y, m) <= (ly, lm):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def atomic_write(dest: Path, blob: bytes) -> None:
    """Write via temp file + rename so readers never see partial files."""
    import os
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, dest)


def official_checksum(key: str, get=http_get) -> str:
    return get(f"{ARCHIVE_BASE}/{key}.CHECKSUM").decode().strip().split()[0]


def fetch(candidates_path: Path, out_dir: Path, only: set[str] | None,
          limit: int, months_filter: set[str] | None = None,
          run_id: str = "unassigned", get=http_get) -> list[dict]:
    """Download kline + funding zips into a raw directory.

    - cached files are re-verified against the OFFICIAL .CHECKSUM; a
      mismatch triggers re-download and atomic replacement
    - only HTTP 404 records 'missing'; retryable codes that still fail
      after http_get's retries (and any other HTTP error) FAIL the run
    - the append manifest is a run log only; a canonical manifest keyed
      by (symbol, month, kind), last-write-wins, is written at the end
    """
    spec = json.loads(candidates_path.read_text())
    symbols = sorted(spec["first_bar_by_symbol"])
    if only:
        symbols = [s for s in symbols if s in only]
    if limit:
        symbols = symbols[:limit]
    klines_dir = out_dir / "klines"
    funding_dir = out_dir / "fundingRate"
    manifest_path = out_dir / "fetch-manifest.jsonl"
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    done_syms = 0
    with open(manifest_path, "a", encoding="utf-8") as manifest:
        for sym in symbols:
            first = spec["first_bar_by_symbol"][sym]
            wanted = month_range(first)
            if months_filter:
                wanted = [m for m in wanted if m in months_filter]
            for m in wanted:
                for kind, key, directory in (
                    ("kline", KLINE_PREFIX + f"{sym}/1d/{sym}-1d-{m}.zip", klines_dir),
                    ("funding", FUNDING_PREFIX + f"{sym}/{sym}-fundingRate-{m}.zip", funding_dir),
                ):
                    dest = directory / sym / f"{kind}-{m}.zip"
                    record = {"run_id": run_id, "symbol": sym, "month": m,
                              "kind": kind, "key": key, "path": str(dest)}
                    try:
                        expected = official_checksum(key, get)
                        if dest.exists():
                            local = hashlib.sha256(dest.read_bytes()).hexdigest()
                            if local == expected:
                                record.update(status="cached", sha256=local)
                            else:
                                blob = get(f"{ARCHIVE_BASE}/{key}")
                                if hashlib.sha256(blob).hexdigest() != expected:
                                    record.update(status="checksum_mismatch")
                                    manifest.write(json.dumps(record, sort_keys=True) + "\n")
                                    raise RuntimeError(
                                        f"fetch run FAIL ({sym} {m} {kind}): "
                                        f"re-downloaded content does not match "
                                        f"the official checksum")
                                atomic_write(dest, blob)
                                record.update(status="replaced",
                                              sha256=expected, bytes=len(blob))
                        else:
                            blob = get(f"{ARCHIVE_BASE}/{key}")
                            if hashlib.sha256(blob).hexdigest() != expected:
                                record.update(status="checksum_mismatch")
                                manifest.write(json.dumps(record, sort_keys=True) + "\n")
                                raise RuntimeError(
                                    f"fetch run FAIL ({sym} {m} {kind}): "
                                    f"downloaded content does not match the "
                                    f"official checksum")
                            atomic_write(dest, blob)
                            record.update(status="ok", sha256=expected,
                                          bytes=len(blob))
                    except urllib.error.HTTPError as exc:
                        if exc.code == 404:
                            record.update(status="missing", http=404)
                        else:
                            record.update(status="run_fail", http=exc.code)
                            manifest.write(json.dumps(record, sort_keys=True) + "\n")
                            raise RuntimeError(
                                f"fetch run FAIL ({sym} {m} {kind}): "
                                f"HTTP {exc.code} after retries")
                    records.append(record)
                    manifest.write(json.dumps(record, sort_keys=True) + "\n")
            done_syms += 1
            if done_syms % 10 == 0:
                print(f"  fetched {done_syms}/{len(symbols)} symbols", file=sys.stderr)
    canonical = {(r["symbol"], r["month"], r["kind"]): r for r in records}
    canonical_doc = {
        "run_id": run_id,
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "entries": [canonical[k] for k in sorted(canonical)],
    }
    out_dir.joinpath("canonical-manifest.json").write_text(
        json.dumps(canonical_doc, indent=2, sort_keys=True))
    print(f"fetch pass complete: {done_syms} symbols -> {out_dir}")
    return records


def audit(out_dir: Path, candidates_path: Path) -> dict:
    """Completeness audit over the canonical manifest.

    Uses the current-symbols set FROZEN at enumeration time (no network)
    so the result is reproducible. Requires each symbol's first ok kline
    month to equal the candidate list's frozen first month; classifies
    other missing months as post-delisting or mid-lifecycle. Emits
    separate kline_verdict and funding_verdict: kline_verdict=PASS may
    advance price-only cards; funding cards require funding_verdict=PASS;
    entering raw->validated->curated requires both."""
    spec = json.loads(candidates_path.read_text())
    if "current_symbols_frozen_at_enumeration" not in spec:
        raise RuntimeError(
            "candidates file lacks the frozen current set; re-run enumerate")
    current_symbols = set(spec["current_symbols_frozen_at_enumeration"])
    canonical = json.loads((out_dir / "canonical-manifest.json").read_text())
    ok: dict[tuple[str, str], list[str]] = {}
    for e in canonical["entries"]:
        if e.get("status") in ("ok", "cached", "replaced"):
            ok.setdefault((e["symbol"], e["kind"]), []).append(e["month"])
    kline_problems: list[str] = []
    funding_problems: list[str] = []
    warnings: list[str] = []
    per_symbol: dict[str, dict] = {}
    for sym in sorted(spec["first_bar_by_symbol"]):
        frozen_first = spec["first_bar_by_symbol"][sym]
        kmonths = sorted(ok.get((sym, "kline"), []))
        fmonths = sorted(ok.get((sym, "funding"), []))
        info: dict = {"kline_months": len(kmonths), "funding_months": len(fmonths)}
        if not kmonths:
            kline_problems.append(f"{sym}: no kline data at all")
            per_symbol[sym] = info
            continue
        if kmonths[0] != frozen_first:
            kline_problems.append(
                f"{sym}: first ok month {kmonths[0]} != frozen first month "
                f"{frozen_first} (leading months missing)")
        expected = month_range(kmonths[0], kmonths[-1])
        kgaps = [m for m in expected if m not in kmonths]
        if kgaps:
            kline_problems.append(f"{sym}: kline mid-lifecycle gaps {kgaps}")
        if kmonths[-1] < "2023-12" and sym in current_symbols:
            kline_problems.append(
                f"{sym}: missing tail after {kmonths[-1]} but currently trading")
        elif kmonths[-1] < "2023-12":
            info["post_delisting_after"] = kmonths[-1]
        if not fmonths:
            funding_problems.append(f"{sym}: no funding data")
        else:
            fexpected = month_range(fmonths[0], fmonths[-1])
            fgaps = [m for m in fexpected if m not in fmonths]
            if fgaps:
                funding_problems.append(f"{sym}: funding mid-gaps {fgaps}")
        per_symbol[sym] = info
    kline_verdict = "PASS" if not kline_problems else "FAIL"
    funding_verdict = "PASS" if not funding_problems else "FAIL"
    report = {
        "canonical_run_id": canonical.get("run_id"),
        "symbols_audited": len(per_symbol),
        "kline_verdict": kline_verdict,
        "funding_verdict": funding_verdict,
        "verdict": "PASS" if kline_verdict == "PASS" and funding_verdict == "PASS" else "FAIL",
        "kline_problems": kline_problems,
        "funding_problems": funding_problems,
        "warnings": warnings,
        "per_symbol": per_symbol,
        "gate": "entering raw->validated->curated requires verdict=PASS; "
                "price-only cards may advance on kline_verdict=PASS; "
                "funding cards require funding_verdict=PASS",
    }
    (out_dir / "audit-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True))
    print(f"audit {report['verdict']} (kline {kline_verdict}, "
          f"funding {funding_verdict}): {len(kline_problems)} kline problems, "
          f"{len(funding_problems)} funding problems, "
          f"{report['symbols_audited']} symbols")
    return report


def smoke_gate_ok(report: dict) -> bool:
    """Gate: every checksum verified, real overlap, zero decimal
    mismatches, zero quote-volume invariant violations."""
    cc = report["cross_check"]
    return (all(m["sha256_ok"] for m in report["months"].values())
            and cc["overlap_days"] > 0
            and cc["decimal_mismatches"] == 0
            and cc["quote_volume_invariant_violations"] == 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["enumerate", "smoke", "fetch", "audit"])
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--months", default=None,
                        help="fetch/smoke month filter; fetch defaults to ALL months")
    parser.add_argument("--out", default="/Users/ASUS/Desktop/gmaq-data/snapshots/"
                                          "pre2024-usdm-current-survivors/raw")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--run-id", default="unassigned")
    args = parser.parse_args()
    here = Path(__file__).parent
    if args.mode == "enumerate":
        enumerate_candidates()
        return 0
    if args.mode == "smoke":
        months = args.months.split(",") if args.months else \
            ["2023-10", "2023-11", "2023-12"]
        curated = Path(
            "/Users/ASUS/Desktop/gmaq-data/snapshots/btceth-weekly-tsmom/curated/"
            "88d9ff34d0e871c4e395730e7584a828448ca62c376005c15ce7f2233c7bf615/data/"
            f"{args.symbol}-1d.jsonl")
        report = smoke(args.symbol, months, curated)
        here.joinpath("pre2024-smoke-report.json").write_text(
            json.dumps(report, indent=2))
        if not smoke_gate_ok(report):
            print("SMOKE_GATE=FAIL", file=sys.stderr)
            return 1
        print("SMOKE_GATE=PASS")
        return 0
    if args.mode == "audit":
        report = audit(Path(args.out), here / "pre2024-candidates.json")
        return 0 if report["verdict"] == "PASS" else 1
    fetch(here / "pre2024-candidates.json", Path(args.out),
          only={args.symbol} if args.symbol != "ALL" else None, limit=args.limit,
          months_filter=set(args.months.split(",")) if args.months else None,
          run_id=args.run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
