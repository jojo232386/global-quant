#!/usr/bin/env python3
"""Build and query the bounded GMAQ PIT Instrument Master V1.

The master is deliberately a fixed historical cohort, not a claim about the
complete Binance USD-M market.  Cohort membership comes from one archived
official ``exchangeInfo`` response.  Price V1 supplies event-time rows, while
authoritative terminal evidence distinguishes real activity from
post-settlement zero-volume tails.  The existing Lifecycle V1 sidecar is
reused and a small cohort-only supplement is bound beside it.  Numeric archive
vintage remains explicitly unverified.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import pathlib
import sys
from typing import Any, Mapping, Sequence


ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gmaq_data import verify_snapshot  # noqa: E402
from research.data.expl_017_lifecycle_v1 import (  # noqa: E402
    TERMINATED_CONFIRMED,
    load_sidecar,
)
from research.exploration.price_alpha_v1 import (  # noqa: E402
    DATASET as PRICE_DATASET,
    DATA_ROOT as DEFAULT_DATA_ROOT,
    DAY_MS,
    MANIFEST_SHA256 as PRICE_MANIFEST_SHA256,
    PIT_SHA256 as PRICE_PIT_SHA256,
    SNAPSHOT_ID as PRICE_SNAPSHOT_ID,
    load_dataset,
)


EVIDENCE_DIR = pathlib.Path(__file__).with_name("pit-instrument-master-v1")
RAW_DIR = EVIDENCE_DIR / "raw"
SNAPSHOT_PATH = RAW_DIR / "wayback-fapi-exchange-info-20210104195101.json"
CDX_PATH = RAW_DIR / "wayback-cdx-exchange-info.json"
ACTIVITY_PATH = EVIDENCE_DIR / "price-v1-cohort-activity.json"
MASTER_PATH = EVIDENCE_DIR / "pit-instrument-master-v1.json"
LIFECYCLE_PATH = pathlib.Path(__file__).with_name("expl-017-lifecycle-v1.json")
SUPPLEMENTAL_LIFECYCLE_PATH = EVIDENCE_DIR / "pit-cohort-terminal-evidence-v1.json"

MASTER_CLASS = "GMAQ_PIT_INSTRUMENT_MASTER_V1"
ACTIVITY_CLASS = "GMAQ_PRICE_V1_COHORT_ACTIVITY_EVIDENCE"
SUPPLEMENTAL_LIFECYCLE_CLASS = "GMAQ_PIT_COHORT_TERMINAL_EVIDENCE_V1"
COHORT_ID = "BINANCE_USDM_PERPETUAL_TRADING_20210104_195102Z"
WAYBACK_TIMESTAMP = "20210104195101"
WAYBACK_CAPTURE_UTC = "2021-01-04T19:51:01Z"
SOURCE_RETRIEVED_UTC = "2026-08-24T14:44:12Z"
CDX_RETRIEVED_UTC = "2026-08-24T14:44:44Z"
ORIGINAL_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
SNAPSHOT_SHA256 = "0c3613accfe9acf01cb6fb0523835ee52dea17f1a7df9de84e6a1fe39e27dfd2"
CDX_SHA256 = "b4e48f0a4e86b55f8158599d56adb0465d3a74b7aab3034ddfb7c818e2f59ac1"
SUPPLEMENTAL_LIFECYCLE_SHA256 = (
    "5ec5624bf75006581d461cce92cb288c7aec27db9ea9b09791f48ec85b5c38e8"
)
COVERAGE_END_UTC = "2023-11-15T00:00:00Z"
EXPECTED_COHORT_SIZE = 80
CANONICAL_TERMINALS = {"BZRXUSDT", "YFIIUSDT"}
SUPPLEMENTAL_TERMINALS = {"CVCUSDT", "HNTUSDT", "SRMUSDT"}
EXPECTED_SUPPLEMENTAL_ARCHIVE_SUMMARIES = {
    "CVCUSDT": (
        "2022-11-29T00:00:03.564Z",
        "2022-11-29T08:59:53.780Z",
        20658,
    ),
    "HNTUSDT": (
        "2023-03-20T00:00:00.094Z",
        "2023-03-20T09:00:23.879Z",
        28894,
    ),
    "SRMUSDT": (
        "2022-11-15T00:00:00.002Z",
        "2022-11-15T04:30:02.092Z",
        284357,
    ),
}
EXPECTED_TERMINALS = CANONICAL_TERMINALS | SUPPLEMENTAL_TERMINALS
EXPECTED_QUARANTINE = "AKROUSDT"
EXPECTED_ZERO_TAILS = {
    "CVCUSDT": 397,
    "HNTUSDT": 286,
    "SRMUSDT": 411,
    "TOMOUSDT": 47,
}
UNRESOLVED_COVERAGE_STOP_SYMBOL = "TOMOUSDT"


class InstrumentMasterError(RuntimeError):
    """An evidence, lineage, lifecycle, or query invariant failed."""


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wayback_cdx_digest(path: pathlib.Path) -> str:
    """Return Wayback's uppercase, unpadded Base32 SHA-1 content identifier."""
    digest = hashlib.sha1(path.read_bytes()).digest()  # noqa: S324 - archive format
    return base64.b32encode(digest).decode("ascii").rstrip("=")


