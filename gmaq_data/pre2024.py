"""Fail-closed price-only V1 for the pre-2024 archive-extended exploration set.

The builder never downloads, repairs, interpolates, or silently excludes data.
It validates every local input before creating its first registry snapshot.  A
non-zero quarantine therefore cannot leave a FAIL snapshot in the registry.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import pathlib
import stat
import zipfile
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .layer import DataLayerError, InputFile, create_snapshot, load_schema, sha256_bytes, sha256_file, verify_snapshot


REFERENCE_DATASET_DIR = pathlib.Path(
    "/Users/ASUS/Desktop/gmaq-data/snapshots/btceth-weekly-tsmom/curated/"
    "88d9ff34d0e871c4e395730e7584a828448ca62c376005c15ce7f2233c7bf615/data"
)
DEFAULT_CANDIDATES_PATH = pathlib.Path(__file__).resolve().parents[1] / "research/data/pre2024-candidates.json"
DEFAULT_EXCLUSIONS_PATH = pathlib.Path(__file__).resolve().parents[1] / "research/data/pre2024-domain-exclusions.json"
DEFAULT_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
EXPECTED_RUN_ID = "acquisition-attempt-001"
EXPECTED_REPAIR_RUN_ID = "acquisition-attempt-001-repair-001"
PRESENT_STATUSES = frozenset(("ok", "cached", "replaced"))
LIQUIDITY_FLOOR = Decimal("5000000")
UNIVERSE_LOOKBACK_DAYS = 90
DATASET = "pre2024-usdm-archive-extended-1d"
DAY_MS = 86_400_000
MINUTE_MS = 60_000
LAST_ALLOWED_OPEN_MS = int(dt.datetime(2023, 12, 31, tzinfo=dt.UTC).timestamp() * 1000)
MAX_ZIP_BYTES = 8 * 1024 * 1024
MAX_UNCOMPRESSED_CSV_BYTES = 32 * 1024 * 1024
MAX_CSV_LINE_BYTES = 16 * 1024
CSV_COLUMNS = 12
SCHEMA_NAME = "gmaq-pre2024-price-v1.json"


def _regular_file(path: pathlib.Path, *, label: str) -> pathlib.Path:
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError as error:
        raise DataLayerError(f"{label} is unavailable: {path}") from error
    if path.is_symlink() or not stat.S_ISREG(mode):
        raise DataLayerError(f"{label} is not a regular non-symlink file: {path}")
    return path


def _inside(root: pathlib.Path, path: pathlib.Path, *, label: str) -> pathlib.Path:
    root = root.resolve()
    candidate = path if path.is_absolute() else root / path
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise DataLayerError(f"{label} escapes raw directory") from error
    if not relative.parts or ".." in relative.parts:
        raise DataLayerError(f"{label} escapes raw directory")
    cursor = candidate
    while cursor != root:
        if cursor.is_symlink():
            raise DataLayerError(f"{label} traverses a symlink")
        cursor = cursor.parent
    return candidate


def _load_json_object(path: pathlib.Path, *, label: str) -> dict[str, Any]:
    _regular_file(path, label=label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DataLayerError(f"invalid {label} JSON") from error
    if not isinstance(value, dict):
        raise DataLayerError(f"{label} JSON root must be an object")
    return value


def _parse_month(value: object, *, label: str) -> tuple[int, int]:
    if not isinstance(value, str):
        raise DataLayerError(f"{label} must be YYYY-MM")
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m")
    except ValueError as error:
        raise DataLayerError(f"{label} must be YYYY-MM") from error
    return parsed.year, parsed.month


def _date_start_ms(value: object, *, label: str) -> int:
    if not isinstance(value, str):
        raise DataLayerError(f"{label} must be YYYY-MM-DD")
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=dt.UTC)
    except ValueError as error:
        raise DataLayerError(f"{label} must be YYYY-MM-DD") from error
    return int(parsed.timestamp() * 1000)


def _month_for_ms(value: int) -> str:
    return dt.datetime.fromtimestamp(value / 1000, tz=dt.UTC).strftime("%Y-%m")


def _decimal(value: str, *, label: str) -> Decimal:
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as error:
        raise DataLayerError(f"invalid decimal {label}") from error
    if not result.is_finite():
        raise DataLayerError(f"non-finite decimal {label}")
    return result


def _read_zip_csv(path: pathlib.Path, *, interval_ms: int, expected_month: str) -> list[list[str]]:
    """Accept one bounded, unencrypted CSV member and validate basic shape."""
    _regular_file(path, label="kline ZIP")
    if path.stat().st_size > MAX_ZIP_BYTES:
        raise DataLayerError(f"kline ZIP exceeds size limit: {path.name}")
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) != 1:
                raise DataLayerError(f"kline ZIP must contain exactly one CSV: {path.name}")
            member = members[0]
            if (
                member.is_dir()
                or pathlib.PurePosixPath(member.filename).name != member.filename
                or not member.filename.endswith(".csv")
                or member.flag_bits & 0x1
                or member.file_size <= 0
                or member.file_size > MAX_UNCOMPRESSED_CSV_BYTES
            ):
                raise DataLayerError(f"unsafe kline ZIP member: {path.name}")
            payload = archive.read(member)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise DataLayerError(f"invalid kline ZIP: {path.name}") from error
    if len(payload) != member.file_size:
        raise DataLayerError(f"kline ZIP member size mismatch: {path.name}")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DataLayerError(f"kline CSV is not UTF-8: {path.name}") from error
    if any(len(line.encode("utf-8")) > MAX_CSV_LINE_BYTES for line in text.splitlines()):
        raise DataLayerError(f"kline CSV line exceeds size limit: {path.name}")
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as error:
        raise DataLayerError(f"invalid kline CSV: {path.name}") from error
    if rows and rows[0] and rows[0][0].strip().lower() == "open_time":
        if len(rows[0]) != CSV_COLUMNS:
            raise DataLayerError(f"invalid kline CSV header: {path.name}")
        rows = rows[1:]
        line_offset = 2
    else:
        line_offset = 1
    if not rows:
        raise DataLayerError(f"empty kline CSV: {path.name}")
    result = []
    for offset, row in enumerate(rows):
        if len(row) != CSV_COLUMNS or any(not item.strip() for item in row):
            raise DataLayerError(f"invalid kline CSV structure: {path.name}:{line_offset + offset}")
        try:
            opened, closed = int(row[0]), int(row[6])
        except ValueError as error:
            raise DataLayerError(f"invalid kline timestamp: {path.name}:{line_offset + offset}") from error
        last_open = LAST_ALLOWED_OPEN_MS + DAY_MS - interval_ms
        if opened < 0 or opened % interval_ms or opened > last_open:
            raise DataLayerError(f"kline timestamp outside UTC pre-2024 window: {path.name}:{line_offset + offset}")
        if closed != opened + interval_ms - 1:
            raise DataLayerError(f"invalid kline close timestamp: {path.name}:{line_offset + offset}")
        if _month_for_ms(opened) != expected_month:
            raise DataLayerError(f"kline row month differs from manifest: {path.name}:{line_offset + offset}")
        for index, name in ((1, "open"), (2, "high"), (3, "low"), (4, "close"), (5, "volume"), (7, "quote_volume")):
            _decimal(row[index], label=f"{name} at {path.name}:{line_offset + offset}")
        result.append(row)
    return result


def _present_kline_entries(canonical: Mapping[str, Any]) -> list[dict[str, str]]:
    entries = canonical.get("entries")
    if canonical.get("run_id") != EXPECTED_RUN_ID or not isinstance(entries, list):
        raise DataLayerError("canonical manifest is not acquisition-attempt-001")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in entries:
        if not isinstance(raw, dict) or raw.get("kind") != "kline":
            continue
        if raw.get("status") == "missing":
            continue
        if raw.get("status") not in PRESENT_STATUSES:
            raise DataLayerError("canonical manifest has unknown kline status")
        entry = {name: raw.get(name) for name in ("symbol", "month", "path", "sha256", "run_id", "key")}
        if any(not isinstance(value, str) or not value for value in entry.values()):
            raise DataLayerError("canonical manifest has malformed present kline entry")
        if entry["run_id"] != EXPECTED_RUN_ID or len(entry["sha256"]) != 64 or any(char not in "0123456789abcdef" for char in entry["sha256"]):
            raise DataLayerError("canonical manifest kline identity is invalid")
        _parse_month(entry["month"], label="canonical kline month")
        key = (entry["symbol"], entry["month"])
        if key in seen:
            raise DataLayerError("canonical manifest has duplicate present kline month")
        seen.add(key)
        result.append(entry)  # type: ignore[arg-type]
    if not result:
        raise DataLayerError("canonical manifest has no present kline ZIPs")
    return sorted(result, key=lambda entry: (entry["symbol"], entry["month"]))


def _repair_entries(raw_dir: pathlib.Path) -> tuple[pathlib.Path | None, list[dict[str, str]], list[dict[str, str]]]:
    """Return verified manifest declarations; bytes are checked before use."""
    path = raw_dir / "repair-manifest.json"
    if not path.exists() and not path.is_symlink():
        return None, [], []
    manifest = _load_json_object(path, label="repair manifest")
    if (
        manifest.get("run_id") != EXPECTED_REPAIR_RUN_ID
        or manifest.get("monthly_files_mutated") is not False
        or manifest.get("curated_1m_fields") is not False
    ):
        raise DataLayerError("repair manifest identity or scope is invalid")
    output: list[list[dict[str, str]]] = []
    seen: set[tuple[str, str, str]] = set()
    for category in ("missing_1d", "volume_validation_1m"):
        entries = manifest.get(category, [])
        if not isinstance(entries, list):
            raise DataLayerError(f"repair manifest {category} must be a list")
        normalized: list[dict[str, str]] = []
        for raw in entries:
            if not isinstance(raw, dict):
                raise DataLayerError("repair manifest entry must be an object")
            entry = {name: raw.get(name) for name in ("symbol", "date", "kind", "path", "sha256")}
            if any(not isinstance(value, str) or not value for value in entry.values()):
                raise DataLayerError("repair manifest entry is malformed")
            expected_kind = "1d" if category == "missing_1d" else "1m"
            if entry["kind"] != expected_kind:
                raise DataLayerError("repair manifest entry has wrong kind")
            _date_start_ms(entry["date"], label="repair date")
            if len(entry["sha256"]) != 64 or any(char not in "0123456789abcdef" for char in entry["sha256"]):
                raise DataLayerError("repair manifest SHA-256 is invalid")
            key = (category, entry["symbol"], entry["date"])
            if key in seen:
                raise DataLayerError("repair manifest has duplicate repair entry")
            seen.add(key)
            source = _inside(raw_dir, pathlib.Path(entry["path"]), label="repair path")
            _regular_file(source, label="repair ZIP")
            if sha256_file(source) != entry["sha256"]:
                raise DataLayerError(f"repair ZIP checksum mismatch: {entry['symbol']} {entry['date']}")
            normalized.append(entry)  # type: ignore[arg-type]
        output.append(normalized)
    return path, output[0], output[1]


def load_symbol_rows(
    raw_dir: pathlib.Path, canonical: Mapping[str, Any], *, missing_1d: list[dict[str, str]] | None = None
) -> dict[str, list[list[str]]]:
    """Load all manifest-present monthly rows and insert only manifest-bound repairs."""
    raw_dir = raw_dir.resolve()
    by_symbol: dict[str, list[list[str]]] = {}
    for entry in _present_kline_entries(canonical):
        source = _inside(raw_dir, pathlib.Path(entry["path"]), label="canonical kline path")
        _regular_file(source, label="canonical kline ZIP")
        if sha256_file(source) != entry["sha256"]:
            raise DataLayerError(f"canonical kline checksum mismatch: {entry['symbol']} {entry['month']}")
        by_symbol.setdefault(entry["symbol"], []).extend(_read_zip_csv(source, interval_ms=DAY_MS, expected_month=entry["month"]))
    for entry in missing_1d or []:
        date_ms = _date_start_ms(entry["date"], label="repair date")
        source = _inside(raw_dir, pathlib.Path(entry["path"]), label="repair path")
        rows = _read_zip_csv(source, interval_ms=DAY_MS, expected_month=entry["date"][:7])
        if len(rows) != 1 or int(rows[0][0]) != date_ms:
            raise DataLayerError(f"1d repair must contain exactly its declared UTC day: {entry['symbol']} {entry['date']}")
        by_symbol.setdefault(entry["symbol"], []).extend(rows)
    for rows in by_symbol.values():
        rows.sort(key=lambda row: int(row[0]))
    return by_symbol


def validate_symbol(rows: list[list[str]]) -> tuple[list[dict[str, Any]], list[str]]:
    problems: list[str] = []
    output: list[dict[str, Any]] = []
    seen: set[int] = set()
    previous: int | None = None
    for row in rows:
        try:
            opened = int(row[0])
            opened_decimal, high, low, closed = (_decimal(row[index], label="OHLC") for index in (1, 2, 3, 4))
            volume, quote = (_decimal(row[index], label="volume") for index in (5, 7))
        except (IndexError, ValueError, DataLayerError) as error:
            problems.append(f"malformed row: {error}")
            continue
        if opened in seen:
            problems.append(f"duplicate day {opened}")
            continue
        seen.add(opened)
        if opened % DAY_MS or opened > LAST_ALLOWED_OPEN_MS:
            problems.append(f"outside UTC daily pre-2024 window {opened}")
        if previous is not None and opened - previous != DAY_MS:
            problems.append(f"day gap between {previous} and {opened}")
        previous = opened
        if not (low > 0 and opened_decimal > 0 and high >= opened_decimal and high >= closed and low <= opened_decimal and low <= closed):
            problems.append(f"OHLC invariant violated at {opened}")
        if volume < 0 or quote < 0 or not (volume * low <= quote <= volume * high):
            problems.append(f"quote-volume invariant violated at {opened}")
        output.append({"open_time_utc_ms": opened, "open": row[1], "high": row[2], "low": row[3], "close": row[4], "quote_volume": row[7]})
    if not output:
        problems.append("no valid rows")
    return output, problems


def _validate_volume_repairs(
    *, raw_dir: pathlib.Path, repairs: list[dict[str, str]], rows_by_symbol: Mapping[str, list[list[str]]]
) -> None:
    """Verify 1m evidence and repair only invalid 1d volume fields in memory."""
    daily_index = {(symbol, int(row[0])): row for symbol, rows in rows_by_symbol.items() for row in rows}
    for entry in repairs:
        day_start = _date_start_ms(entry["date"], label="repair date")
        daily = daily_index.get((entry["symbol"], day_start))
        if daily is None:
            raise DataLayerError(f"1m repair has no corresponding 1d row: {entry['symbol']} {entry['date']}")
        source = _inside(raw_dir, pathlib.Path(entry["path"]), label="repair path")
        minutes = _read_zip_csv(source, interval_ms=MINUTE_MS, expected_month=entry["date"][:7])
        expected_times = list(range(day_start, day_start + DAY_MS, MINUTE_MS))
        if [int(row[0]) for row in minutes] != expected_times:
            raise DataLayerError(f"1m repair must contain 1440 continuous UTC minutes: {entry['symbol']} {entry['date']}")
        opened, high, low, closed = (_decimal(daily[index], label="daily OHLC") for index in (1, 2, 3, 4))
        original_base = _decimal(daily[5], label="daily volume")
        original_quote = _decimal(daily[7], label="daily quote_volume")
        if original_base * low <= original_quote <= original_base * high:
            raise DataLayerError(
                f"1m repair targets a 1d row that does not need repair: "
                f"{entry['symbol']} {entry['date']}")
        minute_open = _decimal(minutes[0][1], label="minute open")
        minute_high = max(_decimal(row[2], label="minute high") for row in minutes)
        minute_low = min(_decimal(row[3], label="minute low") for row in minutes)
        minute_close = _decimal(minutes[-1][4], label="minute close")
        if (minute_open, minute_high, minute_low, minute_close) != (opened, high, low, closed):
            raise DataLayerError(f"1m repair OHLC does not aggregate to 1d: {entry['symbol']} {entry['date']}")
        base_total = sum((_decimal(row[5], label="minute volume") for row in minutes), Decimal("0"))
        quote_total = sum((_decimal(row[7], label="minute quote_volume") for row in minutes), Decimal("0"))
        if not (base_total * low <= quote_total <= base_total * high):
            raise DataLayerError(f"1m repair aggregate quote-volume invariant fails: {entry['symbol']} {entry['date']}")
        # The archived 1d volume fields are the disputed fields.  Curated data
        # retains no base volume; quote volume is replaced only when the
        # checksum-verified 1m aggregate proves the archived 1d quote was bad.
        daily[5] = format(base_total, "f")
        daily[7] = format(quote_total, "f")


def _reference_rows(reference_dir: pathlib.Path, symbol: str) -> dict[int, dict[str, Any]]:
    path = _regular_file(reference_dir / f"{symbol}-1d.jsonl", label=f"{symbol} reference")
    output: dict[int, dict[str, Any]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise DataLayerError(f"cannot read {symbol} reference") from error
    for number, line in enumerate(lines, 1):
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError
            timestamp = int(value["open_time_utc_ms"])
            if timestamp in output:
                raise ValueError
            for field in ("open", "high", "low", "close"):
                _decimal(str(value[field]), label=f"reference {field}")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, DataLayerError) as error:
            raise DataLayerError(f"invalid {symbol} reference row {number}") from error
        output[timestamp] = value
    if not output:
        raise DataLayerError(f"empty {symbol} reference")
    return output


def cross_check_reference(validated: Mapping[str, list[dict[str, Any]]], *, reference_dir: pathlib.Path = REFERENCE_DATASET_DIR) -> dict[str, int]:
    """Require timestamp-set equality over the complete BTC/ETH overlap, then Decimal OHLC equality."""
    compared = 0
    for symbol in ("BTCUSDT", "ETHUSDT"):
        source = {int(row["open_time_utc_ms"]): row for row in validated.get(symbol, [])}
        if not source:
            raise DataLayerError(f"validated source lacks {symbol}")
        reference = _reference_rows(reference_dir, symbol)
        start, end = max(min(source), min(reference)), min(max(source), max(reference))
        if start > end:
            raise DataLayerError(f"{symbol} has no reference overlap")
        source_times = {timestamp for timestamp in source if start <= timestamp <= end}
        reference_times = {timestamp for timestamp in reference if start <= timestamp <= end}
        if source_times != reference_times:
            raise DataLayerError(f"{symbol} reference overlap timestamp set differs")
        for timestamp in sorted(source_times):
            for field in ("open", "high", "low", "close"):
                if _decimal(source[timestamp][field], label=field) != _decimal(str(reference[timestamp][field]), label=field):
                    raise DataLayerError(f"{symbol} reference OHLC mismatch at {timestamp}")
            compared += 1
    return {"compared_days": compared, "mismatches": 0}


def month_ends(first_ms: int, last_ms: int) -> list[int]:
    if first_ms > last_ms:
        raise DataLayerError("invalid PIT date window")
    current = dt.datetime.fromtimestamp(first_ms / 1000, tz=dt.UTC).date().replace(day=1)
    last = dt.datetime.fromtimestamp(last_ms / 1000, tz=dt.UTC).date().replace(day=1)
    output = []
    while current <= last:
        current = dt.date(current.year + (current.month == 12), current.month % 12 + 1, 1)
        output.append(int(dt.datetime.combine(current, dt.time.min, tzinfo=dt.UTC).timestamp() * 1000))
    return output


def build_pit_universe(validated: Mapping[str, list[dict[str, Any]]], first_ms: int, last_ms: int) -> list[dict[str, Any]]:
    """A 90-completed-UTC-bar median becomes eligible in the *following* month."""
    output: list[dict[str, Any]] = []
    series = {symbol: {int(row["open_time_utc_ms"]): _decimal(row["quote_volume"], label="quote_volume") for row in rows} for symbol, rows in validated.items()}
    for effective_month_start in month_ends(first_ms, last_ms):
        members = []
        expected_days = range(
            effective_month_start - UNIVERSE_LOOKBACK_DAYS * DAY_MS,
            effective_month_start,
            DAY_MS,
        )
        for symbol, points in series.items():
            if not all(timestamp in points for timestamp in expected_days):
                continue
            completed = [points[timestamp] for timestamp in expected_days]
            ordered = sorted(completed)
            if (ordered[44] + ordered[45]) / 2 >= LIQUIDITY_FLOOR:
                members.append(symbol)
        output.append({"effective_month_start_utc_ms": effective_month_start, "completed_bars": UNIVERSE_LOOKBACK_DAYS, "symbols": sorted(members)})
    return output


def _jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n" for row in rows)


def _source_index(entries: list[dict[str, str]], repairs: list[dict[str, str]]) -> bytes:
    return json.dumps(
        {
            "kline_zips": [{key: entry[key] for key in ("symbol", "month", "key", "sha256", "path")} for entry in entries],
            "repair_zips": [{key: entry[key] for key in ("symbol", "date", "kind", "sha256", "path")} for entry in repairs],
        }, sort_keys=True, separators=(",", ":")
    ).encode() + b"\n"


def _load_domain_exclusions(
    *, raw_dir: pathlib.Path, candidates_path: pathlib.Path,
    exclusions_path: pathlib.Path | None,
) -> tuple[set[str], dict[str, Any] | None]:
    if exclusions_path is None:
        return set(), None
    document = _load_json_object(exclusions_path, label="domain exclusions")
    if (
        document.get("schema_version") != 1
        or document.get("candidate_set_sha256") != sha256_file(candidates_path)
        or not isinstance(document.get("exclusions"), list)
    ):
        raise DataLayerError("domain exclusions do not bind the candidate set")
    symbols: set[str] = set()
    for raw in document["exclusions"]:
        if not isinstance(raw, dict):
            raise DataLayerError("domain exclusion entry must be an object")
        symbol, code = raw.get("symbol"), raw.get("code")
        if not isinstance(symbol, str) or not symbol or not isinstance(code, str) or not code:
            raise DataLayerError("domain exclusion identity is invalid")
        if symbol in symbols:
            raise DataLayerError("duplicate domain exclusion symbol")
        _date_start_ms(raw.get("gap_start"), label="exclusion gap start")
        _date_start_ms(raw.get("gap_end_inclusive"), label="exclusion gap end")
        evidence = raw.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise DataLayerError("domain exclusion lacks evidence")
        for item in evidence:
            if not isinstance(item, dict):
                raise DataLayerError("domain exclusion evidence must be an object")
            path_value, digest = item.get("path"), item.get("sha256")
            if not isinstance(path_value, str) or not isinstance(digest, str):
                raise DataLayerError("domain exclusion evidence is malformed")
            source = _inside(raw_dir, pathlib.Path(path_value), label="domain exclusion evidence")
            _regular_file(source, label="domain exclusion evidence")
            if sha256_file(source) != digest:
                raise DataLayerError(f"domain exclusion evidence checksum mismatch: {symbol}")
        symbols.add(symbol)
    return symbols, document


def _load_acquisition_contract(*, raw_dir: pathlib.Path, candidates_path: pathlib.Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, str]]]:
    canonical = _load_json_object(raw_dir / "canonical-manifest.json", label="canonical manifest")
    audit = _load_json_object(raw_dir / "audit-report.json", label="audit report")
    candidates = _load_json_object(candidates_path, label="candidate set")
    entries = _present_kline_entries(canonical)
    if audit.get("canonical_run_id") != EXPECTED_RUN_ID or audit.get("kline_verdict") != "PASS":
        raise DataLayerError("price V1 requires audit acquisition-attempt-001 kline_verdict=PASS")
    first_bar = candidates.get("first_bar_by_symbol")
    if not isinstance(first_bar, dict) or not first_bar:
        raise DataLayerError("candidate set lacks first_bar_by_symbol")
    if any(not isinstance(symbol, str) or not isinstance(month, str) for symbol, month in first_bar.items()):
        raise DataLayerError("candidate set has malformed symbol/month")
    for month in first_bar.values():
        _parse_month(month, label="candidate first-bar month")
    candidate_symbols, present_symbols = set(first_bar), {entry["symbol"] for entry in entries}
    if candidate_symbols != present_symbols:
        raise DataLayerError("candidate symbols and present kline symbols differ")
    first_months: dict[str, str] = {}
    for entry in entries:
        first_months.setdefault(entry["symbol"], entry["month"])
    if any(first_months[symbol] != month for symbol, month in first_bar.items()):
        raise DataLayerError("candidate first-bar months and canonical kline months differ")
    if audit.get("symbols_audited") != len(candidate_symbols):
        raise DataLayerError("audit symbol count does not bind candidate set")
    return canonical, audit, candidates, entries


def build_pre2024_v1(
    *, data_root: pathlib.Path, raw_dir: pathlib.Path, candidates_path: pathlib.Path = DEFAULT_CANDIDATES_PATH,
    reference_dir: pathlib.Path = REFERENCE_DATASET_DIR, repo_root: pathlib.Path = DEFAULT_REPO_ROOT,
    exclusions_path: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Build raw→validated→curated only once all archive evidence passes."""
    raw_dir, candidates_path, repo_root = raw_dir.resolve(), candidates_path.resolve(), repo_root.resolve()
    _, schema_id = load_schema(repo_root, SCHEMA_NAME)
    canonical, audit, candidates, entries = _load_acquisition_contract(raw_dir=raw_dir, candidates_path=candidates_path)
    if exclusions_path is None and candidates_path == DEFAULT_CANDIDATES_PATH.resolve():
        exclusions_path = DEFAULT_EXCLUSIONS_PATH
    excluded_symbols, exclusion_document = _load_domain_exclusions(
        raw_dir=raw_dir, candidates_path=candidates_path,
        exclusions_path=exclusions_path.resolve() if exclusions_path is not None else None,
    )
    unknown_exclusions = excluded_symbols - set(candidates["first_bar_by_symbol"])
    if unknown_exclusions:
        raise DataLayerError("domain exclusions contain symbols outside the candidate set")
    repair_path, missing_1d, volume_1m = _repair_entries(raw_dir)
    if repair_path is not None and exclusions_path is not None:
        repair_document = _load_json_object(repair_path, label="repair manifest")
        if (
            repair_document.get("domain_exclusions_sha256") != sha256_file(exclusions_path)
            or set(repair_document.get("excluded_symbols", [])) != excluded_symbols
        ):
            raise DataLayerError("repair manifest and domain exclusions are not hash-bound")
    rows_by_symbol = load_symbol_rows(raw_dir, canonical, missing_1d=missing_1d)
    _validate_volume_repairs(raw_dir=raw_dir, repairs=volume_1m, rows_by_symbol=rows_by_symbol)
    for symbol in sorted(excluded_symbols):
        _, exclusion_problems = validate_symbol(rows_by_symbol.get(symbol, []))
        if not any("day gap" in problem for problem in exclusion_problems):
            raise DataLayerError(f"domain exclusion is not supported by a trade-bar gap: {symbol}")
        rows_by_symbol.pop(symbol, None)
    validated: dict[str, list[dict[str, Any]]] = {}
    quarantined: dict[str, list[str]] = {}
    for symbol, rows in sorted(rows_by_symbol.items()):
        clean, problems = validate_symbol(rows)
        if problems:
            quarantined[symbol] = problems
        else:
            validated[symbol] = clean
    if quarantined:
        raise DataLayerError(f"pre2024 quarantine is nonzero: {len(quarantined)} symbols")
    expected_validated = set(candidates["first_bar_by_symbol"]) - excluded_symbols
    if set(validated) != expected_validated:
        raise DataLayerError("validated symbols and eligible candidate symbols differ")
    reference_check = cross_check_reference(validated, reference_dir=reference_dir)
    timestamps = [int(row["open_time_utc_ms"]) for rows in validated.values() for row in rows]
    pit = build_pit_universe(validated, min(timestamps), max(timestamps))
    if pit != build_pit_universe(validated, min(timestamps), max(timestamps)):
        raise DataLayerError("PIT universe replay is nondeterministic")
    repair_entries = missing_1d + volume_1m
    index_payload = _source_index(entries, repair_entries)

    # Everything above is pre-publication validation.  The registry remains untouched on failure.
    raw_inputs = [
        InputFile("canonical_manifest", "canonical-manifest.json", source=raw_dir / "canonical-manifest.json"),
        InputFile("audit_report", "audit-report.json", source=raw_dir / "audit-report.json"),
        InputFile("candidates", "pre2024-candidates.json", source=candidates_path),
        InputFile("source_index", "source-index.json", payload=index_payload),
    ]
    if repair_path is not None:
        raw_inputs.append(InputFile("repair_manifest", "repair-manifest.json", source=repair_path))
    if exclusions_path is not None:
        raw_inputs.append(InputFile(
            "domain_exclusions", "pre2024-domain-exclusions.json", source=exclusions_path))
    labels = ["archive-extended", "survivor-biased", "exploration-only"]
    raw_id = create_snapshot(
        data_root=data_root, dataset=DATASET, stage="raw", schema_id=schema_id, files=raw_inputs,
        source_metadata={"acquisition_run_id": EXPECTED_RUN_ID, "window_start": "2019-09", "window_end": "2023-12", "labels": labels},
        checks={"kline_audit": {"verdict": "PASS", "audit_canonical_run_id": audit["canonical_run_id"]}, "source_lineage": {"kline_zip_count": len(entries), "repair_zip_count": len(repair_entries), "index_sha256": sha256_bytes(index_payload)}, "domain_exclusions": {"count": len(excluded_symbols), "symbols": sorted(excluded_symbols)}},
        quality_verdict="PASS", cross_source_verdict="SINGLE_SOURCE_PUBLIC_ARCHIVE",
    )
    validated_inputs = [InputFile(f"kline.{symbol}", f"validated-{symbol}.jsonl", payload=_jsonl_bytes(rows)) for symbol, rows in sorted(validated.items())]
    validated_id = create_snapshot(
        data_root=data_root, dataset=DATASET, stage="validated", schema_id=schema_id, files=validated_inputs,
        parent_snapshot_id=raw_id, source_metadata={"window_start": "2019-09", "window_end": "2023-12", "labels": labels},
        checks={"quarantine_total": 0, "reference": reference_check}, quality_verdict="PASS", cross_source_verdict="SINGLE_SOURCE_PUBLIC_ARCHIVE",
    )
    summary = {"dataset": DATASET, "labels": labels, "symbols_curated": len(validated), "domain_excluded_symbols": sorted(excluded_symbols), "quarantined_symbols": [], "pit_universe_months": len(pit), "pit_universe_sizes": {dt.datetime.fromtimestamp(month["effective_month_start_utc_ms"] / 1000, tz=dt.UTC).strftime("%Y-%m"): len(month["symbols"]) for month in pit}}
    curated_id = create_snapshot(
        data_root=data_root, dataset=DATASET, stage="curated", schema_id=schema_id,
        files=validated_inputs + [InputFile("pit_universe", "pit-universe.jsonl", payload=_jsonl_bytes(pit)), InputFile("summary", "summary.json", payload=json.dumps(summary, sort_keys=True, separators=(",", ":")).encode() + b"\n")],
        parent_snapshot_id=validated_id,
        source_metadata={"window_start": "2019-09", "window_end": "2023-12", "liquidity_floor_usd": str(LIQUIDITY_FLOOR), "universe_lookback_completed_days": UNIVERSE_LOOKBACK_DAYS, "labels": labels},
        checks={"quarantine_total": 0, "pit_replay": {"deterministic": True, "months": len(pit)}}, quality_verdict="PASS", cross_source_verdict="SINGLE_SOURCE_PUBLIC_ARCHIVE",
    )
    record = verify_snapshot(data_root, curated_id, expected_dataset=DATASET, minimum_stage="curated")
    return {"raw_id": raw_id, "validated_id": validated_id, "curated_id": curated_id, "verified": record["integrity_verdict"], "summary": summary}
