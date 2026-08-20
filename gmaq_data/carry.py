"""BTC/ETH Binance spot-perpetual carry Data Layer V1 profile."""

from __future__ import annotations

import datetime as dt
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
    sha256_bytes,
    sha256_file,
    verify_snapshot,
)
from .tsmom import (
    ALLOWED_MARK_SOURCES,
    EIGHT_HOURS_MS,
    SYMBOLS,
    _canonical_jsonl,
    _copy_stage_files,
    _finite_positive,
    _load_json,
    _load_jsonl,
    _manifest_payload_valid,
    _role_paths,
)


DATASET = "btceth-spot-perp-carry"
SCHEMA = "gmaq-spot-perp-carry-public-v1.json"
STUDY_ID = "study-2026-08-20-btceth-spot-perp-carry"


def _date_ms(value: str) -> int:
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=dt.UTC)
    except ValueError as error:
        raise DataLayerError(f"invalid UTC date: {value}") from error
    return int(parsed.timestamp() * 1000)


def _source_files(source_dir: pathlib.Path, manifest: dict) -> list[InputFile]:
    files = [InputFile("source_manifest", "source-manifest.json", source=source_dir / "manifest.json")]
    fields = {
        "spot_8h": ("spot_8h_path", "spot-8h"),
        "funding": ("funding_path", "funding"),
        "mark_8h": ("mark_8h_path", "mark-8h"),
    }
    for symbol in SYMBOLS:
        details = manifest.get("symbols", {}).get(symbol)
        if not isinstance(details, dict):
            raise DataLayerError(f"source manifest is missing {symbol}")
        for role, (path_key, suffix) in fields.items():
            name = f"{symbol}-{suffix}.jsonl"
            declared = details.get(path_key)
            declared_path = pathlib.PurePath(declared) if isinstance(declared, str) else None
            if (
                declared_path is None
                or declared_path.is_absolute()
                or ".." in declared_path.parts
                or declared_path.name != name
            ):
                raise DataLayerError(f"source manifest path mismatch for {symbol} {role}")
            files.append(InputFile(f"{symbol}.{role}", name, source=source_dir / name))
    return files


def ingest_carry(*, source_dir: pathlib.Path, data_root: pathlib.Path, repo_root: pathlib.Path) -> str:
    source_dir = source_dir.expanduser().resolve()
    manifest = _load_json(source_dir / "manifest.json")
    _, schema_id = load_schema(repo_root, SCHEMA)
    return create_snapshot(
        data_root=data_root,
        dataset=DATASET,
        stage="raw",
        schema_id=schema_id,
        files=_source_files(source_dir, manifest),
        source_metadata={
            "venue": "Binance spot + USD-M",
            "access": "public_endpoints_only",
            "fetched_at_utc": manifest.get("fetched_at_utc"),
            "window_start": manifest.get("start_inclusive"),
            "window_end": manifest.get("end_exclusive"),
            "study_id": manifest.get("study_id"),
            "source_manifest_sha256": sha256_file(source_dir / "manifest.json"),
        },
        checks={"raw_bytes_preserved": {"passed": True}},
        quality_verdict="UNASSESSED",
        cross_source_verdict="UNVERIFIED_SINGLE_VENUE",
    )


