from importlib.machinery import SourceFileLoader
import pathlib

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gmaq-research-spot-perp-carry"


def load_runner():
    return SourceFileLoader("gmaq_spot_perp_carry_test", str(SCRIPT)).load_module()


def synthetic_week(module, decision: int, prior_rate: float, holding_rate: float) -> dict:
    execution = decision + module.STEP_MS
    exit_time = execution + module.WEEK_MS
    times = range(execution, exit_time + module.STEP_MS, module.STEP_MS)
    spot = {}
    mark = {}
    funding = {}
    for symbol in module.SYMBOLS:
        spot[symbol] = {timestamp: {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0} for timestamp in times}
        mark[symbol] = {timestamp: {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0} for timestamp in times}
        prior = [(decision - module.WEEK_MS + offset * module.STEP_MS + 4, prior_rate, 100.0) for offset in range(21)]
        holding = [(execution + offset * module.STEP_MS + 4, holding_rate, 100.0) for offset in range(21)]
        funding[symbol] = prior + holding
    return {"spot": spot, "mark": mark, "funding": funding}


def test_runner_has_only_verified_curated_v1_entrypoint() -> None:
    source = SCRIPT.read_text()
    assert 'parser.add_argument("--dataset-id", required=True)' in source
    assert "verify_snapshot(" in source
    assert 'minimum_stage="curated"' in source
    assert "EXPECTED_FILES" in source
    assert "results.json" in source
    for forbidden in ("urllib", "requests", "httpx", "api.binance", "fapi.binance"):
        assert forbidden not in source


def test_wrong_dataset_id_stops_before_registry_access(monkeypatch, tmp_path: pathlib.Path) -> None:
    module = load_runner()
    called = False

    def unexpected(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(module, "verify_snapshot", unexpected)
    with pytest.raises(module.DataLayerError, match="frozen binding"):
        module.load_curated_dataset(tmp_path, "0" * 64)
    assert called is False


def test_real_bound_dataset_replays_verified() -> None:
    module = load_runner()
    data_root = pathlib.Path("/Users/ASUS/Desktop/gmaq-data")
    if not data_root.is_dir():
        pytest.skip("local Data Layer V1 warehouse is unavailable")
    data = module.load_curated_dataset(data_root, module.DATASET_ID)
    assert data["record"]["integrity_verdict"] == "VERIFIED"
    assert data["record"]["stage"] == "curated"
    assert data["record"]["quality_verdict"] == "PASS"
    assert data["file_bindings"] == module.EXPECTED_FILES


def test_signal_uses_exactly_prior_21_settlements() -> None:
    module = load_runner()
    decision = 1_704_067_200_000
    data = synthetic_week(module, decision, 0.001, 0.50)
    assert module.trailing_funding(data, "BTCUSDT", decision) == pytest.approx(0.021)


def test_entry_is_strictly_above_frozen_roundtrip_cost() -> None:
    module = load_runner()
    decision = 1_704_067_200_000
    costs = module.load_costs()
    exact_rate = costs["roundtrip"] / 21
    data = synthetic_week(module, decision, exact_rate, 0.0)
    event = module.decisions(data, decision, decision + 8 * module.DAY_MS, costs["roundtrip"], 1)[0]
    assert event["active"] == []


def test_flat_basis_positive_funding_is_accounted_after_four_sided_cost() -> None:
    module = load_runner()
    decision = 1_704_067_200_000
    data = synthetic_week(module, decision, 0.001, 0.001)
    costs = module.load_costs()
    simulation = module.simulate(data, decision, decision + 8 * module.DAY_MS, costs["baseline"], "actual", 1)
    metric = module.metrics(simulation)
    assert metric["scheduled_decisions"] == 1
    assert metric["active_symbol_weeks"] == 2
    assert metric["basis_pnl"] == pytest.approx(0.0)
    assert metric["funding_pnl"] > metric["transaction_cost"]
    assert metric["net_total_return"] > 0


def test_score_rejects_weak_result() -> None:
    module = load_runner()
    metric = {
        "net_total_return": 0.0, "annualized_sharpe": 0.0, "max_drawdown": 0.0,
        "symbol_net_pnl_contribution": {symbol: 0.0 for symbol in module.SYMBOLS},
        "active_symbol_weeks": 0, "scheduled_decisions": 120,
        "largest_absolute_symbol_contribution_share": 1.0,
    }
    results = {
        "oos": {"baseline": metric, "cost_stress": metric, "funding_stress": metric, "delayed_combined_stress": metric},
        "oos_halves": [{"net_total_return": 0.0}, {"net_total_return": 0.0}],
        "bootstrap_positive_probability": 0.0,
        "dataset_binding": {"integrity_verdict": "VERIFIED", "quality_verdict": "PASS"},
    }
    verdict, checks = module.score(results)
    assert verdict == "REJECT"
    assert checks["oos_return_positive"] is False
    assert checks["active_symbol_weeks_gte_52"] is False
