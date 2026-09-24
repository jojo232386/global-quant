import importlib.util
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gmaq-ui"


def load_ui():
    loader = SourceFileLoader("gmaq_ui_process_contract", str(SCRIPT))
    spec = importlib.util.spec_from_loader("gmaq_ui_process_contract", loader)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_owned_process_accepts_the_exact_server_started_by_ui(monkeypatch) -> None:
    module = load_ui()
    command = (
        f"/usr/bin/python3 {module.SERVER_PATH} "
        f"--host {module.HOST} --port {module.PORT}"
    )
    monkeypatch.setattr(module, "process_command", lambda _pid: command)
    assert module.owned_process(12345) is True
    _assert_control_room_regressions(monkeypatch)


def test_owned_process_rejects_lookalike_or_wrong_binding(monkeypatch, tmp_path) -> None:
    module = load_ui()
    lookalike = tmp_path / "control_room" / "server.py"
    commands = (
        f"/usr/bin/python3 {lookalike} --host {module.HOST} --port {module.PORT}",
        f"/usr/bin/python3 {module.SERVER_PATH} --host 0.0.0.0 --port {module.PORT}",
        f"/usr/bin/python3 {module.SERVER_PATH} --host {module.HOST} --port 9999",
        "not valid ' shell",
        "",
    )
    for command in commands:
        monkeypatch.setattr(module, "process_command", lambda _pid, value=command: value)
        assert module.owned_process(12345) is False


def test_start_command_uses_the_same_exact_server_path() -> None:
    module = load_ui()
    text = SCRIPT.read_text()
    assert 'str(ROOT / "control_room" / "server.py")' in text
    assert module.SERVER_PATH == (ROOT / "control_room" / "server.py").resolve()