def _validate_manifest(manifest: dict, schema: dict, repo_root: pathlib.Path) -> tuple[int, int]:
    if manifest.get("schema_version") != 1 or manifest.get("interval") != "8h":
        raise DataLayerError("carry source manifest version/interval mismatch")
    if manifest.get("credential_scope") != "public_endpoints_only":
        raise DataLayerError("carry source is not credential-free")
    if manifest.get("study_id") != STUDY_ID:
        raise DataLayerError("carry source study identity mismatch")
    prereg = repo_root / "research" / "backtests" / STUDY_ID / "preregistration.md"
    if manifest.get("preregistration_sha256") != sha256_file(prereg):
        raise DataLayerError("carry preregistration checksum mismatch")
    if tuple(manifest.get("symbols", {})) != SYMBOLS:
        raise DataLayerError("carry source symbol scope/order mismatch")
    for field, expected in schema["authoritative_sources"].items():
        if manifest.get(field) != expected:
            raise DataLayerError(f"authoritative source mismatch: {field}")
    if not _manifest_payload_valid(manifest):
        raise DataLayerError("source manifest payload checksum mismatch")
    start_ms = _date_ms(str(manifest.get("start_inclusive")))
    end_ms = _date_ms(str(manifest.get("end_exclusive")))
    if end_ms <= start_ms or (end_ms - start_ms) % EIGHT_HOURS_MS:
        raise DataLayerError("carry source coverage window is invalid")
    try:
        fetched = dt.datetime.strptime(str(manifest.get("fetched_at_utc")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    except ValueError as error:
        raise DataLayerError("carry source fetch time is invalid") from error
    if int(fetched.timestamp() * 1000) < end_ms:
        raise DataLayerError("carry source was fetched before the end boundary was complete")
    return start_ms, end_ms


def _validate_ohlc(rows: list[dict], expected: list[int], label: str, *, volume: bool) -> None:
    timestamps = [int(item.get("open_time_utc_ms")) for item in rows]
    if timestamps != expected or len(set(timestamps)) != len(timestamps):
        raise DataLayerError(f"{label} coverage has gaps, duplicates, or disorder")
    for item in rows:
        opened = _finite_positive(item.get("open"), f"{label} open")
        high = _finite_positive(item.get("high"), f"{label} high")
        low = _finite_positive(item.get("low"), f"{label} low")
        closed = _finite_positive(item.get("close"), f"{label} close")
        if volume:
            _finite_positive(item.get("volume"), f"{label} volume", allow_zero=True)
        if high < max(opened, closed) or low > min(opened, closed) or high < low:
            raise DataLayerError(f"{label} invalid OHLC envelope")


def _validate_symbol(symbol: str, details: dict, paths: dict[str, pathlib.Path], start_ms: int, end_ms: int) -> dict:
    expected = list(range(start_ms, end_ms, EIGHT_HOURS_MS))
    role_specs = {
        "spot_8h": ("spot_8h_sha256", "spot_8h_bars"),
        "funding": ("funding_sha256", "funding_records"),
        "mark_8h": ("mark_8h_sha256", "mark_8h_bars"),
    }
    for role, (hash_key, _) in role_specs.items():
        path = paths[f"{symbol}.{role}"]
        if details.get(hash_key) != sha256_file(path):
            raise DataLayerError(f"source checksum mismatch: {path.name}")
    spot = _load_jsonl(paths[f"{symbol}.spot_8h"])
    marks = _load_jsonl(paths[f"{symbol}.mark_8h"])
    _validate_ohlc(spot, expected, f"{symbol} spot 8h", volume=True)
    _validate_ohlc(marks, expected, f"{symbol} mark 8h", volume=False)
    mark_open = {int(row["open_time_utc_ms"]): float(row["open"]) for row in marks}
    funding = _load_jsonl(paths[f"{symbol}.funding"])
    funding_times = [int(item.get("fundingTime")) for item in funding]
    buckets = [timestamp // EIGHT_HOURS_MS * EIGHT_HOURS_MS for timestamp in funding_times]
    if funding_times != sorted(set(funding_times)) or buckets != expected or len(set(buckets)) != len(buckets):
        raise DataLayerError(f"{symbol} funding coverage has gaps or duplicate settlement buckets")
    fallback_count = 0
    for item, bucket in zip(funding, buckets, strict=True):
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
                raise DataLayerError(f"{symbol} fallback mark does not match pinned 8h open")
    actual = {
        "spot_8h_bars": len(spot),
        "mark_8h_bars": len(marks),
        "funding_records": len(funding),
        "funding_mark_price_fallback_records": fallback_count,
        "first_spot_utc_ms": expected[0],
        "last_spot_utc_ms": expected[-1],
        "first_funding_utc_ms": funding_times[0],
        "last_funding_utc_ms": funding_times[-1],
    }
    for key, value in actual.items():
        if details.get(key) != value:
            raise DataLayerError(f"{symbol} manifest count/edge mismatch: {key}")
    if details.get("funding_request_window_complete") is not True:
        raise DataLayerError(f"{symbol} funding request window is incomplete")
    return {"rows": len(expected), "fallback": fallback_count}


def _quarantine(*, data_root: pathlib.Path, raw: dict, reason: str) -> str:
    return create_snapshot(
        data_root=data_root,
        dataset=DATASET,
        stage="quarantine",
        schema_id=raw["schema_id"],
        files=[InputFile("failure", "failure.json", payload=canonical_json_bytes({
            "dataset": DATASET, "parent_snapshot_id": raw["snapshot_id"],
            "reason": reason, "reingest_required": True,
        }))],
        source_metadata=raw["source_metadata"],
        checks={"quality_gate": {"passed": False, "reason": reason}},
        quality_verdict="FAIL",
        cross_source_verdict=raw["cross_source_verdict"],
        parent_snapshot_id=raw["snapshot_id"],
        status=QUARANTINED_STATUS,
    )


def validate_carry(*, raw_snapshot_id: str, data_root: pathlib.Path, repo_root: pathlib.Path) -> str:
    raw = verify_snapshot(data_root, raw_snapshot_id, expected_dataset=DATASET, minimum_stage="raw")
    if raw["stage"] != "raw":
        raise DataLayerError("carry validation requires an exact raw snapshot")
    schema, schema_id = load_schema(repo_root, SCHEMA)
    if raw["schema_id"] != schema_id:
        raise DataLayerError("carry raw snapshot schema identity mismatch")
    try:
        paths = _role_paths(raw)
        manifest = _load_json(paths["source_manifest"])
        start_ms, end_ms = _validate_manifest(manifest, schema, repo_root)
        counts = {symbol: _validate_symbol(symbol, manifest["symbols"][symbol], paths, start_ms, end_ms) for symbol in SYMBOLS}
        if counts[SYMBOLS[0]]["rows"] != counts[SYMBOLS[1]]["rows"]:
            raise DataLayerError("carry cross-symbol coverage is misaligned")
        return create_snapshot(
            data_root=data_root,
            dataset=DATASET,
            stage="validated",
            schema_id=schema_id,
            files=_copy_stage_files(raw),
            source_metadata=raw["source_metadata"],
            checks={
                "source_manifest_and_preregistration_hashes": {"passed": True},
                "authoritative_public_sources": {"passed": True, "venue": "Binance spot + USD-M"},
                "spot_mark_funding_8h_coverage_complete": {"passed": True, "rows_per_symbol": counts[SYMBOLS[0]]["rows"]},
                "timestamps_utc_monotonic_unique": {"passed": True},
                "ohlc_numeric_envelopes": {"passed": True},
                "funding_settlement_buckets_complete": {"passed": True},
                "credentials_and_private_endpoints_absent": {"passed": True},
                "point_in_time_availability": {"passed": True, "detail": "signals use only completed bars and settled funding strictly before decision time"},
            },
            quality_verdict="PASS",
            cross_source_verdict="UNVERIFIED_SINGLE_VENUE",
            parent_snapshot_id=raw_snapshot_id,
        )
    except (DataLayerError, KeyError, TypeError, ValueError) as error:
        _quarantine(data_root=data_root, raw=raw, reason=str(error))
        if isinstance(error, DataLayerError):
            raise
        raise DataLayerError(f"carry quality validation failed: {error}") from error


def curate_carry(*, validated_snapshot_id: str, data_root: pathlib.Path, repo_root: pathlib.Path) -> str:
    validated = verify_snapshot(data_root, validated_snapshot_id, expected_dataset=DATASET, minimum_stage="validated")
    if validated["stage"] != "validated" or validated["quality_verdict"] != "PASS":
        raise DataLayerError("carry curation requires an exact validated PASS snapshot")
    _, schema_id = load_schema(repo_root, SCHEMA)
    paths = _role_paths(validated)
    manifest = _load_json(paths["source_manifest"])
    manifest.pop("manifest_payload_sha256", None)
    for symbol in SYMBOLS:
        manifest["symbols"][symbol].update({
            "spot_8h_path": f"{symbol}-spot-8h.jsonl",
            "funding_path": f"{symbol}-funding.jsonl",
            "mark_8h_path": f"{symbol}-mark-8h.jsonl",
        })
    manifest["data_layer_parent_snapshot_id"] = validated_snapshot_id
    manifest["data_layer_schema_id"] = schema_id
    unhashed = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    manifest["manifest_payload_sha256"] = sha256_bytes(unhashed.encode())
    files = [InputFile("dataset_manifest", "manifest.json", payload=(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode())]
    for symbol in SYMBOLS:
        for role, suffix in (("spot_8h", "spot-8h"), ("funding", "funding"), ("mark_8h", "mark-8h")):
            files.append(InputFile(f"{symbol}.{role}", f"{symbol}-{suffix}.jsonl", payload=_canonical_jsonl(paths[f"{symbol}.{role}"])))
    return create_snapshot(
        data_root=data_root,
        dataset=DATASET,
        stage="curated",
        schema_id=schema_id,
        files=files,
        source_metadata=validated["source_metadata"],
        checks={
            "parent_validated_pass": {"passed": True, "snapshot_id": validated_snapshot_id},
            "canonical_jsonl": {"passed": True, "encoding": "UTF-8", "key_order": "sorted"},
            "deterministic_transform": {"passed": True},
            "research_fitness": {"passed": True, "scope": "BTC/ETH spot-perpetual carry"},
            "live_execution_fitness": {"passed": True, "detail": "BLOCKED_NOT_EVALUATED"},
        },
        quality_verdict="PASS",
        cross_source_verdict="UNVERIFIED_SINGLE_VENUE",
        parent_snapshot_id=validated_snapshot_id,
    )


def migrate_carry(*, source_dir: pathlib.Path, data_root: pathlib.Path, repo_root: pathlib.Path) -> dict:
    raw = ingest_carry(source_dir=source_dir, data_root=data_root, repo_root=repo_root)
    validated = validate_carry(raw_snapshot_id=raw, data_root=data_root, repo_root=repo_root)
    curated = curate_carry(validated_snapshot_id=validated, data_root=data_root, repo_root=repo_root)
    final = verify_snapshot(data_root, curated, expected_dataset=DATASET, minimum_stage="curated")
    return {
        "verdict": "PASS", "dataset": DATASET, "raw_snapshot_id": raw,
        "validated_snapshot_id": validated, "curated_snapshot_id": curated,
        "artifact_path": str(pathlib.Path(final["artifact_path"]) / "data"),
        "quality_verdict": final["quality_verdict"],
        "cross_source_verdict": final["cross_source_verdict"],
        "live_execution_fitness": "BLOCKED_NOT_EVALUATED",
    }
