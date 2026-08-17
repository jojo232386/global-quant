import importlib.util
import json
import pathlib
from importlib.machinery import SourceFileLoader


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gmaq-research-pit"
FETCH_SCRIPT = ROOT / "scripts" / "gmaq-fetch-pit"
STUDIES = {
    "funding_crosssection": ROOT / "research" / "backtests" / "study-2026-08-17-pit-funding-crosssection",
    "xts_momentum": ROOT / "research" / "backtests" / "study-2026-08-17-pit-xts-momentum",
}


def load(name, path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_universe_uses_d2_volume_without_lookahead() -> None:
    module = load("f", FETCH_SCRIPT)
    DAY = 86_400_000
    base = 1769904000000
    volumes = {
        "A": {base - 2 * DAY: 100.0, base - DAY: 900.0, base: 900.0},
        "B": {base - 2 * DAY: 90.0, base - DAY: 90.0, base: 1000.0},
        "C": {base - 2 * DAY: 80.0, base - DAY: 80.0, base: 80.0},
    }
    universes = module.build_daily_universes(volumes, base, base, top_n=2)
    # Day base must rank by D-2 volumes (A first, B second); D-1/D volumes ignored.
    assert universes[base] == ["A", "B"]
    universes_next = module.build_daily_universes(volumes, base + DAY, base + DAY, top_n=2)
    # D+1 ranks by D-1 (= base) volumes? No: D-2 of D+1 is base-DAY, where
    # A=900 > B=90. B's 1000 volume at base (D-1) must NOT be used.
    assert universes_next[base + DAY] == ["A", "B"]


def test_pit_runner_executes_open_to_open() -> None:
    module = load("p", SCRIPT)
    assert module.PARAMS["stress_cost_bps_per_side"] == 30.0
    day = 1769990400000 + 86_400_000
    symbols = {
        "AAA": {
            "open": {day - 900_000: 90.0, day: 100.0, day + 86_400_000: 110.0},
            "close": {day - 900_000: 95.0, day: 105.0, day + 86_400_000: 115.0},
            "funding": [(day - 900_000, -0.01)],
        },
        "BBB": {
            "open": {day - 900_000: 200.0, day: 210.0, day + 86_400_000: 220.0},
            "close": {day - 900_000: 205.0, day: 215.0, day + 86_400_000: 225.0},
            "funding": [(day - 900_000, -0.02)],
        },
    }
    universes = {day: ["AAA", "BBB"]}
    params = dict(module.PARAMS, legs=1)
    trades = module.run_pit(symbols, universes, "funding_crosssection", 0.0, 0.0, params)
    assert len(trades) == 1
    trade = trades[0]
    assert trade["symbol"] == "BBB"  # lowest funding
    assert trade["entry"] == 210.0  # open of day bar, not the 23:45 close
    assert trade["exit"] == 220.0
    assert trade["net_return"] == 220.0 / 210.0 - 1


def test_pit_runner_charges_funding_inside_hold_window() -> None:
    module = load("p_funding", SCRIPT)
    day = 1770076800000
    symbols = {
        "AAA": {
            "open": {day: 100.0, day + module.DAY_MS: 101.0},
            "close": {day - 900_000: 100.0},
            "funding": [
                (day - 900_001, -0.5),
                (day + 8 * 3_600_000, 0.001),
                (day + module.DAY_MS + 1, 0.5),
            ],
        }
    }
    params = dict(module.PARAMS, legs=1)
    trade = module.run_pit(
        symbols, {day: ["AAA"]}, "funding_crosssection", 0.0, 0.0, params
    )[0]
    assert trade["funding_return"] == 0.001
    assert abs(trade["net_return"] - 0.009) < 1e-12


def test_funding_fetch_pages_the_exact_historical_window() -> None:
    module = load("f_funding", FETCH_SCRIPT)
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


def test_pit_studies_artifacts_and_verdict_consistent() -> None:
    module = load("p", SCRIPT)
    shared = load("shared", ROOT / "scripts" / "gmaq-research-backtest")
    for study_dir in STUDIES.values():
        for name in ("preregistration.md", "data-checklist.md", "results.json", "manifest.md", "verdict.md"):
            assert (study_dir / name).is_file(), f"{study_dir.name}/{name} missing"
        results = json.loads((study_dir / "results.json").read_text())
        expected, _ = shared.verdict(results["out_of_sample"], results["out_of_sample"]["stress"], results["train"])
        assert results["verdict"] in ("PASS", "REJECT")
        assert results["verdict"] == expected, study_dir.name


def test_fetch_script_pins_and_policies() -> None:
    text = FETCH_SCRIPT.read_text()
    assert "top-15 by the 1d quote volume of day D-2" in text
    assert "universes_sha256" in text
    assert "funding_coverage" in text
    assert "funding_gaps" in text
    assert "startTime" in text and "endTime" in text
    assert "X-MBX-APIKEY" not in text
    assert "signature" not in text