def _require_regular(path: pathlib.Path) -> None:
    absolute = path.absolute()
    root = ROOT.absolute()
    try:
        relative = absolute.relative_to(root)
    except ValueError:
        relative = None
    if relative is not None:
        cursor = root
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise InstrumentMasterError(f"evidence path contains a symlink: {path}")
    if path.is_symlink() or not path.is_file():
        raise InstrumentMasterError(f"evidence is not a regular file: {path}")


def _load_json(path: pathlib.Path) -> Any:
    _require_regular(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InstrumentMasterError(f"invalid JSON evidence: {path}") from error


def _utc_from_ms(value: int) -> str:
    if type(value) is not int or value < 0:
        raise InstrumentMasterError("timestamp must be a non-negative integer")
    parsed = dt.datetime.fromtimestamp(value / 1000, tz=dt.UTC)
    rendered = parsed.isoformat(timespec="milliseconds")
    return rendered.replace("+00:00", "Z")


def _parse_utc(value: Any, *, field: str) -> dt.datetime:
    if not isinstance(value, str):
        raise InstrumentMasterError(f"invalid {field}")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise InstrumentMasterError(f"invalid {field}") from error
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise InstrumentMasterError(f"{field} must be UTC")
    return parsed


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _load_snapshot() -> tuple[dict[str, Mapping[str, Any]], str]:
    if sha256_file(SNAPSHOT_PATH) != SNAPSHOT_SHA256:
        raise InstrumentMasterError("historical exchangeInfo SHA-256 mismatch")
    payload = _load_json(SNAPSHOT_PATH)
    if not isinstance(payload, Mapping):
        raise InstrumentMasterError("historical exchangeInfo must be an object")
    if payload.get("timezone") != "UTC" or payload.get("futuresType") != "U_MARGINED":
        raise InstrumentMasterError("historical exchangeInfo venue identity mismatch")
    response_utc = _utc_from_ms(payload.get("serverTime"))
    capture = _parse_utc(WAYBACK_CAPTURE_UTC, field="Wayback capture")
    response = _parse_utc(response_utc, field="exchangeInfo server time")
    if not dt.timedelta(0) <= response - capture <= dt.timedelta(seconds=10):
        raise InstrumentMasterError("Wayback capture and official server time differ")

    raw_symbols = payload.get("symbols")
    if not isinstance(raw_symbols, list):
        raise InstrumentMasterError("historical exchangeInfo lacks symbols")
    selected: dict[str, Mapping[str, Any]] = {}
    required_strings = (
        "symbol",
        "pair",
        "baseAsset",
        "quoteAsset",
        "marginAsset",
        "contractType",
        "status",
    )
    for raw in raw_symbols:
        if not isinstance(raw, Mapping):
            raise InstrumentMasterError("malformed historical instrument")
        if not all(isinstance(raw.get(key), str) and raw[key] for key in required_strings):
            raise InstrumentMasterError("historical instrument identity incomplete")
        if (
            raw["contractType"] != "PERPETUAL"
            or raw["quoteAsset"] != "USDT"
            or raw["status"] != "TRADING"
        ):
            raise InstrumentMasterError("snapshot is not the frozen all-trading USD-M cohort")
        symbol = raw["symbol"]
        if symbol in selected or not symbol.isalnum() or symbol != symbol.upper():
            raise InstrumentMasterError("duplicate or malformed historical symbol")
        if type(raw.get("onboardDate")) is not int:
            raise InstrumentMasterError("exchange-reported onboardDate is malformed")
        selected[symbol] = raw
    if len(selected) != EXPECTED_COHORT_SIZE or len(raw_symbols) != EXPECTED_COHORT_SIZE:
        raise InstrumentMasterError("historical cohort size differs")
    return selected, response_utc


def _load_cdx() -> dict[str, str]:
    if sha256_file(CDX_PATH) != CDX_SHA256:
        raise InstrumentMasterError("Wayback CDX SHA-256 mismatch")
    payload = _load_json(CDX_PATH)
    expected_header = ["timestamp", "original", "digest", "statuscode", "mimetype"]
    if not isinstance(payload, list) or not payload or payload[0] != expected_header:
        raise InstrumentMasterError("Wayback CDX schema differs")
    matches = [dict(zip(expected_header, row)) for row in payload[1:] if row[0] == WAYBACK_TIMESTAMP]
    if len(matches) != 1:
        raise InstrumentMasterError("Wayback CDX capture identity is ambiguous")
    match = matches[0]
    if (
        match["original"] != ORIGINAL_URL
        or match["statuscode"] != "200"
        or match["mimetype"] != "application/json"
        or not match["digest"]
    ):
        raise InstrumentMasterError("Wayback CDX capture metadata differs")
    if match["digest"] != _wayback_cdx_digest(SNAPSHOT_PATH):
        raise InstrumentMasterError("Wayback CDX digest does not bind the saved response body")
    return match


def capture_price_activity(
    data_root: pathlib.Path = DEFAULT_DATA_ROOT,
) -> dict[str, Any]:
    """Replay canonical Price V1 and summarize only the frozen cohort."""
    cohort, _ = _load_snapshot()
    dataset = load_dataset(data_root)
    record = verify_snapshot(
        data_root,
        PRICE_SNAPSHOT_ID,
        expected_dataset=PRICE_DATASET,
        minimum_stage="curated",
    )
    if record["integrity_verdict"] != "VERIFIED" or record["quality_verdict"] != "PASS":
        raise InstrumentMasterError("Price V1 is not VERIFIED/PASS")
    file_by_role = {item["role"]: item for item in record["files"]}
    rows: list[dict[str, Any]] = []
    missing = sorted(set(cohort) - set(dataset.bars))
    if missing:
        raise InstrumentMasterError(f"Price V1 lacks cohort symbols: {missing}")
    for symbol in sorted(cohort):
        points = sorted(dataset.bars[symbol])
        positive_points = [
            timestamp
            for timestamp in points
            if dataset.bars[symbol][timestamp].quote_volume > 0
        ]
        if not positive_points:
            raise InstrumentMasterError(
                f"Price V1 has no positive-volume activity: {symbol}"
            )
        last_positive = positive_points[-1]
        trailing_points = [timestamp for timestamp in points if timestamp > last_positive]
        if any(
            dataset.bars[symbol][timestamp].quote_volume == 0
            for timestamp in points
            if timestamp < last_positive
        ):
            raise InstrumentMasterError(f"Price V1 has an internal zero-volume day: {symbol}")
        if any(
            dataset.bars[symbol][timestamp].quote_volume != 0
            for timestamp in trailing_points
        ):
            raise InstrumentMasterError(f"Price V1 activity tail is inconsistent: {symbol}")
        gaps = sum(right != left + DAY_MS for left, right in zip(points, points[1:]))
        role = f"kline.{symbol}"
        source_file = file_by_role.get(role)
        if gaps or not source_file or source_file["row_count"] != len(points):
            raise InstrumentMasterError(f"Price V1 activity contract differs for {symbol}")
        rows.append(
            {
                "symbol": symbol,
                "first_bar_open_utc": _utc_from_ms(points[0]),
                "last_bar_open_utc": _utc_from_ms(points[-1]),
                "last_positive_quote_volume_bar_open_utc": _utc_from_ms(last_positive),
                "trailing_zero_quote_volume_day_count": len(trailing_points),
                "row_count": len(points),
                "internal_gap_count": gaps,
                "source_role": role,
                "source_file_sha256": source_file["sha256"],
                "first_bar_semantics": "PROXY_EVIDENCE_NOT_LISTING_TIMESTAMP",
                "activity_semantics": (
                    "POSITIVE_QUOTE_VOLUME_IS_EVENT_ACTIVITY; ZERO_VOLUME_ROWS_ARE_NOT_ACTIVITY; "
                    "NUMERIC_VINTAGE_UNVERIFIED"
                ),
            }
        )
    return {
        "artifact_class": ACTIVITY_CLASS,
        "artifact_version": 1,
        "cohort_id": COHORT_ID,
        "source": {
            "dataset_id": PRICE_DATASET,
            "snapshot_id": PRICE_SNAPSHOT_ID,
            "manifest_sha256": PRICE_MANIFEST_SHA256,
            "pit_sha256": PRICE_PIT_SHA256,
            "schema_id": record["schema_id"],
            "created_at_utc": record["created_at_utc"],
            "integrity_verdict": record["integrity_verdict"],
            "quality_verdict": record["quality_verdict"],
            "labels": record["source_metadata"].get("labels"),
            "numeric_vintage_lineage": "VINTAGE_UNVERIFIED",
        },
        "records": rows,
    }


def _load_activity() -> dict[str, Mapping[str, Any]]:
    payload = _load_json(ACTIVITY_PATH)
    if not isinstance(payload, Mapping) or payload.get("artifact_class") != ACTIVITY_CLASS:
        raise InstrumentMasterError("Price V1 activity artifact identity differs")
    source = payload.get("source")
    if not isinstance(source, Mapping):
        raise InstrumentMasterError("Price V1 activity source is absent")
    required = {
        "dataset_id": PRICE_DATASET,
        "snapshot_id": PRICE_SNAPSHOT_ID,
        "manifest_sha256": PRICE_MANIFEST_SHA256,
        "pit_sha256": PRICE_PIT_SHA256,
        "integrity_verdict": "VERIFIED",
        "quality_verdict": "PASS",
        "numeric_vintage_lineage": "VINTAGE_UNVERIFIED",
    }
    if any(source.get(key) != value for key, value in required.items()):
        raise InstrumentMasterError("Price V1 activity lineage differs")
    records = payload.get("records")
    if not isinstance(records, list) or len(records) != EXPECTED_COHORT_SIZE:
        raise InstrumentMasterError("Price V1 activity cohort size differs")
    output: dict[str, Mapping[str, Any]] = {}
    for row in records:
        if not isinstance(row, Mapping) or not isinstance(row.get("symbol"), str):
            raise InstrumentMasterError("malformed Price V1 activity record")
        symbol = row["symbol"]
        if symbol in output or row.get("internal_gap_count") != 0:
            raise InstrumentMasterError("duplicate or discontinuous Price V1 activity")
        if row.get("first_bar_semantics") != "PROXY_EVIDENCE_NOT_LISTING_TIMESTAMP":
            raise InstrumentMasterError("first bar was promoted beyond proxy evidence")
        if row.get("activity_semantics") != (
            "POSITIVE_QUOTE_VOLUME_IS_EVENT_ACTIVITY; ZERO_VOLUME_ROWS_ARE_NOT_ACTIVITY; "
            "NUMERIC_VINTAGE_UNVERIFIED"
        ):
            raise InstrumentMasterError("Price V1 activity availability semantics differ")
        expected_tail = EXPECTED_ZERO_TAILS.get(symbol, 0)
        if row.get("trailing_zero_quote_volume_day_count") != expected_tail:
            raise InstrumentMasterError("Price V1 zero-volume tail differs")
        first = _parse_utc(row.get("first_bar_open_utc"), field="first bar")
        last_positive = _parse_utc(
            row.get("last_positive_quote_volume_bar_open_utc"), field="last positive bar"
        )
        last = _parse_utc(row.get("last_bar_open_utc"), field="last bar")
        if not first <= last_positive <= last:
            raise InstrumentMasterError("Price V1 activity timestamps differ")
        if not isinstance(row.get("source_file_sha256"), str) or len(row["source_file_sha256"]) != 64:
            raise InstrumentMasterError("Price V1 activity file identity is absent")
        output[symbol] = row
    return output


def _load_supplemental_events() -> tuple[
    dict[str, Mapping[str, Any]], Mapping[str, Any]
]:
    if sha256_file(SUPPLEMENTAL_LIFECYCLE_PATH) != SUPPLEMENTAL_LIFECYCLE_SHA256:
        raise InstrumentMasterError("supplemental terminal artifact SHA-256 mismatch")
    payload = _load_json(SUPPLEMENTAL_LIFECYCLE_PATH)
    if (
        not isinstance(payload, Mapping)
        or payload.get("artifact_class") != SUPPLEMENTAL_LIFECYCLE_CLASS
        or payload.get("artifact_version") != 1
    ):
        raise InstrumentMasterError("supplemental terminal artifact identity differs")
    if payload.get("source_policy") != (
        "OFFICIAL_ANNOUNCEMENT_PLUS_OFFICIAL_EVENT_ARCHIVE; "
        "SUBMINUTE_SETTLEMENT_TAILS_RECORDED_NOT_HIDDEN"
    ):
        raise InstrumentMasterError("supplemental terminal source policy differs")
    rows = payload.get("events")
    if not isinstance(rows, list):
        raise InstrumentMasterError("supplemental terminal events are absent")
    events: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("symbol"), str):
            raise InstrumentMasterError("supplemental terminal event is malformed")
        symbol = row["symbol"]
        source = row.get("evidence_source")
        identity = row.get("evidence_identity")
        archive = row.get("trade_archive")
        if symbol in events or row.get("classification") != TERMINATED_CONFIRMED:
            raise InstrumentMasterError("supplemental terminal set is duplicate or unresolved")
        if not all(isinstance(value, Mapping) for value in (source, identity, archive)):
            raise InstrumentMasterError("supplemental terminal evidence is incomplete")
        published = _parse_utc(row.get("evidence_published_at_utc"), field="publication")
        effective = _parse_utc(row.get("terminal_effective_at_utc"), field="terminal")
        first_trade = _parse_utc(archive.get("first_trade_at_utc"), field="first trade")
        last_trade = _parse_utc(archive.get("last_trade_at_utc"), field="last trade")
        if not published < effective < _parse_utc(
            COVERAGE_END_UTC, field="coverage end"
        ):
            raise InstrumentMasterError("supplemental terminal chronology differs")
        if not first_trade < effective or not last_trade <= effective + dt.timedelta(
            minutes=1
        ):
            raise InstrumentMasterError("supplemental terminal archive boundary differs")
        actual_tail = max(0.0, (last_trade - effective).total_seconds())
        recorded_tail = archive.get("settlement_tail_after_effective_seconds")
        if (
            isinstance(recorded_tail, bool)
            or not isinstance(recorded_tail, (int, float))
            or abs(float(recorded_tail) - actual_tail) > 0.000_001
        ):
            raise InstrumentMasterError("supplemental settlement tail differs")
        if type(archive.get("row_count")) is not int or archive["row_count"] <= 0:
            raise InstrumentMasterError("supplemental terminal archive count differs")
        expected_archive_summary = EXPECTED_SUPPLEMENTAL_ARCHIVE_SUMMARIES.get(symbol)
        if expected_archive_summary != (
            archive.get("first_trade_at_utc"),
            archive.get("last_trade_at_utc"),
            archive.get("row_count"),
        ):
            raise InstrumentMasterError("supplemental terminal archive summary differs")
        if not source.get("announcement_url", "").startswith(
            "https://www.binance.com/en/support/announcement/detail/"
        ) or not source.get("cms_api_url", "").startswith(
            "https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query?"
        ):
            raise InstrumentMasterError("supplemental announcement source differs")
        expected_archive_prefix = (
            "https://data.binance.vision/data/futures/um/daily/aggTrades/"
            f"{symbol}/{symbol}-aggTrades-"
        )
        if not source.get("trade_archive_url", "").startswith(expected_archive_prefix):
            raise InstrumentMasterError("supplemental trade archive source differs")
        if any(
            not isinstance(identity.get(key), str)
            or len(identity[key]) != 64
            or any(character not in "0123456789abcdef" for character in identity[key])
            for key in ("announcement_response_sha256", "trade_archive_sha256")
        ):
            raise InstrumentMasterError("supplemental terminal hashes differ")
        events[symbol] = row
    if set(events) != SUPPLEMENTAL_TERMINALS:
        raise InstrumentMasterError("supplemental terminal symbol set differs")

    coverage_stop = payload.get("coverage_stop")
    if not isinstance(coverage_stop, Mapping) or coverage_stop.get("symbol") != (
        UNRESOLVED_COVERAGE_STOP_SYMBOL
    ):
        raise InstrumentMasterError("unresolved coverage stop is absent")
    required_stop = {
        "classification": "LIFECYCLE_UNRESOLVED_NO_TERMINAL_INFERENCE",
        "coverage_end_exclusive_utc": COVERAGE_END_UTC,
        "last_positive_quote_volume_bar_open_utc": "2023-11-14T00:00:00.000Z",
        "first_zero_quote_volume_tail_bar_open_utc": "2023-11-15T00:00:00.000Z",
        "trailing_zero_quote_volume_day_count": EXPECTED_ZERO_TAILS[
            UNRESOLVED_COVERAGE_STOP_SYMBOL
        ],
    }
    if any(coverage_stop.get(key) != value for key, value in required_stop.items()):
        raise InstrumentMasterError("unresolved coverage stop contract differs")
    return events, coverage_stop


