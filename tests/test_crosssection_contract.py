import importlib.util
import json
import pathlib
from importlib.machinery import SourceFileLoader


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gmaq-research-crosssection"
UNIVERSE_SCRIPT = ROOT / "scripts" / "gmaq-fetch-universe"
STUDIES = {
    "funding_crosssection": ROOT / "research" / "backtests" / "study-2026-08-16-multi-funding-crosssection",
    "xts_momentum": ROOT / "research" / "backtests" / "study-2026-08-16-multi-xts-momentum",
}


def load(module_name, path):
    loader = SourceFileLoader(module_name, str(path))
    spec = importlib.util.spec_from_loader(module_name, loader)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_universe_excludes_stablecoin_bases() -> None:
    module = load("u", UNIVERSE_SCRIPT)
    payload = [
        {"symbol": "BTCUSDT", "quoteVolume": "9000"},
        {"symbol": "ETHUSDT", "quoteVolume": "8000"},
        {"symbol": "USDCUSDT", "quoteVolume": "99999"},
        {"symbol": "BUSDUSDT", "quoteVolume": "99998"},
        {"symbol": "SOLUSDT", "quoteVolume": "7000"},
        {"symbol": "XRPUSDT", "quoteVolume": "6000"},
        {"symbol": "EURUSDT", "quoteVolume": "5000"},
        {"symbol": "BAD", "quoteVolume": "1"},
    ]
    # Stablecoin bases (USDC, BUSD, EUR) and non-USDT quotes are excluded;
    # only 4 eligible symbols remain.
    assert module.select_universe(payload, 5) == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]


def test_funding_and_return_signals() -> None:
    module = load("cs", SCRIPT)
    data = {"funding": [(1000, 0.001), (2000, -0.002), (3000, -0.003), (4000, 0.004)]}
    assert module.funding_mean_until(data, 3500) == (-0.002 + -0.003 + 0.001) / 3
    assert module.funding_mean_until(data, 500) is None
    price_data = {"close": {0: 100.0, 86_400_000: 110.0}}
    assert abs(module.return_24h_until(price_data, 86_400_000) - 0.1) < 1e-9
    assert module.return_24h_until(price_data, 86_400_000 + 900_000) is None


def test_selection_order() -> None:
    module = load("cs", SCRIPT)
    scores = {"A": 0.01, "B": -0.02, "C": 0.03, "D": None}
    assert module.select_legs(scores, 2, reverse=False) == ["B", "A"]
    assert module.select_legs(scores, 2, reverse=True) == ["C", "A"]


def test_rebalance_executes_at_open_without_lookahead() -> None:
    module = load("cs", SCRIPT)
    day = 1769990400000 + 86_400_000  # 2026-02-02T00:00Z
    symbols = {
        "AAA": {
            "ts": [day - 900_000, day, day + 86_400_000],
            "open": {day - 900_000: 90.0, day: 100.0, day + 86_400_000: 110.0},
            "close": {day - 900_000: 95.0, day: 105.0, day + 86_400_000: 115.0},
            "funding": [(day - 900_000, -0.01)],
        },
        "BBB": {
            "ts": [day - 900_000, day, day + 86_400_000],
            "open": {day - 900_000: 200.0, day: 210.0, day + 86_400_000: 220.0},
            "close": {day - 900_000: 205.0, day: 215.0, day + 86_400_000: 225.0},
            "funding": [(day - 900_000, -0.02)],
        },
    }
    params = dict(module.PARAMS, legs=1)
    trades = module.run_cross_section(symbols, "funding_crosssection", 0.0, 0.0, params)
    assert len(trades) == 1
    trade = trades[0]
    # lowest funding is BBB (-0.02); entry must be open of day bar = 210, not close 215
    assert trade["symbol"] == "BBB"
    assert trade["entry"] == 210.0
    assert trade["exit"] == 220.0
    assert trade["net_return"] == 220.0 / 210.0 - 1


def test_cross_section_studies_artifacts_and_verdict_consistent() -> None:
    module = load("cs", SCRIPT)
    shared = load("shared", ROOT / "scripts" / "gmaq-research-backtest")
    for study_dir in STUDIES.values():
        for name in ("preregistration.md", "data-checklist.md", "results.json", "manifest.md", "verdict.md"):
            assert (study_dir / name).is_file(), f"{study_dir.name}/{name} missing"
        results = json.loads((study_dir / "results.json").read_text())
        expected, _ = shared.verdict(results["out_of_sample"], results["out_of_sample"]["stress"], results["train"])
        assert results["verdict"] in ("PASS", "REJECT")
        assert results["verdict"] == expected, study_dir.name


def test_results_document_selection_bias_and_single_symbol_dominance() -> None:
    for study_dir in STUDIES.values():
        checklist = (study_dir / "data-checklist.md").read_text()
        assert "survivorship bias" in checklist
        assert "AKEUSDT" in checklist
        results = json.loads((study_dir / "results.json").read_text())
        assert "survivorship bias" in results["note"]
