import pathlib
from importlib.machinery import SourceFileLoader

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "research"

ARTIFACTS = {
    "preregistration": RESEARCH / "preregistration" / "HYPOTHESIS_TEMPLATE.md",
    "data": RESEARCH / "data" / "DATA_AVAILABILITY_CHECKLIST.md",
    "manifest": RESEARCH / "manifest" / "RUN_MANIFEST_TEMPLATE.md",
    "costs": RESEARCH / "costs" / "COST_MODEL_BASELINE.md",
    "gate": RESEARCH / "gate" / "EVALUATION_GATE.md",
}

LEGACY_RUNNERS = (
    "gmaq-research-backtest",
    "gmaq-research-crosssection",
    "gmaq-research-pit",
    "gmaq-research-pit-funding-shock-neutral",
    "gmaq-research-tsmom",
)


def test_research_readme_indexes_all_artifacts() -> None:
    readme = (RESEARCH / "README.md").read_text()
    for name in ("preregistration", "data", "manifest", "costs", "gate"):
        assert name in readme
    assert "does not authorize live trading" in readme
    assert "A run without a preregistration and a manifest is not evidence" in readme


def test_all_research_artifacts_exist() -> None:
    for path in ARTIFACTS.values():
        assert path.is_file(), f"missing research artifact: {path}"


def test_preregistration_template_has_required_sections() -> None:
    text = ARTIFACTS["preregistration"].read_text()
    for section in (
        "Hypothesis",
        "Applicable environment",
        "Predeclared failure conditions",
        "Data plan",
        "Timing and availability",
        "Minimal strategy",
        "Cost and risk model",
        "Evaluation",
        "Robustness plan",
        "Predeclared PASS/REJECT rule",
        "Change log",
    ):
        assert f"## {section}" in text or section in text, f"missing section: {section}"
    assert "Amendments after results are observed invalidate" in text
    assert "does not authorize live trading" in text


def test_data_checklist_covers_timing_and_availability() -> None:
    text = ARTIFACTS["data"].read_text()
    for section in ("## A. Provenance", "## B. Quality checks", "## C. Timing and availability"):
        assert section in text
    assert "UNKNOWN" in text
    assert "future function" in ARTIFACTS["preregistration"].read_text().lower()
    assert "tradable" in text


def test_run_manifest_requires_pins_and_verdict() -> None:
    text = ARTIFACTS["manifest"].read_text()
    for marker in (
        "preregistration reference and sha256",
        "repository sha256",
        "random seeds",
        "checksum of inputs",
        "cost model applied",
        "PASS / REJECT / INCONCLUSIVE",
        "A run without a manifest is not evidence",
    ):
        assert marker in text, f"missing marker: {marker}"


def test_cost_model_is_explicitly_unverified_by_default() -> None:
    text = ARTIFACTS["costs"].read_text()
    assert "PLACEHOLDER_UNVERIFIED" in text
    for cost in (
        "fees",
        "funding",
        "borrow",
        "spread",
        "slippage",
        "impact",
        "latency",
        "partial fills",
        "liquidation and ADL",
        "roll costs",
    ):
        assert cost in text, f"missing cost category: {cost}"
    assert "x2" in text
    assert "does not authorize live trading" in text


def test_evaluation_gate_requires_metrics_robustness_and_separation() -> None:
    text = ARTIFACTS["gate"].read_text()
    for metric in (
        "total return",
        "annualized return",
        "annualized volatility",
        "maximum drawdown",
        "Sharpe",
        "Calmar",
        "win rate",
        "profit factor",
        "turnover",
        "holding period",
        "benchmark comparison",
    ):
        assert metric in text, f"missing metric: {metric}"
    for rule in (
        "lookahead",
        "out-of-sample failure",
        "data-mining risk",
        "does not authorize",
    ):
        assert rule in text, f"missing rule: {rule}"
    assert "train / test separation" in text
    assert "walk-forward" in text


def test_research_never_authorizes_live_trading() -> None:
    for name, path in ARTIFACTS.items():
        text = path.read_text()
        assert "does not authorize live trading" in text, name


@pytest.mark.parametrize("name", LEGACY_RUNNERS)
def test_legacy_direct_file_runners_cannot_write_new_formal_results(name: str) -> None:
    script = ROOT / "scripts" / name
    module = SourceFileLoader(f"retired_{name.replace('-', '_')}", str(script)).load_module()
    with pytest.raises(SystemExit, match="VERIFIED curated Data Layer V1 dataset ID/SHA"):
        module.main([])


def test_every_active_formal_result_writer_consumes_verified_curated_v1() -> None:
    writers = []
    for script in sorted((ROOT / "scripts").glob("gmaq-research-*")):
        source = script.read_text()
        if "results.json" not in source:
            continue
        writers.append(script.name)
        assert 'parser.add_argument("--dataset-id", required=True)' in source
        assert "verify_snapshot(" in source
        assert 'minimum_stage="curated"' in source
        assert "expected_dataset=" in source
        assert "urllib" not in source
        assert "requests" not in source
    assert writers == ["gmaq-research-ls-tsmom", "gmaq-research-spot-perp-carry"]


def test_post_result_remediation_is_explicit_and_blocks_promotion() -> None:
    text = (ROOT / "configs" / "RESEARCH_REMEDIATION.md").read_text()
    assert "PROMOTION_BLOCKED = TRUE" in text
    for defect in (
        "stop loss",
        "daily risk metrics",
        "funding",
        "spread stress",
        "latency stress",
        "cost provenance",
        "funding history",
    ):
        assert defect in text
    assert "today's top-100" in text
    assert "not silently overwritten" in text
    assert "does not authorize Demo entries or live trading" in text


def test_pending_carry_study_cannot_bypass_data_layer_v1() -> None:
    path = (
        RESEARCH
        / "backtests"
        / "study-2026-08-20-btceth-spot-perp-carry"
        / "preregistration.md"
    )
    text = path.read_text()
    for marker in (
        "WAITING_FOR_VERIFIED_DATASET",
        "raw -> validated -> curated",
        'verify_snapshot(..., minimum_stage="curated")',
        "curated dataset ID",
        "snapshot-manifest SHA-256",
        "every input-file SHA-256",
        "No runner may fetch Binance",
        "UNASSIGNED",
        "one formal run",
        "does not authorize building",
        "exchange-bound",
    ):
        assert marker in text
    binding = (path.parent / "dataset-binding.md").read_text()
    for marker in (
        "DATASET_BOUND_READY_FOR_RUNNER",
        "9601a8ff1cbfd52b75744d3380bf7b0961d289c11d3ef4641c5c4e42cd38aee8",
        "d0afd4e8e448859933cedfeb83bf8edc6fb185ed047c3e4439ec312f3e61c01d",
        "31f35151bd5c9c6135a2ece74937aee0a5caa8f17c7964fea60fc6d4bae6c652",
        "VERIFIED / curated / PASS",
        'verify_snapshot(..., minimum_stage="curated")',
        "contain no exchange network client",
    ):
        assert marker in binding
    readme = (RESEARCH / "README.md").read_text()
    assert "Next frozen study" in readme
    assert "DATASET_BOUND_READY_FOR_RUNNER" in readme
