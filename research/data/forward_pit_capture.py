#!/usr/bin/env python3
"""Capture prospective Binance USD-M PIT observations without account access.

This collector is research-only.  One invocation makes one public-endpoint
capture and appends immutable JSONL envelopes; it has no scheduler, database,
account endpoint, credential, order, or runtime integration.  A supervisor or
loop belongs outside this intentionally small program.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping


FAPI = "https://fapi.binance.com"
EXCHANGE_INFO_URL = f"{FAPI}/fapi/v1/exchangeInfo"
PRICE_URL = f"{FAPI}/fapi/v2/ticker/price"
PREMIUM_URL = f"{FAPI}/fapi/v1/premiumIndex"
OPEN_INTEREST_URL = f"{FAPI}/fapi/v1/openInterest"
DEFAULT_OUT_DIR = pathlib.Path(__file__).resolve().parents[3] / "gmaq-forward-pit-capture"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class HttpResponse:
    status: int | None
    payload: bytes | None
    error: str | None


Request = Callable[[str, float], HttpResponse]


def utc_now() -> str:
    return dt.datetime.now(tz=dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def request_public(url: str, timeout: float) -> HttpResponse:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "gmaq-forward-pit-capture/1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                return HttpResponse(response.status, None, "response_exceeds_safety_bound")
            return HttpResponse(response.status, payload, None)
    except urllib.error.HTTPError as error:
        body = error.read(MAX_RESPONSE_BYTES + 1)
        return HttpResponse(error.code, body if len(body) <= MAX_RESPONSE_BYTES else None, f"http_error:{error.code}")
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return HttpResponse(None, None, f"request_error:{type(error).__name__}")


def parse_json(payload: bytes | None) -> Any | None:
    if payload is None:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def source_timestamps(payload: Any | None, keys: tuple[str, ...]) -> list[int]:
    """Return source-provided millisecond timestamps without inventing one."""
    records: list[Mapping[str, Any]] = []
    if isinstance(payload, Mapping):
        records.append(payload)
        symbols = payload.get("symbols")
        if isinstance(symbols, list):
            records.extend(item for item in symbols if isinstance(item, Mapping))
    elif isinstance(payload, list):
        records.extend(item for item in payload if isinstance(item, Mapping))
    values: set[int] = set()
    for record in records:
        for key in keys:
            value = record.get(key)
            if isinstance(value, int) and value >= 0:
                values.add(value)
            elif isinstance(value, str) and value.isdigit():
                values.add(int(value))
    return sorted(values)


def envelope(event_type: str, endpoint: str, response: HttpResponse, arrival_utc: str) -> dict[str, Any]:
    decoded = parse_json(response.payload)
    return {
        "schema_version": 1,
        "event_type": event_type,
        "endpoint": endpoint,
        "source_event_timestamps_utc_ms": source_timestamps(
            decoded, ("serverTime", "time", "updateTime", "fundingTime")
        ),
        "source_scheduled_timestamps_utc_ms": source_timestamps(decoded, ("nextFundingTime",)),
        "local_arrival_utc": arrival_utc,
        "http_status": response.status,
        "error_status": response.error,
        "raw_payload_base64": base64.b64encode(response.payload).decode("ascii") if response.payload is not None else None,
        "raw_payload_sha256": hashlib.sha256(response.payload).hexdigest() if response.payload is not None else None,
    }


def append_envelope(out_dir: pathlib.Path, record: Mapping[str, Any]) -> pathlib.Path:
    if out_dir.exists() and (not out_dir.is_dir() or out_dir.is_symlink()):
        raise ValueError("output directory must be a real directory")
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    day = str(record["local_arrival_utc"])[:10]
    path = out_dir / f"forward-pit-{day}.jsonl"
    if path.exists() and (not path.is_file() or path.is_symlink()):
        raise ValueError("append target must be a regular non-symlink file")
    line = (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "ab", closefd=True) as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def active_usdt_perpetual_symbols(payload: Any | None) -> list[str]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("symbols"), list):
        return []
    symbols = {
        item.get("symbol")
        for item in payload["symbols"]
        if isinstance(item, Mapping)
        and item.get("status") == "TRADING"
        and item.get("contractType") == "PERPETUAL"
        and item.get("quoteAsset") == "USDT"
        and isinstance(item.get("symbol"), str)
    }
    return sorted(symbols)


def capture_once(
    out_dir: pathlib.Path,
    *,
    timeout: float = 10.0,
    oi_delay_seconds: float = 0.05,
    max_symbols: int | None = None,
    symbol_offset: int = 0,
    request: Request = request_public,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Append one full public capture; failures are recorded rather than hidden."""
    if timeout <= 0 or oi_delay_seconds < 0 or symbol_offset < 0 or (max_symbols is not None and max_symbols < 1):
        raise ValueError("invalid capture arguments")
    written: list[pathlib.Path] = []

    exchange = request(EXCHANGE_INFO_URL, timeout)
    exchange_arrival = utc_now()
    written.append(append_envelope(out_dir, envelope("instrument_status", EXCHANGE_INFO_URL, exchange, exchange_arrival)))
    symbols = active_usdt_perpetual_symbols(parse_json(exchange.payload))
    if symbols and symbol_offset:
        offset = symbol_offset % len(symbols)
        symbols = symbols[offset:] + symbols[:offset]
    if max_symbols is not None:
        symbols = symbols[:max_symbols]

    for event_type, url in (("price", PRICE_URL), ("funding_premium", PREMIUM_URL)):
        response = request(url, timeout)
        written.append(append_envelope(out_dir, envelope(event_type, url, response, utc_now())))

    for index, symbol in enumerate(symbols):
        endpoint = f"{OPEN_INTEREST_URL}?{urllib.parse.urlencode({'symbol': symbol})}"
        response = request(endpoint, timeout)
        written.append(append_envelope(out_dir, envelope("open_interest", endpoint, response, utc_now())))
        if oi_delay_seconds and index + 1 < len(symbols):
            sleep(oi_delay_seconds)

    return {
        "verdict": "CAPTURED_RESEARCH_ONLY",
        "records_appended": len(written),
        "active_usdt_perpetual_symbols_requested": len(symbols),
        "open_interest_symbol_offset": symbol_offset,
        "open_interest_symbols_requested": symbols,
        "output_files": sorted({str(path) for path in written}),
        "credentials_used": False,
        "orders_submitted": False,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--out-dir", type=pathlib.Path, default=DEFAULT_OUT_DIR)
    value.add_argument("--timeout", type=float, default=10.0)
    value.add_argument("--oi-delay-seconds", type=float, default=0.05)
    value.add_argument("--max-symbols", type=int, default=None, help="test/sampling bound; omit to capture every active USDT perpetual")
    value.add_argument("--symbol-offset", type=int, default=0, help="rotate the sorted OI symbol list before applying --max-symbols")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    result = capture_once(
        args.out_dir,
        timeout=args.timeout,
        oi_delay_seconds=args.oi_delay_seconds,
        max_symbols=args.max_symbols,
        symbol_offset=args.symbol_offset,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
