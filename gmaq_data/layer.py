"""Immutable local snapshots and an append-only SQLite registry.

The warehouse deliberately lives outside a Git worktree by default.  Tracked
code and evidence retain snapshot IDs while the operational bytes remain local
and can be copied or backed up independently.  This module never reads exchange
credentials and has no order or account API surface.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import stat
import tempfile
import time
from collections.abc import Iterable, Iterator, Mapping


SCHEMA_VERSION = 1
STAGE_RANK = {"quarantine": -1, "raw": 0, "validated": 1, "curated": 2}
PASS_STATUS = "PASS"
QUARANTINED_STATUS = "QUARANTINED"


class DataLayerError(RuntimeError):
    """A fail-closed data-layer contract violation."""


@dataclasses.dataclass(frozen=True)
class InputFile:
    role: str
    name: str
    source: pathlib.Path | None = None
    payload: bytes | None = None

    def __post_init__(self) -> None:
        if (self.source is None) == (self.payload is None):
            raise ValueError("exactly one of source or payload is required")
        if pathlib.PurePath(self.name).name != self.name or self.name in {"", ".", ".."}:
            raise ValueError("snapshot file names must be flat and traversal-free")


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def default_data_root(repo_root: pathlib.Path) -> pathlib.Path:
    configured = os.environ.get("GMAQ_DATA_ROOT")
    if configured:
        return pathlib.Path(configured).expanduser().resolve()
    return (repo_root.resolve().parent / "gmaq-data").resolve()


def registry_path(data_root: pathlib.Path) -> pathlib.Path:
    return data_root / "registry.sqlite"


def _connect(data_root: pathlib.Path, *, read_only: bool = False) -> sqlite3.Connection:
    path = registry_path(data_root)
    if read_only:
        if not path.is_file():
            raise DataLayerError("data registry is absent")
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    else:
        data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize(data_root: pathlib.Path) -> pathlib.Path:
    data_root = data_root.expanduser().resolve()
    data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("snapshots", "tmp"):
        (data_root / name).mkdir(exist_ok=True, mode=0o700)
    with contextlib.closing(_connect(data_root)) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id TEXT PRIMARY KEY,
                dataset TEXT NOT NULL,
                stage TEXT NOT NULL CHECK(stage IN ('raw','validated','curated','quarantine')),
                schema_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('PASS','QUARANTINED')),
                quality_verdict TEXT NOT NULL,
                cross_source_verdict TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                source_fetched_at_utc TEXT,
                window_start TEXT,
                window_end TEXT,
                parent_snapshot_id TEXT REFERENCES snapshots(snapshot_id),
                artifact_relpath TEXT NOT NULL UNIQUE,
                manifest_sha256 TEXT NOT NULL,
                total_rows INTEGER NOT NULL CHECK(total_rows >= 0),
                total_bytes INTEGER NOT NULL CHECK(total_bytes >= 0),
                source_metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS snapshot_files (
                snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
                role TEXT NOT NULL,
                relpath TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                row_count INTEGER NOT NULL CHECK(row_count >= 0),
                byte_count INTEGER NOT NULL CHECK(byte_count >= 0),
                PRIMARY KEY(snapshot_id, role),
                UNIQUE(snapshot_id, relpath)
            );
            CREATE INDEX IF NOT EXISTS snapshots_lookup
                ON snapshots(dataset, stage, created_at_utc DESC);
            CREATE TRIGGER IF NOT EXISTS snapshots_no_update
                BEFORE UPDATE ON snapshots BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS snapshots_no_delete
                BEFORE DELETE ON snapshots BEGIN SELECT RAISE(ABORT, 'snapshots are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS snapshot_files_no_update
                BEFORE UPDATE ON snapshot_files BEGIN SELECT RAISE(ABORT, 'snapshot files are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS snapshot_files_no_delete
                BEFORE DELETE ON snapshot_files BEGIN SELECT RAISE(ABORT, 'snapshot files are immutable'); END;
            """
        )
        connection.commit()
    return data_root


def load_schema(repo_root: pathlib.Path, name: str) -> tuple[dict, str]:
    path = repo_root / "schemas" / name
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DataLayerError(f"schema unavailable: {path}") from error
    if not isinstance(schema, dict):
        raise DataLayerError("schema document must be an object")
    return schema, sha256_bytes(canonical_json_bytes(schema))


def _regular_source(path: pathlib.Path) -> pathlib.Path:
    try:
        if path.is_symlink() or not path.is_file():
            raise DataLayerError(f"source is not a regular non-symlink file: {path}")
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError as error:
        raise DataLayerError(f"source cannot be inspected: {path}") from error
    if not stat.S_ISREG(mode):
        raise DataLayerError(f"source is not a regular file: {path}")
    return path