def _terminal_source(raw: Mapping[str, Any]) -> dict[str, Any]:
    source = raw.get("evidence_source")
    identity = raw.get("evidence_identity")
    if not isinstance(source, Mapping) or not isinstance(identity, Mapping):
        raise InstrumentMasterError("terminal evidence is incomplete")
    return {
        "evidence_type": "TIER_A_OFFICIAL_ANNOUNCEMENT_PLUS_EVENT_ARCHIVE",
        "published_at_utc": raw["evidence_published_at_utc"],
        "effective_at_utc": raw["terminal_effective_at_utc"],
        "announcement_url": source["announcement_url"],
        "trade_archive_url": source["trade_archive_url"],
        **({"cms_api_url": source["cms_api_url"]} if "cms_api_url" in source else {}),
        "announcement_response_sha256": identity["announcement_response_sha256"],
        "trade_archive_sha256": identity["trade_archive_sha256"],
    }


def build_master() -> dict[str, Any]:
    cohort, response_utc = _load_snapshot()
    cdx = _load_cdx()
    activity = _load_activity()
    if set(activity) != set(cohort):
        raise InstrumentMasterError("historical cohort and Price V1 activity differ")

    events, lifecycle_sha = load_sidecar(LIFECYCLE_PATH)
    lifecycle_raw = _load_json(LIFECYCLE_PATH)
    raw_events = {row["symbol"]: row for row in lifecycle_raw["exceptions"]}
    canonical_events = {symbol: event for symbol, event in events.items() if symbol in cohort}
    if set(canonical_events) != CANONICAL_TERMINALS:
        raise InstrumentMasterError("canonical cohort terminal set differs")
    if any(event.classification != TERMINATED_CONFIRMED for event in canonical_events.values()):
        raise InstrumentMasterError("frozen cohort contains an unresolved terminal")
    supplemental_events, coverage_stop = _load_supplemental_events()
    terminal_records = {
        **{symbol: raw_events[symbol] for symbol in canonical_events},
        **supplemental_events,
    }
    if set(terminal_records) != EXPECTED_TERMINALS:
        raise InstrumentMasterError("combined cohort terminal set differs")
    supplemental_sha = SUPPLEMENTAL_LIFECYCLE_SHA256

    records: list[dict[str, Any]] = []
    for symbol in sorted(cohort):
        raw = cohort[symbol]
        activity_row = activity[symbol]
        terminal = terminal_records.get(symbol)
        terminal_evidence = _terminal_source(terminal) if terminal else None
        interval_end = terminal["terminal_effective_at_utc"] if terminal else COVERAGE_END_UTC
        listing_start = response_utc
        if _parse_utc(activity_row["first_bar_open_utc"], field="first bar") > _parse_utc(
            listing_start, field="confirmed active start"
        ):
            raise InstrumentMasterError(f"Price activity starts after cohort capture: {symbol}")
        last_positive = _parse_utc(
            activity_row["last_positive_quote_volume_bar_open_utc"],
            field="last positive bar",
        )
        if terminal and last_positive.date() != _parse_utc(
            interval_end, field="terminal effective"
        ).date():
            raise InstrumentMasterError(f"terminal activity date differs for {symbol}")
        required_active_through = _parse_utc(
            COVERAGE_END_UTC, field="coverage end"
        ) - dt.timedelta(days=1)
        if not terminal and last_positive < required_active_through:
            raise InstrumentMasterError(f"active cohort member does not reach cutoff: {symbol}")
        if symbol == UNRESOLVED_COVERAGE_STOP_SYMBOL and (
            activity_row["last_positive_quote_volume_bar_open_utc"]
            != coverage_stop["last_positive_quote_volume_bar_open_utc"]
        ):
            raise InstrumentMasterError("coverage stop and Price V1 activity differ")
        records.append(
            {
                "instrument_id": f"BINANCE:USD_M:PERPETUAL:{symbol}",
                "symbol": symbol,
                "base_asset": raw["baseAsset"],
                "quote_asset": raw["quoteAsset"],
                "margin_asset": raw["marginAsset"],
                "contract_type": raw["contractType"],
                "listing_timestamp_utc": listing_start,
                "listing_timestamp_semantics": (
                    "CONSERVATIVE_CONFIRMED_ACTIVE_FROM; NOT_TRUE_ONBOARD_TIMESTAMP"
                ),
                "exchange_reported_onboard_timestamp_utc": _utc_from_ms(raw["onboardDate"]),
                "exchange_reported_onboard_trusted": False,
                "terminal_timestamp_utc": interval_end if terminal else None,
                "status_intervals": [
                    {
                        "status": "TRADING_CONFIRMED",
                        "start_inclusive_utc": listing_start,
                        "end_exclusive_utc": interval_end,
                    }
                ],
                "source": {
                    "venue": "BINANCE_USD_M",
                    "official_url": ORIGINAL_URL,
                    "wayback_capture_timestamp": WAYBACK_TIMESTAMP,
                    "wayback_capture_utc": WAYBACK_CAPTURE_UTC,
                    "wayback_digest": cdx["digest"],
                    "wayback_cdx_retrieval_timestamp_utc": CDX_RETRIEVED_UTC,
                    "official_response_sha256": SNAPSHOT_SHA256,
                    "official_response_timestamp_utc": response_utc,
                    "historical_status_as_of_utc": response_utc,
                    "source_publication_timestamp_utc": None,
                    "source_publication_semantics": "NOT_APPLICABLE_TO_REST_STATUS_RESPONSE",
                    "source_retrieval_timestamp_utc": SOURCE_RETRIEVED_UTC,
                },
                "evidence_type": "TIER_B_WAYBACK_CAPTURE_OF_OFFICIAL_EXCHANGEINFO",
                "confidence": "HIGH_FOR_STATUS_AT_CAPTURE",
                "activity_evidence": dict(activity_row),
                "terminal_evidence": terminal_evidence,
                "lineage": {
                    "cohort_id": COHORT_ID,
                    "price_snapshot_id": PRICE_SNAPSHOT_ID,
                    "price_manifest_sha256": PRICE_MANIFEST_SHA256,
                    "price_pit_sha256": PRICE_PIT_SHA256,
                    "lifecycle_sidecar_sha256": lifecycle_sha,
                    "supplemental_terminal_evidence_sha256": supplemental_sha,
                },
                "vintage_lineage": {
                    "instrument_status_snapshot": "VERIFIED_HISTORICAL_CAPTURE",
                    "event_activity": (
                        "POSITIVE_QUOTE_VOLUME_EVENT_TIME_ONLY; "
                        "ZERO_VOLUME_ROWS_NOT_ACTIVITY"
                    ),
                    "numeric_archive_bytes": "VINTAGE_UNVERIFIED",
                },
                "conflict_status": "NONE",
            }
        )

    akro = raw_events.get(EXPECTED_QUARANTINE)
    if not isinstance(akro, Mapping) or akro.get("classification") != "TERMINATED_UNCONFIRMED":
        raise InstrumentMasterError("AKRO quarantine evidence differs")
    quarantine = {
        "instrument_id": f"BINANCE:USD_M:PERPETUAL:{EXPECTED_QUARANTINE}",
        "symbol": EXPECTED_QUARANTINE,
        "cohort_member": False,
        "conflict_status": "QUARANTINED",
        "reason": akro.get("conflict"),
        "announced_terminal_effective_at_utc": akro["terminal_effective_at_utc"],
        "evidence_published_at_utc": akro["evidence_published_at_utc"],
        "last_valid_daily_bar_utc": akro["last_valid_daily_bar_utc"],
        "source": _terminal_source(akro),
        "universe_eligible": False,
    }
    return {
        "artifact_class": MASTER_CLASS,
        "artifact_version": 1,
        "result_class": "PARTIAL_PIT_COHORT_CANDIDATE",
        "cohort": {
            "cohort_id": COHORT_ID,
            "definition": (
                "All 80 TRADING USDT-quoted PERPETUAL instruments in the archived official "
                "Binance USD-M exchangeInfo response at the frozen capture"
            ),
            "selection_uses_current_survivors": False,
            "selection_uses_price_outcomes": False,
            "symbol_count": len(records),
            "formally_tier2_admitted_symbol_count": 0,
            "tier2_data_foundation_ready": False,
            "coverage_start_inclusive_utc": response_utc,
            "coverage_end_exclusive_utc": COVERAGE_END_UTC,
            "coverage_end_reason": (
                "TOMOUSDT_POST_2023_11_14_LIFECYCLE_UNRESOLVED; "
                "ZERO_VOLUME_TAIL_NOT_TERMINAL_EVIDENCE"
            ),
            "scope_limit": "FIXED_COHORT_NOT_COMPLETE_DYNAMIC_MARKET_UNIVERSE",
        },
        "input_lineage": {
            "wayback_exchange_info_sha256": SNAPSHOT_SHA256,
            "wayback_cdx_sha256": CDX_SHA256,
            "price_activity_sha256": sha256_file(ACTIVITY_PATH),
            "lifecycle_sidecar_sha256": lifecycle_sha,
            "supplemental_terminal_evidence_sha256": supplemental_sha,
        },
        "availability_contract": {
            "instrument_status": "HISTORICALLY_CAPTURED_OFFICIAL_RESPONSE",
            "event_timestamp": "PRICE_V1_EXCHANGE_EVENT_TIME",
            "exchange_timestamp": "PRICE_V1_EXCHANGE_EVENT_TIME",
            "historical_status_as_of_timestamp": response_utc,
            "publication_timestamp": "NOT_APPLICABLE_TO_REST_STATUS_RESPONSE",
            "collection_timestamp": WAYBACK_CAPTURE_UTC,
            "retrieval_timestamp": SOURCE_RETRIEVED_UTC,
            "numeric_price_vintage": "VINTAGE_UNVERIFIED",
            "funding_oi_vintage": "VINTAGE_UNVERIFIED",
            "zero_volume_tail_policy": (
                "NOT_TRADING_ACTIVITY_OR_TERMINAL_EVIDENCE; FAIL_CLOSED_AT_FIRST_"
                "UNRESOLVED_TAIL"
            ),
        },
        "records": records,
        "quarantine": [quarantine],
    }


