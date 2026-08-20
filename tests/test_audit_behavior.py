import concurrent.futures
import importlib.util
import pathlib
from importlib.machinery import SourceFileLoader


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gmaq-control"


def load_control():
    loader = SourceFileLoader("gmaq_control_audit_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader("gmaq_control_audit_test", loader)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def redirect_audit(control, tmp_path, monkeypatch) -> None:
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    monkeypatch.setattr(control, "AUDIT_DIR", audit_dir)
    monkeypatch.setattr(control, "AUDIT_PATH", audit_dir / "manifest.jsonl")
    monkeypatch.setattr(control, "AUDIT_LOCK_PATH", audit_dir / ".manifest.lock")


def test_concurrent_appends_have_unique_sequence_and_valid_chain(tmp_path, monkeypatch) -> None:
    control = load_control()
    redirect_audit(control, tmp_path, monkeypatch)

    def append(index: int):
        return control.append_audit("concurrent", "test", refs={"index": index}, verdict="PASS")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(append, range(100)))
    assert all(result["ok"] for result in results)
    chain = control.read_audit_chain()
    assert len(chain) == 100
    assert [item["seq"] for item in chain] == list(range(1, 101))
    assert control.audit_chain_valid(chain) == (True, "chain ok")


def test_broken_or_truncated_chain_refuses_append(tmp_path, monkeypatch) -> None:
    control = load_control()
    redirect_audit(control, tmp_path, monkeypatch)
    assert control.append_audit("first", "test", verdict="PASS")["ok"] is True
    assert control.append_audit("second", "test", verdict="FAIL")["ok"] is True
    lines = control.AUDIT_PATH.read_text().splitlines()
    control.AUDIT_PATH.write_text(lines[0] + "\n" + lines[1][:-5] + "\n")
    result = control.append_audit("third", "test", verdict="PASS")
    assert result["ok"] is False
    assert "refusing to append" in result["error"] or "corrupt" in result["error"]


def test_top_level_verdict_is_the_actual_event_verdict(tmp_path, monkeypatch) -> None:
    control = load_control()
    redirect_audit(control, tmp_path, monkeypatch)
    control.append_audit("preflight", "test", refs={"detail": "failed"}, verdict="FAIL")
    record = control.read_audit_chain()[0]
    assert record["verdict"] == "FAIL"
    assert record["refs"]["detail"] == "failed"
