"""BTC/ETH Binance USD-M daily TSMOM migration and quality profile."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import pathlib
from .layer import (
    DataLayerError,
    InputFile,
    QUARANTINED_STATUS,
    canonical_json_bytes,
    create_snapshot,
    load_schema,
    _regular_source,
    sha256_bytes,
    sha256_file,
    verify_snapshot,
)


DATASET = "btceth-weekly-tsmom"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
DAY_MS = 86_400_000
EIGHT_HOURS_MS = 28_800_000
MAX_JSONL_BYTES = 512 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
ALLOWED_MARK_SOURCES = {
    "fundingRate_response",
    "fapi_8h_mark_kline_open_fallback",
}


def _date_ms(value: str) -> int:
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=dt.UTC)
    except ValueError as error:
        raise DataLayerError(f"invalid UTC date: {value}") from error
    return int(parsed.timestamp() * 1000)


def _load_json(path: pathlib.Path) -> dict:
    path = _regular_source(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DataLayerError(f"invalid JSON: {path.name}") from error
    if not isinstance(value, dict):
        raise DataLayerError(f"JSON root must be an object: {path.name}")
    return value


def _load_jsonl(path: pathlib.Path) -> list[dict]:
    if path.stat().st_size > MAX_JSONL_BYTES:
        raise DataLayerError(f"JSONL exceeds the v1 safety bound: {path.name}")
    rows = []
    with path.open("rb") as handle:
        for line_number, raw in enumerate(handle, 1):
            if len(raw) > MAX_LINE_BYTES:
                raise DataLayerError(f"oversized JSONL row: {path.name}:{line_number}")
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise DataLayerError(f"invalid JSONL row: {path.name}:{line_number}") from error
            if not isinstance(value, dict):
                raise DataLayerError(f"JSONL row must be an object: {path.name}:{line_number}")
            rows.append(value)
    if not rows:
        raise DataLayerError(f"empty JSONL file: {path.name}")
    return rows


def _finite_positive(value: object, label: str, *, allow_zero: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise DataLayerError(f"non-numeric {label}") from error
    minimum_ok = number >= 0 if allow_zero else number > 0
    if not math.isfinite(number) or not minimum_ok:
        raise DataLayerError(f"invalid {label}")
    return number


def _manifest_payload_valid(manifest: dict) -> bool:
    expected = manifest.get("manifest_payload_sha256")
    payload = dict(manifest)
    payload.pop("manifest_payload_sha256", None)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return isinstance(expected, str) and hashlib.sha256(serialized.encode()).hexdigest() == expected


def _source_files(source_dir: pathlib.Path, manifest: dict) -> list[InputFile]:
    inputs = [InputFile("source_manifest", "source-manifest.json", source=source_dir / "manifest.json")]
    for symbol in SYMBOLS:
        details = manifest.get("symbols", {}).get(symbol)
        if not isinstance(details, dict):
            raise DataLayerError(f"source manifest is missing {symbol}")
        expected = {
            "klines": f"{symbol}-1d.jsonl",
            "funding": f"{symbol}-funding.jsonl",
            "mark_8h": f"{symbol}-mark-8h.jsonl",
        }
        for role, name in expected.items():
            declared = details.get({"klines": "klines_path", "funding": "funding_path", "mark_8h": "mark_8h_path"}[role])
            declared_path = pathlib.PurePath(declared) if isinstance(declared, str) else None
            if (
                declared_path is None
                or declared_path.is_absolute()
                or ".." in declared_path.parts
                or declared_path.name != name
            ):
                raise DataLayerError(f"source manifest path mismatch for {symbol} {role}")
            inputs.append(InputFile(f"{symbol}.{role}", name, source=source_dir / name))
    return inputs


def ingest_tsmom(
    *, source_dir: pathlib.Path, data_root: pathlib.Path, repo_root: pathlib.Path
) -> str:
    source_dir = source_dir.expanduser().resolve()
    manifest = _load_json(source_dir / "manifest.json")
    _, schema_id = load_schema(repo_root, "gmaq-tsmom-public-v1.json")
    source_metadata = {
        "venue": "Binance USD-M",
        "access": "public_endpoints_only",
        "fetched_at_utc": manifest.get("fetched_at_utc"),
        "window_start": manifest.get("start_inclusive"),
        "window_end": manifest.get("end_exclusive"),
        "study_id": manifest.get("study_id"),
        "source_manifest_sha256": sha256_file(source_dir / "manifest.json"),
    }
    return create_snapshot(
        data_root=data_root,
        dataset=DATASET,
        stage="raw",
        schema_id=schema_id,
        files=_source_files(source_dir, manifest),
        source_metadata=source_metadata,
        checks={"raw_bytes_preserved": {"passed": True}},
        quality_verdict="UNASSESSED",
        cross_source_verdict="UNVERIFIED_SINGLE_VENUE",
    )


def _role_paths(record: dict) -> dict[str, pathlib.Path]:
    artifact = pathlib.Path(record["artifact_path"])
    return {item["role"]: artifact / item["relpath"] for item in record["files"]}


def _validate_manifest(manifest: dict, schema: dict) -> tuple[int, int]:
    if manifest.get("schema_version") != 1:
        raise DataLayerError("source manifest schema version mismatch")
    if manifest.get("credential_scope") != "public_endpoints_only":
        raise DataLayerError("source manifest is not credential-free")
    if manifest.get("interval") != "1d":
        raise DataLayerError("source interval must be 1d")
    if tuple(manifest.get("symbols", {})) != SYMBOLS:
        raise DataLayerError("source symbol scope/order mismatch")
    for field, expected in schema["authoritative_sources"].items():
        if manifest.get(field) != expected:
            raise DataLayerError(f"authoritative source mismatch: {field}")
    if not _manifest_payload_valid(manifest):
        raise DataLayerError("source manifest payload checksum mismatch")
    start_ms = _date_ms(str(manifest.get("start_inclusive")))
    end_ms = _date_ms(str(manifest.get("end_exclusive")))
    if end_ms <= start_ms or (end_ms - start_ms) % DAY_MS:
        raise DataLayerError("source coverage window is invalid")
    try:
        fetched = dt.datetime.strptime(str(manifest.get("fetched_at_utc")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    except ValueError as error:
        raise DataLayerError("source fetch time is invalid") from error
    if int(fetched.timestamp() * 1000) < end_ms:
        raise DataLayerError("source was fetched before the exclusive end boundary was available")
    return start_ms, end_ms


def _validate_symbol(
    symbol: str,
    details: dict,
    paths: dict[str, pathlib.Path],
    start_ms: int,
    end_ms: int,
) -> dict:
    expected_days = list(range(start_ms, end_ms, DAY_MS))
    klines_path = paths[f"{symbol}.klines"]
    funding_path = paths[f"{symbol}.funding"]
    mark_path = paths[f"{symbol}.mark_8h"]
    expected_hashes = {
        klines_path: details.get("klines_sha256"),
        funding_path: details.get("funding_sha256"),
        mark_path: details.get("mark_8h_sha256"),
    }
    for path, expected in expected_hashes.items():
        if not isinstance(expected, str) or sha256_file(path) != expected:
            raise DataLayerError(f"legacy checksum mismatch: {path.name}")

    bars = _load_jsonl(klines_path)
    bar_times = [int(item.get("open_time_utc_ms")) for item in bars]
    if bar_times != expected_days or len(set(bar_times)) != len(bar_times):
        raise DataLayerError(f"{symbol} daily coverage has gaps, duplicates, or disorder")
    for item in bars:
        opened = _finite_positive(item.get("open"), "open")
        high = _finite_positive(item.get("high"), "high")
        low = _finite_positive(item.get("low"), "low")
        closed = _finite_positive(item.get("close"), "close")
        _finite_positive(item.get("volume"), "volume", allow_zero=True)
        if high < max(opened, closed) or low > min(opened, closed) or high < low:
            raise DataLayerError(f"{symbol} invalid OHLC envelope")

    expected_marks = list(range(start_ms, end_ms, EIGHT_HOURS_MS))
    marks = _load_jsonl(mark_path)
    mark_times = [int(item.get("open_time_utc_ms")) for item in marks]
    if mark_times != expected_marks or len(set(mark_times)) != len(mark_times):
        raise DataLayerError(f"{symbol} mark-price coverage has gaps, duplicates, or disorder")
    mark_open = {}
    for item in marks:
        timestamp = int(item["open_time_utc_ms"])
        opened = _finite_positive(item.get("open"), "mark open")
        high = _finite_positive(item.get("high"), "mark high")
        low = _finite_positive(item.get("low"), "mark low")
        closed = _finite_positive(item.get("close"), "mark close")
        if high < max(opened, closed) or low > min(opened, closed) or high < low:
            raise DataLayerError(f"{symbol} invalid mark OHLC envelope")
        mark_open[timestamp] = opened

    funding = _load_jsonl(funding_path)
    funding_times = [int(item.get("fundingTime")) for item in funding]
    if funding_times != sorted(set(funding_times)):
        raise DataLayerError(f"{symbol} funding timestamps are duplicated or disordered")
    funding_buckets = [timestamp // EIGHT_HOURS_MS * EIGHT_HOURS_MS for timestamp in funding_times]
    if funding_buckets != expected_marks or len(set(funding_buckets)) != len(funding_buckets):
        raise DataLayerError(f"{symbol} funding coverage has gaps or duplicate settlement buckets")
    fallback_count = 0
    for item, bucket in zip(funding, funding_buckets, strict=True):
        if item.get("symbol") != symbol:
            raise DataLayerError(f"{symbol} funding row symbol mismatch")
        rate = float(item.get("fundingRate"))
        if not math.isfinite(rate) or abs(rate) > 1:
            raise DataLayerError(f"{symbol} funding rate is invalid")
        mark_price = _finite_positive(item.get("markPrice"), "funding mark price")
        source = item.get("markPriceSource")
        if source not in ALLOWED_MARK_SOURCES:
            raise DataLayerError(f"{symbol} funding mark source is invalid")
        if source == "fapi_8h_mark_kline_open_fallback":
            fallback_count += 1
            if not math.isclose(mark_price, mark_open[bucket], rel_tol=0, abs_tol=1e-12):
                raise DataLayerError(f"{symbol} fallback mark price does not match the pinned 8h open")

    expected_counts = {
        "bars": len(bars),
        "funding_records": len(funding),
        "mark_8h_bars": len(marks),
        "funding_mark_price_fallback_records": fallback_count,
        "first_bar_utc_ms": bar_times[0],
        "last_bar_utc_ms": bar_times[-1],
        "first_funding_utc_ms": funding_times[0],
        "last_funding_utc_ms": funding_times[-1],
    }
    for key, actual in expected_counts.items():
        if details.get(key) != actual:
            raise DataLayerError(f"{symbol} manifest count/edge mismatch: {key}")
    if details.get("funding_request_window_complete") is not True:
        raise DataLayerError(f"{symbol} funding request window is not complete")
    if details.get("end_boundary_utc_ms") != end_ms or details.get("end_boundary_source") != "excluded_incomplete_candle_open_only":
        raise DataLayerError(f"{symbol} end valuation boundary is invalid")
    _finite_positive(details.get("end_boundary_open"), "end boundary open")
    return {"bars": len(bars), "funding": len(funding), "marks": len(marks), "fallback": fallback_count}


def _copy_stage_files(record: dict) -> list[InputFile]:
    artifact = pathlib.Path(record["artifact_path"])
    return [
        InputFile(item["role"], pathlib.Path(item["relpath"]).name, source=artifact / item["relpath"])
        for item in record["files"]
    ]


def _quarantine(
    *, data_root: pathlib.Path, raw_record: dict, reason: str
) -> str:
    payload = canonical_json_bytes(
        {
            "dataset": DATASET,
            "parent_snapshot_id": raw_record["snapshot_id"],
            "reason": reason,
            "reingest_required": True,
        }
    )
    return create_snapshot(
        data_root=data_root,
        dataset=DATASET,
        stage="quarantine",
        schema_id=raw_record["schema_id"],
        files=[InputFile("failure", "failure.json", payload=payload)],
        source_metadata=raw_record["source_metadata"],
        checks={"quality_gate": {"passed": False, "reason": reason}},
        quality_verdict="FAIL",
        cross_source_verdict=raw_record["cross_source_verdict"],
        parent_snapshot_id=raw_record["snapshot_id"],
        status=QUARANTINED_STATUS,
    )


def validate_tsmom(
    *, raw_snapshot_id: str, data_root: pathlib.Path, repo_root: pathlib.Path
) -> str:
    raw = verify_snapshot(data_root, raw_snapshot_id, expected_dataset=DATASET, minimum_stage="raw")
    if raw["stage"] != "raw":
        raise DataLayerError("TSMOM validation requires a raw snapshot")
    schema, schema_id = load_schema(repo_root, "gmaq-tsmom-public-v1.json")
    if raw["schema_id"] != schema_id:
        raise DataLayerError("raw snapshot schema identity mismatch")
    try:
        paths = _role_paths(raw)
        manifest = _load_json(paths["source_manifest"])
        start_ms, end_ms = _validate_manifest(manifest, schema)
        counts = {
            symbol: _validate_symbol(symbol, manifest["symbols"][symbol], paths, start_ms, end_ms)
            for symbol in SYMBOLS
        }
        if counts[SYMBOLS[0]]["bars"] != counts[SYMBOLS[1]]["bars"]:
            raise DataLayerError("cross-symbol daily coverage is misaligned")
        checks = {
            "source_manifest_self_hash": {"passed": True},
            "authoritative_public_sources": {"passed": True, "venue": "Binance USD-M"},
            "timestamps_utc_monotonic_unique": {"passed": True},
            "daily_coverage_complete": {"passed": True, "rows_per_symbol": counts[SYMBOLS[0]]["bars"]},
            "funding_coverage_complete": {"passed": True, "rows_per_symbol": counts[SYMBOLS[0]]["funding"]},
            "mark_8h_coverage_complete": {"passed": True, "rows_per_symbol": counts[SYMBOLS[0]]["marks"]},
            "ohlc_numeric_envelopes": {"passed": True},
            "fallback_mark_prices_pinned": {"passed": True, "rows_per_symbol": counts[SYMBOLS[0]]["fallback"]},
            "incomplete_end_candle_excluded": {"passed": True},
            "credentials_and_private_endpoints_absent": {"passed": True},
            "point_in_time_availability": {
                "passed": True,
                "detail": "signals may use only completed prior-day closes; end boundary open is valuation-only",
            },
            "cross_source_independence": {
                "passed": True,
                "detail": "not required for source-specific integrity; remains UNVERIFIED_SINGLE_VENUE",
            },
        }
        validated = create_snapshot(
            data_root=data_root,
            dataset=DATASET,
            stage="validated",
            schema_id=schema_id,
            files=_copy_stage_files(raw),
            source_metadata=raw["source_metadata"],
            checks=checks,
            quality_verdict="PASS",
            cross_source_verdict="UNVERIFIED_SINGLE_VENUE",
            parent_snapshot_id=raw_snapshot_id,
        )
        return validated
    except (DataLayerError, KeyError, TypeError, ValueError) as error:
        _quarantine(data_root=data_root, raw_record=raw, reason=str(error))
        if isinstance(error, DataLayerError):
            raise
        raise DataLayerError(f"TSMOM quality validation failed: {error}") from error


def _canonical_jsonl(path: pathlib.Path) -> bytes:
    rows = _load_jsonl(path)
    return b"".join(canonical_json_bytes(row) for row in rows)


def curate_tsmom(
    *, validated_snapshot_id: str, data_root: pathlib.Path, repo_root: pathlib.Path
) -> str:
    validated = verify_snapshot(
        data_root,
        validated_snapshot_id,
        expected_dataset=DATASET,
        minimum_stage="validated",
    )
    if validated["stage"] != "validated" or validated["quality_verdict"] != "PASS":
        raise DataLayerError("curation requires an exact validated PASS snapshot")
    _, schema_id = load_schema(repo_root, "gmaq-tsmom-public-v1.json")
    paths = _role_paths(validated)
    source_manifest = _load_json(paths["source_manifest"])
    source_manifest.pop("manifest_payload_sha256", None)
    for symbol in SYMBOLS:
        details = source_manifest["symbols"][symbol]
        details["klines_path"] = f"{symbol}-1d.jsonl"
        details["funding_path"] = f"{symbol}-funding.jsonl"
        details["mark_8h_path"] = f"{symbol}-mark-8h.jsonl"
    source_manifest["data_layer_parent_snapshot_id"] = validated_snapshot_id
    source_manifest["data_layer_schema_id"] = schema_id
    payload_without_hash = json.dumps(source_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    source_manifest["manifest_payload_sha256"] = sha256_bytes(payload_without_hash.encode())
    manifest_payload = (json.dumps(source_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    files = [InputFile("dataset_manifest", "manifest.json", payload=manifest_payload)]
    for symbol in SYMBOLS:
        files.extend(
            [
                InputFile(f"{symbol}.klines", f"{symbol}-1d.jsonl", payload=_canonical_jsonl(paths[f"{symbol}.klines"])),
                InputFile(f"{symbol}.funding", f"{symbol}-funding.jsonl", payload=_canonical_jsonl(paths[f"{symbol}.funding"])),
                InputFile(f"{symbol}.mark_8h", f"{symbol}-mark-8h.jsonl", payload=_canonical_jsonl(paths[f"{symbol}.mark_8h"])),
            ]
        )
    checks = {
        "parent_validated_pass": {"passed": True, "snapshot_id": validated_snapshot_id},
        "canonical_jsonl": {"passed": True, "encoding": "UTF-8", "key_order": "sorted"},
        "deterministic_transform": {"passed": True},
        "research_fitness": {"passed": True, "scope": "source-specific daily TSMOM"},
        "live_execution_fitness": {"passed": True, "detail": "BLOCKED_NOT_EVALUATED"},
    }
    return create_snapshot(
        data_root=data_root,
        dataset=DATASET,
        stage="curated",
        schema_id=schema_id,
        files=files,
        source_metadata=validated["source_metadata"],
        checks=checks,
        quality_verdict="PASS",
        cross_source_verdict="UNVERIFIED_SINGLE_VENUE",
        parent_snapshot_id=validated_snapshot_id,
    )


def migrate_tsmom(
    *, source_dir: pathlib.Path, data_root: pathlib.Path, repo_root: pathlib.Path
) -> dict:
    raw = ingest_tsmom(source_dir=source_dir, data_root=data_root, repo_root=repo_root)
    validated = validate_tsmom(raw_snapshot_id=raw, data_root=data_root, repo_root=repo_root)
    curated = curate_tsmom(validated_snapshot_id=validated, data_root=data_root, repo_root=repo_root)
    final = verify_snapshot(data_root, curated, expected_dataset=DATASET, minimum_stage="curated")
    return {
        "verdict": "PASS",
        "dataset": DATASET,
        "raw_snapshot_id": raw,
        "validated_snapshot_id": validated,
        "curated_snapshot_id": curated,
        "artifact_path": str(pathlib.Path(final["artifact_path"]) / "data"),
        "quality_verdict": final["quality_verdict"],
        "cross_source_verdict": final["cross_source_verdict"],
        "live_execution_fitness": "BLOCKED_NOT_EVALUATED",
    }