def load_master(path: pathlib.Path = MASTER_PATH) -> dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, dict) or payload.get("artifact_class") != MASTER_CLASS:
        raise InstrumentMasterError("instrument master identity differs")
    expected = build_master()
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise InstrumentMasterError("instrument master is unreadable") from error
    if payload != expected or raw != _canonical_json(expected):
        raise InstrumentMasterError("instrument master differs from deterministic rebuild")
    return payload


def universe_at(payload: Mapping[str, Any], timestamp: str) -> tuple[str, ...]:
    as_of = _parse_utc(timestamp, field="universe as-of timestamp")
    cohort = payload.get("cohort")
    records = payload.get("records")
    if not isinstance(cohort, Mapping) or not isinstance(records, list):
        raise InstrumentMasterError("instrument master is malformed")
    start = _parse_utc(cohort["coverage_start_inclusive_utc"], field="coverage start")
    end = _parse_utc(cohort["coverage_end_exclusive_utc"], field="coverage end")
    if as_of < start or as_of >= end:
        raise InstrumentMasterError("universe query is outside the proven cohort window")
    active: list[str] = []
    for record in records:
        if record.get("conflict_status") != "NONE":
            continue
        intervals = record.get("status_intervals")
        if not isinstance(intervals, list) or len(intervals) != 1:
            raise InstrumentMasterError("instrument status interval is malformed")
        interval = intervals[0]
        left = _parse_utc(interval["start_inclusive_utc"], field="interval start")
        right = _parse_utc(interval["end_exclusive_utc"], field="interval end")
        if left <= as_of < right:
            active.append(record["symbol"])
    return tuple(sorted(active))


