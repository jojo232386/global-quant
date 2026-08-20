import importlib.util
import pathlib
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
