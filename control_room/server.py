#!/usr/bin/env python3
"""Local read-only dashboard for GMAQ runtime and research evidence.

The server binds to loopback, exposes GET/HEAD only, never returns secrets,
and does not offer arm, order, exit, pause, or kill actions.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import pathlib
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from urllib.parse import urlsplit


ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "control_room" / "static"
RESEARCH_DIR = ROOT / "research" / "backtests"
AUDIT_DIR = ROOT / "user_data" / "audit"
CONFIG_PATH = ROOT / "user_data" / "config.json"
COST_MODEL_PATH = ROOT / "configs" / "execution-costs.json"
LIVE_READINESS_PATH = ROOT / "configs" / "LIVE_READINESS.md"
CONTROL_PATH = ROOT / "scripts" / "gmaq-control"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090
CACHE_SECONDS = 10
EXPECTED_API_BASE = "http://127.0.0.1:8082"
EXPECTED_CONTAINER = "gmaq-freqtrade-p0-remediation"


def load_control_module():
    loader = SourceFileLoader("gmaq_control_room_backend", str(CONTROL_PATH))
    spec = importlib.util.spec_from_loader("gmaq_control_room_backend", loader)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


CONTROL = load_control_module()
_CACHE_LOCK = threading.Lock()
_CACHE: dict = {"captured_at_epoch": 0.0, "payload": None}


def git_text(*args: str) -> str | None:
    try:
        process = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return process.stdout.strip() if process.returncode == 0 else None


def read_json_object(path: pathlib.Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def audit_snapshot() -> dict:
    try:
        chain = CONTROL.read_audit_chain()
        valid, detail = CONTROL.audit_chain_valid(chain)
    except (OSError, TypeError, ValueError) as error:
        return {"verdict": "BROKEN", "records": None, "detail": str(error)}
    return {
        "verdict": "VERIFIED" if valid else "BROKEN",
        "records": len(chain),
        "detail": detail,
        "last_event": chain[-1].get("event") if chain else None,
        "last_event_utc": chain[-1].get("ts_utc") if chain else None,
    }


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_api_get(path: str, token: str) -> object | None:
    request = urllib.request.Request(
        f"{EXPECTED_API_BASE}{path}",
        headers={"Authorization": f"Basic {token}"},
    )
    try:
        with urllib.request.build_opener(NoRedirectHandler()).open(
            request, timeout=CONTROL.NETWORK_TIMEOUT_S
        ) as response:
            if response.status != 200:
                return None
            return json.loads(response.read())
    except (OSError, urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return None


def unavailable_runtime(reason: str) -> dict:
    return {
        "health": {
            "verdict": "OFFLINE" if reason == "runtime_unavailable" else "UNKNOWN",
            "checks": [
                {"name": "Local boundary", "passed": False, "detail": reason},
                {"name": "API", "passed": False, "detail": "unavailable"},
                {"name": "Heartbeat", "passed": False, "detail": None, "unit": "seconds"},
                {"name": "Clock", "passed": False, "detail": None, "unit": "seconds"},
            ],
        },
        "reconciliation": {
            "verdict": "UNKNOWN",
            "unknown_outcomes": [reason],
            "mismatches": [],
            "open_trades": None,
            "open_orders": None,
            "partial_orders": None,
            "matches_database": False,
        },
    }


def runtime_probe() -> dict:
    """Read only the expected local runtime without appending audit or redirecting secrets."""
    try:
        env = CONTROL.read_env()
        if env.get("GMAQ_API_BASE") != EXPECTED_API_BASE:
            return unavailable_runtime("api_boundary_mismatch")
        if env.get("GMAQ_CONTAINER_NAME") != EXPECTED_CONTAINER:
            return unavailable_runtime("container_boundary_mismatch")
        token = CONTROL.api_login(env)
        if not token:
            return unavailable_runtime("local_api_credentials_unavailable")
        ping_payload = local_api_get("/api/v1/ping", token)
        health_payload = local_api_get("/api/v1/health", token)
        rest_payload = local_api_get("/api/v1/status", token)
        ping = (
            ping_payload == "pong"
            or isinstance(ping_payload, dict) and ping_payload.get("status") == "pong"
        )
        heartbeat_age = None
        if isinstance(health_payload, dict) and health_payload.get("last_process_ts"):
            heartbeat_age = max(0.0, time.time() - float(health_payload["last_process_ts"]))
        clock_offset = CONTROL.exchange_time_offset_s()
        db_payload = CONTROL.bot_db_snapshot(env)
    except (OSError, TypeError, ValueError):
        return unavailable_runtime("runtime_probe_error")

    if rest_payload is not None and db_payload is not None:
        reconciliation = CONTROL.compare_snapshots(rest_payload, db_payload)
    else:
        missing = []
        if rest_payload is None:
            missing.append("rest_status_unavailable")
        if db_payload is None:
            missing.append("database_snapshot_unavailable")
        reconciliation = {
            "verdict": "UNKNOWN",
            "unknown_outcomes": missing,
            "mismatches": [],
            "open_trades": None,
            "open_orders": None,
            "partial_orders": None,
            "matches_database": False,
        }

    health_checks = [
        {"name": "API", "passed": ping, "detail": "pong" if ping else "unreachable"},
        {
            "name": "Heartbeat",
            "passed": heartbeat_age is not None and heartbeat_age <= 2 * CONTROL.TIMEFRAME_SECONDS,
            "detail": round(heartbeat_age, 1) if heartbeat_age is not None else None,
            "unit": "seconds",
        },
        {
            "name": "Clock",
            "passed": clock_offset is not None and abs(clock_offset) <= CONTROL.CLOCK_OFFSET_LIMIT_S,
            "detail": round(clock_offset, 3) if clock_offset is not None else None,
            "unit": "seconds",
        },
    ]
    if all(item["passed"] for item in health_checks):
        health_verdict = "HEALTHY"
    elif ping:
        health_verdict = "UNHEALTHY"
    else:
        health_verdict = "OFFLINE"
    return {
        "health": {"verdict": health_verdict, "checks": health_checks},
        "reconciliation": reconciliation,
    }


def safe_config_snapshot() -> dict:
    config = read_json_object(CONFIG_PATH) or {}
    exchange = config.get("exchange") if isinstance(config.get("exchange"), dict) else {}
    pairs = exchange.get("pair_whitelist") if isinstance(exchange.get("pair_whitelist"), list) else []
    return {
        "dry_run": config.get("dry_run") is True,
        "trading_mode": config.get("trading_mode"),
        "margin_mode": config.get("margin_mode"),
        "stake_currency": config.get("stake_currency"),
        "stake_amount": config.get("stake_amount"),
        "max_open_trades": config.get("max_open_trades"),
        "pairs": [str(pair) for pair in pairs],
        "credential_free": not exchange.get("key") and not exchange.get("secret"),
    }


def cost_model_sha() -> str | None:
    try:
        return hashlib.sha256(COST_MODEL_PATH.read_bytes()).hexdigest()
    except OSError:
        return None


def research_snapshot() -> dict:
    studies = []
    current_cost_sha = cost_model_sha()
    if RESEARCH_DIR.is_dir():
        for path in sorted(RESEARCH_DIR.glob("*/results.json")):
            result = read_json_object(path)
            if not result:
                continue
            oos = result.get("out_of_sample") if isinstance(result.get("out_of_sample"), dict) else {}
            studies.append(
                {
                    "study_id": str(result.get("study_id") or path.parent.name),
                    "verdict": str(result.get("verdict") or "UNKNOWN"),
                    "total_return": oos.get("total_return"),
                    "sharpe": oos.get("sharpe"),
                    "max_drawdown": oos.get("max_drawdown"),
                    "trade_count": oos.get("trade_count"),
                    "evidence_generation": (
                        "COST_MODEL_MATCH_ONLY"
                        if current_cost_sha and result.get("cost_model_sha256") == current_cost_sha
                        else "UNVERIFIED_ARTIFACT"
                    ),
                }
            )
    counts: dict[str, int] = {}
    for study in studies:
        counts[study["verdict"]] = counts.get(study["verdict"], 0) + 1
    return {
        "promotion_verdict": "BLOCKED_UNVERIFIED_ARTIFACTS",
        "counts": counts,
        "studies": studies,
    }


def parse_blockers() -> list[str]:
    try:
        lines = LIVE_READINESS_PATH.read_text().splitlines()
    except OSError:
        return ["LIVE_READINESS unavailable"]
    blockers = []
    in_section = False
    current = ""
    for line in lines:
        if line == "## Current blockers":
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if not in_section:
            continue
        if line.startswith("- "):
            if current:
                blockers.append(current)
            current = line[2:].strip()
        elif current and line.startswith("  "):
            current += " " + line.strip()
    if current:
        blockers.append(current)
    return blockers


def evidence_snapshot() -> list[dict]:
    rows = []
    if not AUDIT_DIR.is_dir():
        return rows
    for path in sorted(AUDIT_DIR.glob("soak-*"), reverse=True)[:8]:
        manifest = read_json_object(path / "manifest.json") or {}
        final_audit = read_json_object(path / "final-audit-verify.json") or {}
        events = []
        try:
            for line in (path / "events.jsonl").read_text().splitlines():
                value = json.loads(line)
                if isinstance(value, dict):
                    events.append(value)
        except (OSError, json.JSONDecodeError):
            events = []
        expected_smoke = {"E0": "EXACT_MATCH", "E1": "HEALTHY", "E2": "MATCH"}
        observed = {str(item.get("event")): item.get("verdict") for item in events}
        integrity = (
            "VERIFIED"
            if manifest.get("schema_version") == 1
            and final_audit.get("verdict") == "VERIFIED"
            and all(observed.get(event) == verdict for event, verdict in expected_smoke.items())
            else "UNVERIFIED"
        )
        rows.append(
            {
                "name": path.name,
                "verdict": "SMOKE_EVIDENCE" if integrity == "VERIFIED" else "UNVERIFIED",
                "integrity": integrity,
                "candidate_sha": manifest.get("candidate_sha"),
                "environment": manifest.get("environment"),
                "gate_state": manifest.get("gate_state"),
            }
        )
    return rows


def build_snapshot() -> dict:
    state = read_json_object(AUDIT_DIR / "state.json") or {"state": "UNKNOWN"}
    binding = read_json_object(AUDIT_DIR / "runtime-binding.json") or {}
    runtime = runtime_probe()
    repo_sha = git_text("rev-parse", "HEAD")
    identity_errors = []
    for field in ("candidate_sha", "config_sha256", "environment", "run_id"):
        if not state.get(field) or state.get(field) != binding.get(field):
            identity_errors.append(f"state_binding_{field}_mismatch")
    if state.get("candidate_sha") != repo_sha:
        identity_errors.append("candidate_repo_sha_mismatch")
    if state.get("environment") != "dry_run":
        identity_errors.append("environment_not_dry_run")
    current_config_sha = None
    try:
        current_config_sha = hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()
    except OSError:
        identity_errors.append("config_unavailable")
    if current_config_sha and state.get("config_sha256") != current_config_sha:
        identity_errors.append("config_sha_mismatch")
    if state.get("state") == "ARMED":
        expires = state.get("expires_at_epoch")
        if not isinstance(expires, (int, float)) or expires <= time.time():
            identity_errors.append("authorization_expired")
    identity_verdict = "VERIFIED" if not identity_errors else "STOP"
    effective_gate_state = state.get("state", "UNKNOWN") if not identity_errors else "UNKNOWN"
    worktree_status = git_text("status", "--porcelain")
    return {
        "schema_version": 1,
        "captured_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "read_only": True,
        "actions_enabled": False,
        "repo": {
            "sha": repo_sha,
            "branch": git_text("branch", "--show-current"),
            "clean": worktree_status == "",
        },
        "gate": {
            "state": effective_gate_state,
            "reported_state": state.get("state", "UNKNOWN"),
            "identity_verdict": identity_verdict,
            "identity_errors": identity_errors,
            "reason": state.get("reason"),
            "environment": state.get("environment") or binding.get("environment"),
            "run_id": state.get("run_id") or binding.get("run_id"),
            "candidate_sha": state.get("candidate_sha") or binding.get("candidate_sha"),
            "authorization_scope": state.get("authorization_scope"),
            "expires_at_epoch": state.get("expires_at_epoch"),
        },
        "runtime": runtime,
        "audit": audit_snapshot(),
        "config": safe_config_snapshot(),
        "research": research_snapshot(),
        "blockers": parse_blockers(),
        "evidence": evidence_snapshot(),
        "ui_notice": "Read-only local dashboard. No arm, order, exit, pause, or kill actions.",
    }


def cached_snapshot(force: bool = False) -> dict:
    now = time.monotonic()
    with _CACHE_LOCK:
        if (
            force
            or _CACHE["payload"] is None
            or now - float(_CACHE["captured_at_epoch"]) >= CACHE_SECONDS
        ):
            _CACHE["payload"] = build_snapshot()
            _CACHE["captured_at_epoch"] = now
        return _CACHE["payload"]


class ControlRoomHandler(BaseHTTPRequestHandler):
    server_version = "GMAQControlRoom/1"

    def log_message(self, format: str, *args) -> None:
        return

    def allowed_host(self) -> bool:
        raw_host = self.headers.get("Host", "").lower()
        if raw_host.startswith("["):
            host = raw_host.partition("]")[0].lstrip("[")
        else:
            host = raw_host.split(":", 1)[0]
        return host in {"127.0.0.1", "localhost", "::1"}

    def security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")

    def send_bytes(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self.allowed_host():
            self.send_bytes(b"host rejected\n", "text/plain; charset=utf-8", HTTPStatus.MISDIRECTED_REQUEST)
            return
        path = urlsplit(self.path).path
        if path == "/api/snapshot":
            payload = json.dumps(cached_snapshot(), ensure_ascii=False).encode("utf-8")
            self.send_bytes(payload, "application/json; charset=utf-8")
            return
        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        asset = assets.get(path)
        if asset:
            try:
                body = (STATIC_DIR / asset[0]).read_bytes()
            except OSError:
                self.send_bytes(b"asset unavailable\n", "text/plain; charset=utf-8", HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_bytes(body, asset[1])
            return
        self.send_bytes(b"not found\n", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        self.send_bytes(
            b"read-only dashboard: mutations are disabled\n",
            "text/plain; charset=utf-8",
            HTTPStatus.METHOD_NOT_ALLOWED,
        )

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    if host != DEFAULT_HOST:
        raise ValueError("Control Room must bind to 127.0.0.1")
    server = ThreadingHTTPServer((host, port), ControlRoomHandler)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GMAQ local read-only Control Room")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