def _assert_control_room_regressions(monkeypatch) -> None:
    from control_room import server

    # Local API credentials must reach the local server, never the configured proxy.
    requests_seen = {"origin": [], "proxy": []}

    class OriginHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests_seen["origin"].append(self.headers.get("Authorization"))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{proxy.server_port}/redirected")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'"origin"')

        def log_message(self, *_args):
            pass

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests_seen["proxy"].append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'"proxy"')

        def log_message(self, *_args):
            pass

    origin = ThreadingHTTPServer(("127.0.0.1", 0), OriginHandler)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    threads = [threading.Thread(target=httpd.serve_forever, daemon=True) for httpd in (origin, proxy)]
    for thread in threads:
        thread.start()
    try:
        monkeypatch.setattr(server, "EXPECTED_API_BASE", f"http://127.0.0.1:{origin.server_port}")
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.server_port}")
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_port}")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        assert server.local_api_get("/api/v1/ping", "fake-token") == "origin"
        assert server.local_api_get("/redirect", "fake-token") is None
        assert requests_seen == {"origin": ["Basic fake-token", "Basic fake-token"], "proxy": []}
    finally:
        for httpd in (origin, proxy):
            httpd.shutdown()
            httpd.server_close()
        for thread in threads:
            thread.join(timeout=2)

    # Regression: runtime_probe clock assessment and fail-closed semantics
    import time
    monkeypatch.setattr(
        server.CONTROL,
        "read_env",
        lambda: {
            "GMAQ_API_BASE": server.EXPECTED_API_BASE,
            "GMAQ_CONTAINER_NAME": server.EXPECTED_CONTAINER,
        },
    )
    monkeypatch.setattr(server.CONTROL, "api_login", lambda env: "valid-token")
    monkeypatch.setattr(
        server,
        "local_api_get",
        lambda path, token: (
            {"status": "pong"}
            if path == "/api/v1/ping"
            else ({"last_process_ts": time.time()} if path == "/api/v1/health" else [])
        ),
    )
    monkeypatch.setattr(server.CONTROL, "bot_db_snapshot", lambda env: [])
    monkeypatch.setattr(
        server.CONTROL,
        "compare_snapshots",
        lambda rest, db: {
            "verdict": "MATCH",
            "matches_database": True,
            "open_trades": 0,
            "open_orders": 0,
            "partial_orders": 0,
            "unknown_outcomes": [],
        },
    )

    # Case 1: DISARMED clock assessment (passed=True, estimate_offset_s=None)
    monkeypatch.setattr(
        server.CONTROL,
        "health_clock_assessment",
        lambda: {
            "status": "NOT_REQUIRED_DISARMED",
            "passed": True,
            "estimate_offset_s": None,
            "detail": "gate disarmed",
        },
    )
    healthy_result = server.runtime_probe()
    assert healthy_result["health"]["verdict"] == "HEALTHY"
    clock_check = next(c for c in healthy_result["health"]["checks"] if c["name"] == "Clock")
    assert clock_check["passed"] is True
    assert clock_check["detail"] == "gate disarmed"

    monkeypatch.setattr(
        server.CONTROL,
        "health_clock_assessment",
        lambda: {"status": "PASS", "passed": True, "estimate_offset_s": None},
    )
    incomplete_result = server.runtime_probe()
    assert incomplete_result["health"]["verdict"] == "UNHEALTHY"

    # Case 2: ARMED clock assessment with valid offset
    monkeypatch.setattr(
        server.CONTROL,
        "health_clock_assessment",
        lambda: {
            "status": "PASS",
            "passed": True,
            "estimate_offset_s": 0.05,
            "detail": "offset ok",
        },
    )
    offset_result = server.runtime_probe()
    assert offset_result["health"]["verdict"] == "HEALTHY"
    clock_check = next(c for c in offset_result["health"]["checks"] if c["name"] == "Clock")
    assert clock_check["passed"] is True
    assert clock_check["detail"] == 0.05
    assert clock_check["unit"] == "seconds"

    # Case 3: Clock assessment fails or offset exceeds limit (fail-closed)
    monkeypatch.setattr(
        server.CONTROL,
        "health_clock_assessment",
        lambda: {
            "status": "SUSTAINED_OFFSET_EXCEEDED",
            "passed": False,
            "estimate_offset_s": 5.0,
            "detail": "offset exceeded",
        },
    )
    unhealthy_result = server.runtime_probe()
    assert unhealthy_result["health"]["verdict"] == "UNHEALTHY"
    clock_check = next(c for c in unhealthy_result["health"]["checks"] if c["name"] == "Clock")
    assert clock_check["passed"] is False

    # Case 4: AttributeError is caught and fails closed to unavailable_runtime
    def raise_attr_error():
        raise AttributeError("simulated missing attribute")

    monkeypatch.setattr(server.CONTROL, "health_clock_assessment", raise_attr_error)
    safe_result = server.runtime_probe()
    assert safe_result["health"]["verdict"] == "UNKNOWN"
    assert safe_result["health"]["checks"][0]["detail"] == "runtime_probe_error"

    # Non-finite and implausibly future timestamps must not become a fresh heartbeat.
    monkeypatch.setattr(
        server.CONTROL,
        "health_clock_assessment",
        lambda: {
            "status": "NOT_REQUIRED_DISARMED",
            "passed": True,
            "estimate_offset_s": None,
            "detail": "gate disarmed",
        },
    )
    for invalid_timestamp in (float("nan"), float("inf"), time.time() + 60):
        monkeypatch.setattr(
            server,
            "local_api_get",
            lambda path, token: (
                {"status": "pong"}
                if path == "/api/v1/ping"
                else ({"last_process_ts": invalid_timestamp} if path == "/api/v1/health" else [])
            ),
        )
        result = server.runtime_probe()
        heartbeat = next(c for c in result["health"]["checks"] if c["name"] == "Heartbeat")
        assert heartbeat["passed"] is False
        assert result["health"]["verdict"] == "UNHEALTHY"
