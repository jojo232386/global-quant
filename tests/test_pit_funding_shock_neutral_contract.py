import hashlib
import importlib.util
import json
import pathlib
from importlib.machinery import SourceFileLoader


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "legacy_research_engines" / "gmaq-research-pit-funding-shock-neutral"


def load_script():
    loader = SourceFileLoader("funding_shock_neutral_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader("funding_shock_neutral_test", loader)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def symbol_data(day, prior_rates, latest_rate, entry=100.0, exit_price=101.0):
    funding = []
    for index, rate in enumerate([*prior_rates, latest_rate]):
        funding.append((day - (4 - index) * 8 * 3_600_000, rate))
    return {
        "open": {day: entry, day + 86_400_000: exit_price},
        "close": {},
        "funding": funding,
    }


def test_funding_shock_uses_only_records_visible_at_cutoff():
    module = load_script()
    cutoff = 10_000_000
    data = {"funding": [(1, 0.01), (2, 0.02), (3, 0.03), (4, 0.04), (cutoff + 1, 99.0)]}
    assert module.funding_shock(data, cutoff) == 0.04 - (0.01 + 0.02 + 0.03) / 3


def test_rule_selects_both_frozen_tails_without_overlap():
    module = load_script()
    day = 200_000_000
    symbols = {}
    universe = []
    for index in range(8):
        name = f"S{index}"
        universe.append(name)
        symbols[name] = symbol_data(day, [0.0, 0.0, 0.0], (index - 4) / 10_000)
    trades = module.run_rule(symbols, {day: universe}, cost_per_side=0.0)
    longs = {trade["symbol"] for trade in trades if trade["side"] == "long"}
    shorts = {trade["symbol"] for trade in trades if trade["side"] == "short"}
    assert longs == {"S0", "S1", "S2"}
    assert shorts == {"S5", "S6", "S7"}
    assert not longs & shorts


def test_short_receives_positive_funding_and_costs_both_sides():
    module = load_script()
    day = 200_000_000
    data = symbol_data(day, [0.0, 0.0, 0.0], 0.001, entry=100, exit_price=100)
    data["funding"].append((day + 8 * 3_600_000, 0.001))
    symbols = {f"S{i}": data for i in range(6)}
    trades = module.run_rule(symbols, {day: list(symbols)}, cost_per_side=0.001)
    short = next(trade for trade in trades if trade["side"] == "short")
    assert short["net_return"] == -0.002 + 0.001


def test_study_is_explicitly_non_promotable_with_current_inputs():
    preregistration = (SCRIPT.parents[1] / "research" / "backtests" / module_id() / "preregistration.md").read_text()
    assert "historical listing master" in preregistration
    assert "never authorizes an order" in preregistration


def module_id():
    return "study-2026-08-17-pit-funding-shock-neutral"


def test_committed_result_is_bound_to_frozen_engine_and_preregistration():
    study = ROOT / "research" / "backtests" / module_id()
    result = json.loads((study / "results.json").read_text())
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["study_id"] == module_id()
    assert result["verdict"] == "REJECT"
    assert result["promotion_eligible"] is False
    assert result["engine_sha256"] == digest(SCRIPT)
    assert result["preregistration_sha256"] == digest(study / "preregistration.md")
    assert result["cost_model_sha256"] == digest(ROOT / "configs" / "execution-costs.json")
    assert result["out_of_sample"]["total_return"] < 0
    assert result["out_of_sample"]["stress"]["total_return"] < 0
