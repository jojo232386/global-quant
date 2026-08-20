import importlib.util
import json
import pathlib
from importlib.machinery import SourceFileLoader


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "legacy_research_engines" / "gmaq-research-crosssection"
UNIVERSE_SCRIPT = ROOT / "scripts" / "gmaq-fetch-universe"
MULTI_FETCH_SCRIPT = ROOT / "scripts" / "gmaq-fetch-multi"
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
        {"symbol": "../../outsideUSDT", "quoteVolume": "999999"},
        {"symbol": "BTC/USDT", "quoteVolume": "999998"},
        {"symbol": "BAD", "quoteVolume": "1"},
    ]
    # Stablecoin bases (USDC, BUSD, EUR) and non-USDT quotes are excluded;
    # only 4 eligible symbols remain.
    assert module.select_universe(payload, 5) == ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
    assert module.validate_symbol("1000PEPEUSDT") == "1000PEPEUSDT"
    for malicious in ("../../outsideUSDT", "BTC/USDT", "BTC\\USDT", ".USDT"):
        try:
            module.validate_symbol(malicious)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe symbol accepted: {malicious}")


def test_multi_fetch_rejects_unsafe_manifest_symbols() -> None:
    module = load("multi_fetch_symbols", MULTI_FETCH_SCRIPT)
    assert module.validate_symbols(["BTCUSDT", "1000PEPEUSDT"]) == [
        "BTCUSDT",
        "1000PEPEUSDT",
    ]
    for malicious in ("../../outsideUSDT", "BTC/USDT", "BTC\\USDT"):
        try:
            module.validate_symbols([malicious])
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe manifest symbol accepted: {malicious}")


def test_funding_and_return_signals() -> None:
    module = load("cs", SCRIPT)
    assert module.PARAMS["cost_bps_per_side"] == 15.0
    assert len(module.COST_PARAMS["sha256"]) == 64
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


def test_multi_fetch_pages_exact_funding_window() -> None:
    module = load("multi_fetch", MULTI_FETCH_SCRIPT)
    calls = []
    first = [
        {"fundingTime": 1000 + i, "fundingRate": "0.001"}
        for i in range(1000)
    ]
    second = [{"fundingTime": 2000, "fundingRate": "-0.001"}]

    def fake(url):
        calls.append(url)
        return first if len(calls) == 1 else second

    records, complete = module.fetch_funding_history("ETHUSDT", 1000, 3000, fake)
    assert complete is True
    assert len(records) == 1001
    assert "startTime=1000" in calls[0]
    assert "startTime=2000" in calls[1]
    assert "endTime=3000" in calls[0]


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


def test_cross_section_charges_published_funding_during_hold() -> None:
    module = load("cs_funding", SCRIPT)
    day = 1770076800000
    signal = day - module.BAR_MS
    symbols = {
        "AAA": {
            "ts": [signal, day, day + module.DAY_MS],
            "open": {signal: 99.0, day: 100.0, day + module.DAY_MS: 101.0},
            "close": {signal: 100.0, day: 100.0, day + module.DAY_MS: 101.0},
            "funding": [(signal, -0.5), (day + 8 * 3_600_000, 0.001)],
        }
    }
    params = dict(module.PARAMS, legs=1)
    trade = module.run_cross_section(
        symbols, "funding_crosssection", 0.0, 0.0, params
    )[0]
    assert trade["funding_return"] == 0.001
    assert abs(trade["net_return"] - (0.01 - 0.001)) < 1e-12


def test_cross_section_studies_artifacts_and_verdict_consistent() -> None:
    module = load("cs", SCRIPT)
    shared = load("shared", ROOT / "legacy_research_engines" / "gmaq-research-backtest")
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