def _copy_no_follow(source: pathlib.Path, destination: pathlib.Path) -> None:
    source = _regular_source(source)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise DataLayerError(f"source could not be opened safely: {source}") from error
    try:
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode):
            raise DataLayerError(f"source changed type during copy: {source}")
        with os.fdopen(descriptor, "rb", closefd=False) as src, destination.open("xb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
    finally:
        os.close(descriptor)


def _row_count(path: pathlib.Path) -> int:
    if path.suffix == ".jsonl":
        with path.open("rb") as handle:
            return sum(1 for line in handle if line.strip())
    return 1


def _safe_artifact(data_root: pathlib.Path, relpath: str) -> pathlib.Path:
    root = data_root.resolve()
    candidate = root / relpath
    resolved = candidate.resolve()
    if resolved == root or root not in resolved.parents:
        raise DataLayerError("artifact path escapes the data root")
    return candidate


def _remove_unregistered_artifact(data_root: pathlib.Path, artifact: pathlib.Path) -> None:
    snapshots_root = (data_root / "snapshots").resolve()
    try:
        relative = artifact.relative_to(snapshots_root)
    except ValueError as error:
        raise DataLayerError("refusing to clean an unexpected artifact path") from error
    if len(relative.parts) != 3:
        raise DataLayerError("refusing to clean an unexpected artifact path")
    if artifact.is_symlink():
        artifact.unlink()
        return
    (artifact / "data").chmod(0o700)
    artifact.chmod(0o700)
    shutil.rmtree(artifact)


def create_snapshot(
    *,
    data_root: pathlib.Path,
    dataset: str,
    stage: str,
    schema_id: str,
    files: Iterable[InputFile],
    source_metadata: Mapping[str, object],
    checks: Mapping[str, object],
    quality_verdict: str,
    cross_source_verdict: str,
    parent_snapshot_id: str | None = None,
    status: str = PASS_STATUS,
) -> str:
    if stage not in STAGE_RANK:
        raise DataLayerError(f"invalid stage: {stage}")
    if status == PASS_STATUS and stage == "quarantine":
        raise DataLayerError("quarantine snapshots cannot have PASS status")
    if status == QUARANTINED_STATUS and stage != "quarantine":
        raise DataLayerError("QUARANTINED status requires quarantine stage")
    if not dataset or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for char in dataset):
        raise DataLayerError("dataset name is invalid")

    inputs = list(files)
    roles = [item.role for item in inputs]
    invalid_role = any(
        not role
        or not role.isascii()
        or any(not (char.isalnum() or char in "._-") for char in role)
        for role in roles
    )
    if not inputs or invalid_role or len(roles) != len(set(roles)):
        raise DataLayerError("snapshot files must have unique non-empty roles")
    data_root = initialize(data_root)
    checked_at = utc_now()

    with tempfile.TemporaryDirectory(prefix="snapshot-", dir=data_root / "tmp") as temporary:
        work = pathlib.Path(temporary) / "artifact"
        data_dir = work / "data"
        data_dir.mkdir(parents=True, mode=0o700)
        file_rows = []
        for item in inputs:
            destination = data_dir / item.name
            if item.source is not None:
                _copy_no_follow(item.source, destination)
            else:
                assert item.payload is not None
                with destination.open("xb") as handle:
                    handle.write(item.payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            file_rows.append(
                {
                    "role": item.role,
                    "relpath": f"data/{item.name}",
                    "sha256": sha256_file(destination),
                    "row_count": _row_count(destination),
                    "byte_count": destination.stat().st_size,
                }
            )

        normalized_checks = {
            str(name): value if isinstance(value, dict) else {"detail": value}
            for name, value in sorted(checks.items())
        }
        identity = {
            "protocol_version": SCHEMA_VERSION,
            "dataset": dataset,
            "stage": stage,
            "schema_id": schema_id,
            "parent_snapshot_id": parent_snapshot_id,
            "files": file_rows,
            "source_metadata": dict(source_metadata),
            "quality_verdict": quality_verdict,
            "cross_source_verdict": cross_source_verdict,
            "checks": normalized_checks,
            "status": status,
        }
        snapshot_id = sha256_bytes(canonical_json_bytes(identity))
        artifact_relpath = f"snapshots/{dataset}/{stage}/{snapshot_id}"
        final = _safe_artifact(data_root, artifact_relpath)

        with contextlib.closing(_connect(data_root, read_only=True)) as connection:
            existing = connection.execute(
                "SELECT snapshot_id FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone()
        if existing:
            verify_snapshot(data_root, snapshot_id, allow_quarantine=True)
            return snapshot_id
        if final.exists() or final.is_symlink():
            _remove_unregistered_artifact(data_root, final)

        manifest = {
            **identity,
            "snapshot_id": snapshot_id,
            "created_at_utc": checked_at,
        }
        manifest_path = work / "snapshot.manifest.json"
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        with manifest_path.open("rb") as handle:
            os.fsync(handle.fileno())
        manifest_sha = sha256_file(manifest_path)

        for path in data_dir.iterdir():
            path.chmod(0o444)
        manifest_path.chmod(0o444)
        final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.rename(work, final)
        (final / "data").chmod(0o555)
        final.chmod(0o555)

    source_fetched = source_metadata.get("fetched_at_utc")
    window_start = source_metadata.get("window_start")
    window_end = source_metadata.get("window_end")
    total_rows = sum(int(item["row_count"]) for item in file_rows)
    total_bytes = sum(int(item["byte_count"]) for item in file_rows)
    try:
        with contextlib.closing(_connect(data_root)) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO snapshots(
                        snapshot_id,dataset,stage,schema_id,status,quality_verdict,
                        cross_source_verdict,created_at_utc,source_fetched_at_utc,
                        window_start,window_end,parent_snapshot_id,artifact_relpath,
                        manifest_sha256,total_rows,total_bytes,source_metadata_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        snapshot_id,
                        dataset,
                        stage,
                        schema_id,
                        status,
                        quality_verdict,
                        cross_source_verdict,
                        checked_at,
                        str(source_fetched) if source_fetched else None,
                        str(window_start) if window_start else None,
                        str(window_end) if window_end else None,
                        parent_snapshot_id,
                        artifact_relpath,
                        manifest_sha,
                        total_rows,
                        total_bytes,
                        json.dumps(dict(source_metadata), ensure_ascii=False, sort_keys=True),
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO snapshot_files(snapshot_id,role,relpath,sha256,row_count,byte_count)
                    VALUES(?,?,?,?,?,?)
                    """,
                    [
                        (
                            snapshot_id,
                            item["role"],
                            item["relpath"],
                            item["sha256"],
                            item["row_count"],
                            item["byte_count"],
                        )
                        for item in file_rows
                    ],
                )
    except sqlite3.Error as error:
        try:
            _remove_unregistered_artifact(data_root, final)
        except (OSError, DataLayerError) as cleanup_error:
            raise DataLayerError(
                f"registry commit failed for {snapshot_id}; orphan cleanup failed: {cleanup_error}"
            ) from error
        raise DataLayerError(f"registry commit failed for {snapshot_id}") from error
    verify_snapshot(data_root, snapshot_id, allow_quarantine=True)
    return snapshot_id


def snapshot_record(data_root: pathlib.Path, snapshot_id: str) -> dict:
    with contextlib.closing(_connect(data_root, read_only=True)) as connection:
        row = connection.execute(
            "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise DataLayerError(f"snapshot is not registered: {snapshot_id}")
        files = connection.execute(
            "SELECT role,relpath,sha256,row_count,byte_count FROM snapshot_files WHERE snapshot_id = ? ORDER BY role",
            (snapshot_id,),
        ).fetchall()
    result = dict(row)
    result["source_metadata"] = json.loads(result.pop("source_metadata_json"))
    result["files"] = [dict(item) for item in files]
    result["artifact_path"] = str(_safe_artifact(data_root, result["artifact_relpath"]))
    return result


def verify_snapshot(
    data_root: pathlib.Path,
    snapshot_id: str,
    *,
    expected_dataset: str | None = None,
    minimum_stage: str | None = None,
    allow_quarantine: bool = False,
) -> dict:
    if len(snapshot_id) != 64 or any(char not in "0123456789abcdef" for char in snapshot_id):
        raise DataLayerError("snapshot ID must be a full lowercase SHA-256")
    record = snapshot_record(data_root, snapshot_id)
    if expected_dataset and record["dataset"] != expected_dataset:
        raise DataLayerError("snapshot dataset mismatch")
    if minimum_stage:
        if minimum_stage not in STAGE_RANK or STAGE_RANK[record["stage"]] < STAGE_RANK[minimum_stage]:
            raise DataLayerError("snapshot stage is below the required gate")
    if not allow_quarantine and (
        record["status"] != PASS_STATUS or record["stage"] == "quarantine"
    ):
        raise DataLayerError("quarantined data cannot be consumed")
    if minimum_stage == "curated" and record["quality_verdict"] != "PASS":
        raise DataLayerError("curated snapshot lacks a PASS quality verdict")

    artifact = pathlib.Path(record["artifact_path"])
    if artifact.is_symlink() or not artifact.is_dir():
        raise DataLayerError("snapshot artifact directory is missing or unsafe")
    manifest_path = artifact / "snapshot.manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise DataLayerError("snapshot manifest is missing or unsafe")
    if sha256_file(manifest_path) != record["manifest_sha256"]:
        raise DataLayerError("snapshot manifest checksum mismatch")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DataLayerError("snapshot manifest is invalid JSON") from error
    for field in ("snapshot_id", "dataset", "stage", "schema_id", "parent_snapshot_id"):
        if manifest.get(field) != record.get(field):
            raise DataLayerError(f"snapshot manifest binding mismatch: {field}")

    manifest_files = {item["role"]: item for item in manifest.get("files", [])}
    if set(manifest_files) != {item["role"] for item in record["files"]}:
        raise DataLayerError("snapshot file registry and manifest disagree")
    for item in record["files"]:
        manifest_item = manifest_files[item["role"]]
        if any(manifest_item.get(key) != item[key] for key in ("relpath", "sha256", "row_count", "byte_count")):
            raise DataLayerError("snapshot file metadata mismatch")
        path = _safe_artifact(artifact, item["relpath"])
        if path.is_symlink() or not path.is_file():
            raise DataLayerError("snapshot data file is missing or unsafe")
        if path.stat().st_size != item["byte_count"] or sha256_file(path) != item["sha256"]:
            raise DataLayerError(f"snapshot data checksum mismatch: {item['role']}")
        if _row_count(path) != item["row_count"]:
            raise DataLayerError(f"snapshot row count mismatch: {item['role']}")

    with contextlib.closing(_connect(data_root, read_only=True)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise DataLayerError(f"registry integrity failed: {integrity}")
    record["integrity_verdict"] = "VERIFIED"
    return record


def status_summary(data_root: pathlib.Path) -> dict:
    data_root = data_root.expanduser().resolve()
    if not registry_path(data_root).is_file():
        return {
            "verdict": "ABSENT",
            "data_root": str(data_root),
            "registry_integrity": "UNKNOWN",
            "latest": [],
            "quarantine_count": 0,
            "curated_available": False,
        }
    try:
        with contextlib.closing(_connect(data_root, read_only=True)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            rows = connection.execute(
                """
                SELECT s.* FROM snapshots s
                JOIN (
                    SELECT dataset,stage,MAX(created_at_utc) AS latest_at
                    FROM snapshots GROUP BY dataset,stage
                ) latest
                ON latest.dataset=s.dataset AND latest.stage=s.stage AND latest.latest_at=s.created_at_utc
                ORDER BY s.dataset,s.stage
                """
            ).fetchall()
            quarantine_count = connection.execute(
                "SELECT COUNT(*) FROM snapshots WHERE stage='quarantine'"
            ).fetchone()[0]
        latest = []
        all_verified = integrity == "ok"
        for row in rows:
            item = dict(row)
            try:
                verified = verify_snapshot(
                    data_root, item["snapshot_id"], allow_quarantine=True
                )["integrity_verdict"]
            except DataLayerError:
                verified = "BROKEN"
                all_verified = False
            latest.append(
                {
                    "dataset": item["dataset"],
                    "stage": item["stage"],
                    "snapshot_id": item["snapshot_id"],
                    "quality_verdict": item["quality_verdict"],
                    "cross_source_verdict": item["cross_source_verdict"],
                    "source_fetched_at_utc": item["source_fetched_at_utc"],
                    "window_start": item["window_start"],
                    "window_end": item["window_end"],
                    "total_rows": item["total_rows"],
                    "total_bytes": item["total_bytes"],
                    "integrity_verdict": verified,
                }
            )
        curated = [
            item
            for item in latest
            if item["stage"] == "curated"
            and item["quality_verdict"] == "PASS"
            and item["integrity_verdict"] == "VERIFIED"
        ]
        return {
            "verdict": "PASS" if all_verified and curated else "FAIL",
            "data_root": str(data_root),
            "registry_integrity": "VERIFIED" if integrity == "ok" else "BROKEN",
            "latest": latest,
            "quarantine_count": quarantine_count,
            "curated_available": bool(curated),
        }
    except (DataLayerError, sqlite3.Error, OSError) as error:
        return {
            "verdict": "FAIL",
            "data_root": str(data_root),
            "registry_integrity": "BROKEN",
            "detail": str(error),
            "latest": [],
            "quarantine_count": None,
            "curated_available": False,
        }
