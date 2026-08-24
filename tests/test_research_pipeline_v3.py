"""Contract tests for the deliberately small Research Pipeline v3 additions."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROCESS = ROOT / "research" / "process"


def load_module(name: str):
    path = PROCESS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


metadata = load_module("research_run_metadata")
readiness = load_module("formal_readiness")
diagnostic = load_module("factor_diagnostic")


def test_metadata_hashes_explicit_files_and_resolves_checked_out_code(tmp_path: pathlib.Path) -> None:
    def git(*arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=tmp_path, check=True, capture_output=True)

    git("init")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    dataset = tmp_path / "dataset.json"
    config = tmp_path / "config.json"
    result = tmp_path / "result.json"
    dataset.write_text("dataset")
    config.write_text("config")
    result.write_text("result")
    git("add", "dataset.json", "config.json", "result.json")
    git("commit", "-m", "inputs")
    record = metadata.build_run_metadata(
        hypothesis_id="HYP-001",
        implementation_id="IMPL-001",
        formal_run_id="FORMAL-001",
        dataset_path=dataset,
        config_path=config,
        result_path=result,
        repo_root=tmp_path,
    )
    assert tuple(record) == metadata.REQUIRED_KEYS
    assert record["DATASET_SHA"] == hashlib.sha256(b"dataset").hexdigest()
    assert record["CONFIG_SHA"] == hashlib.sha256(b"config").hexdigest()
    assert record["RESULT_SHA"] == hashlib.sha256(b"result").hexdigest()
    assert len(record["CODE_SHA"]) == 40
    output = tmp_path / "metadata.json"
    assert metadata.emit_run_metadata(
        output,
        hypothesis_id="HYP-001",
        implementation_id="IMPL-001",
        formal_run_id="FORMAL-001",
        dataset_path=dataset,
        config_path=config,
        result_path=result,
        repo_root=tmp_path,
    ) == record
    assert json.loads(output.read_text()) == record
    with pytest.raises(metadata.MetadataError, match="unsafe"):
        metadata.emit_run_metadata(output, hypothesis_id="H", implementation_id="I", formal_run_id="F", dataset_path=dataset, config_path=config, result_path=result, repo_root=tmp_path)


def test_metadata_rejects_dirty_tracked_or_untracked_worktree(tmp_path: pathlib.Path) -> None:
    def git(*arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=tmp_path, check=True, capture_output=True)

    git("init")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    for name in ("dataset", "config", "result"):
        (tmp_path / name).write_text(name)
    git("add", "dataset", "config", "result")
    git("commit", "-m", "inputs")
    arguments = {
        "hypothesis_id": "H", "implementation_id": "I", "formal_run_id": "F",
        "dataset_path": tmp_path / "dataset", "config_path": tmp_path / "config",
        "result_path": tmp_path / "result", "repo_root": tmp_path,
    }
    (tmp_path / "dataset").write_text("changed")
    with pytest.raises(metadata.MetadataError, match="worktree must be clean"):
        metadata.build_run_metadata(**arguments)
    git("checkout", "--", "dataset")
    (tmp_path / "untracked").write_text("untracked")
    with pytest.raises(metadata.MetadataError, match="worktree must be clean"):
        metadata.build_run_metadata(**arguments)


def test_metadata_fails_closed_on_missing_file_or_invalid_identity(tmp_path: pathlib.Path) -> None:
    existing = tmp_path / "existing"
    existing.write_text("x")
    with pytest.raises(metadata.MetadataError, match="invalid HYPOTHESIS_ID"):
        metadata.build_run_metadata(
            hypothesis_id=" ", implementation_id="I", formal_run_id="F", dataset_path=existing,
            config_path=existing, result_path=existing, repo_root=ROOT,
        )
    with pytest.raises(metadata.MetadataError, match="required regular file missing"):
        metadata.build_run_metadata(
            hypothesis_id="H", implementation_id="I", formal_run_id="F", dataset_path=tmp_path / "absent",
            config_path=existing, result_path=existing, repo_root=ROOT,
        )


def test_formal_readiness_is_exact_boolean_and_fail_closed() -> None:
    all_true = {name: True for name in readiness.REQUIRED_READINESS}
    assert readiness.require_formal_readiness(all_true) == all_true
    missing = dict(all_true)
    missing.pop("REPORT_READY")
    with pytest.raises(readiness.FormalReadinessError, match="keys differ"):
        readiness.require_formal_readiness(missing)
    non_boolean = dict(all_true, PIT_READY=1)
    with pytest.raises(readiness.FormalReadinessError, match="PIT_READY must be a boolean"):
        readiness.require_formal_readiness(non_boolean)
    false = dict(all_true, ACCOUNTING_READY=False)
    with pytest.raises(readiness.FormalReadinessError, match="ACCOUNTING_READY is not ready"):
        readiness.require_formal_readiness(false)


def test_readiness_template_is_intentionally_not_an_approval() -> None:
    template = json.loads((PROCESS / "FORMAL_READINESS_TEMPLATE.json").read_text())
    assert tuple(template) == readiness.REQUIRED_READINESS
    assert all(value is False for value in template.values())
    with pytest.raises(readiness.FormalReadinessError, match="not ready"):
        readiness.require_formal_readiness(template)


def test_readiness_loader_rejects_duplicate_keys_before_last_value_wins(tmp_path: pathlib.Path) -> None:
    fields = []
    for name in readiness.REQUIRED_READINESS:
        if name == "PIT_READY":
            fields.extend(('"PIT_READY": false', '"PIT_READY": true'))
        else:
            fields.append(f'"{name}": true')
    path = tmp_path / "duplicate-readiness.json"
    path.write_text("{" + ", ".join(fields) + "}")
    with pytest.raises(readiness.FormalReadinessError, match="duplicate readiness key: PIT_READY"):
        readiness.load_and_require_formal_readiness(path)


def _observations() -> list[dict[str, object]]:
    return [
        {"timestamp": 1, "symbol": "A", "factor": 1.0, "future_return": 0.01},
        {"timestamp": 1, "symbol": "B", "factor": 2.0, "future_return": 0.02},
        {"timestamp": 1, "symbol": "C", "factor": 3.0, "future_return": 0.03},
        {"timestamp": 1, "symbol": "D", "factor": 4.0, "future_return": 0.04},
        {"timestamp": 1, "symbol": "E", "factor": 5.0, "future_return": 0.05},
        {"timestamp": 2, "symbol": "A", "factor": 5.0, "future_return": 0.05},
        {"timestamp": 2, "symbol": "B", "factor": 4.0, "future_return": 0.04},
        {"timestamp": 2, "symbol": "C", "factor": 3.0, "future_return": 0.03},
        {"timestamp": 2, "symbol": "D", "factor": 2.0, "future_return": 0.02},
        {"timestamp": 2, "symbol": "E", "factor": 1.0, "future_return": 0.01},
    ]


def test_factor_diagnostic_is_explicit_diagnostic_only_with_two_leg_turnover() -> None:
    report = diagnostic.factor_diagnostic(_observations(), quantiles=5, cost_bps=(0.0, 10.0))
    assert report["status"] == diagnostic.DIAGNOSTIC_STATUS
    assert "formal_verdict" not in report
    assert report["mean_ic"] == pytest.approx(1.0)
    assert report["mean_rank_ic"] == pytest.approx(1.0)
    assert report["mean_quantile_spread"] == pytest.approx(0.04)
    assert report["mean_gross_one_return"] == pytest.approx(0.02)
    assert report["total_transition_two_leg_turnover"] == pytest.approx(2.0)
    assert report["mean_amortized_transition_two_leg_turnover"] == pytest.approx(1.0)
    costs = report["cost_sensitivity"]
    assert costs[0]["mean_gross_one_return_after_cost"] == pytest.approx(0.02)
    assert costs[1]["mean_gross_one_return_after_cost"] == pytest.approx(0.019)


def test_factor_diagnostic_rejects_symbol_dependent_quantile_boundary_ties() -> None:
    bottom_tied = _observations()
    bottom_tied[2]["factor"] = 2.0
    with pytest.raises(diagnostic.FactorDiagnosticError, match="bottom quantile boundary tie"):
        diagnostic.factor_diagnostic(bottom_tied, quantiles=2)
    top_tied = _observations()
    top_tied[2]["factor"] = 4.0
    top_tied[3]["factor"] = 4.0
    with pytest.raises(diagnostic.FactorDiagnosticError, match="top quantile boundary tie"):
        diagnostic.factor_diagnostic(top_tied, quantiles=2)


@pytest.mark.parametrize(
    "observations, message",
    [
        (_observations()[:5], "turnover requires"),
        (_observations() + [_observations()[0]], "duplicate"),
        ([dict(row, factor=1.0) for row in _observations()], "IC is degenerate"),
        ([dict(row, timestamp=float("nan")) for row in _observations()], "timestamp must be finite"),
    ],
)
def test_factor_diagnostic_rejects_malformed_or_degenerate_input(observations, message: str) -> None:
    with pytest.raises(diagnostic.FactorDiagnosticError, match=message):
        diagnostic.factor_diagnostic(observations, quantiles=5)


def test_protocol_and_candidate_contracts_preserve_scope_and_required_checks() -> None:
    protocol = (PROCESS / "GMAQ_RESEARCH_PROTOCOL_V3.md").read_text()
    for marker in (
        "CURRENT_FLOW",
        "IDENTIFIED_BOTTLENECKS",
        "DUPLICATED_WORK",
        "MISSING_CHECKS",
        "Qlib",
        "Alphalens",
        "Freqtrade",
        "VectorBT",
        "does not authorize runtime",
        "new research platform",
        "including\nuntracked files, is clean",
    ):
        assert marker in protocol
    normalized_protocol = " ".join(protocol.split())
    assert normalized_protocol.index("independent Hypothesis Review") < normalized_protocol.index("Data Admission")
    assert normalized_protocol.index("Data Admission") < normalized_protocol.index("Gold Sample")
    candidate_template = (PROCESS / "CANDIDATE_REVIEW_TEMPLATE_V3.md").read_text()
    for field in (
        "HYPOTHESIS_ID", "MECHANISM", "WHY_EXISTS", "DIFFERENCE_FROM_FAILED_WORK",
        "REQUIRED_DATA", "PIT_REQUIREMENTS", "EXPECTED_FAILURE",
    ):
        assert field in candidate_template
    review = " ".join((PROCESS / "NEXT_ALPHA_CANDIDATE_REVIEW_V3.md").read_text().split())
    for marker in (
        "attention/liquidity migration",
        "not price confirmation",
        "contemporaneous own price return",
        "contemporaneous volatility",
        "contemporaneous market beta",
        "momentum plus volume confirmation",
        "CAND-PERPETUAL-LISTING-MATURATION-001",
        "CAND-ORDER-BOOK-RESILIENCE-001",
        "SOURCE_CANDIDATE_ID=CAND-QUOTE-VOLUME-SHARE-MIGRATION-001",
        "REVIEWED_HYPOTHESIS_ID=HYP-QUOTE-VOLUME-SHARE-MIGRATION-001",
        "HYPOTHESIS_REVIEW_DECISION=DATA_BLOCKED",
        "SELECTED_NEXT_HYPOTHESIS=NONE",
        "EXPL_CREATED=NO",
        "HYPOTHESIS_REVIEW_COMPLETE_DATA_BLOCKED_NO_EXPL_CREATED",
        "PIT-denominator and lifecycle data-feasibility proof",
    ):
        assert marker in review
    assert "EXPL-018" not in review