def _write(path: pathlib.Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical_json(value), encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture-activity")
    capture.add_argument("--data-root", type=pathlib.Path, default=DEFAULT_DATA_ROOT)
    capture.add_argument("--output", type=pathlib.Path, default=ACTIVITY_PATH)
    rebuild = sub.add_parser("rebuild")
    rebuild.add_argument("--output", type=pathlib.Path, default=MASTER_PATH)
    check = sub.add_parser("check")
    check.add_argument("--verify-price-v1", action="store_true")
    check.add_argument("--data-root", type=pathlib.Path, default=DEFAULT_DATA_ROOT)
    query = sub.add_parser("universe-at")
    query.add_argument("timestamp")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "capture-activity":
        value = capture_price_activity(args.data_root)
        _write(args.output, value)
        print(f"PRICE_ACTIVITY_CAPTURE=PASS symbols={len(value['records'])}")
        return 0
    if args.command == "rebuild":
        value = build_master()
        _write(args.output, value)
        print(f"PIT_INSTRUMENT_MASTER_REBUILD=PASS symbols={len(value['records'])}")
        return 0
    if args.command == "check":
        value = load_master()
        if args.verify_price_v1:
            replay = capture_price_activity(args.data_root)
            if replay != _load_json(ACTIVITY_PATH):
                raise InstrumentMasterError("Price V1 activity differs from live replay")
        print(
            "PIT_INSTRUMENT_MASTER_CHECK=PASS "
            f"symbols={len(value['records'])} quarantine={len(value['quarantine'])}"
        )
        return 0
    if args.command == "universe-at":
        value = load_master()
        symbols = universe_at(value, args.timestamp)
        print(
            json.dumps(
                {
                    "cohort_id": value["cohort"]["cohort_id"],
                    "as_of_utc": args.timestamp,
                    "symbol_count": len(symbols),
                    "symbols": symbols,
                },
                sort_keys=True,
            )
        )
        return 0
    raise InstrumentMasterError("unknown command")


if __name__ == "__main__":
    raise SystemExit(main())
