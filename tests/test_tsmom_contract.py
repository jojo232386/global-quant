import importlib.util
import pathlib
from importlib.machinery import SourceFileLoader


ROOT = pathlib.Path(__file__).resolve().parents[1]
FETCH_SCRIPT = ROOT / "scripts" / "gmaq-fetch-tsmom"
RUNNER = ROOT / "scripts" / "gmaq-research-tsmom"
DAY_MS = 86_400_000


def load(name: str, path: pathlib.Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def row(ts: int, open_price: float = 100.0, close: float = 101.0) -> list:
    return [ts, str(open_price), str(max(open_price, close) + 1), str(min(open_price, close) - 1), str(close), "10"]


def test_daily_fetch_validation_is_exact_and_end_exclusive() -> None:
    module = load("tsmom_fetch_exact", FETCH_SCRIPT)
    start = 1_700_000_000_000
    start -= start % DAY_MS
    rows = [row(start), row(start + DAY_MS), row(start + 2 * DAY_MS)]
    assert module.validate_daily_rows(rows, start, start + 2 * DAY_MS) == rows[:2]
    try:
        module.validate_daily_rows([rows[0], rows[2]], start, start + 3 * DAY_MS)
    except ValueError as error:
        assert "coverage mismatch" in str(error)
    else:
        raise AssertionError("missing daily bar was accepted")


def test_funding_validation_rejects_duplicate_or_edge_gap() -> None:
    module = load("tsmom_fetch_funding", FETCH_SCRIPT)
    start = 1_700_000_000_000
    end = start + 2 * DAY_MS
    good = [
        {"fundingTime": start + 8 * 3_600_000, "fundingRate": "0.001", "markPrice": "100"},
        {"fundingTime": start + DAY_MS + 16 * 3_600_000, "fundingRate": "-0.001", "markPrice": "101"},
    ]
    assert module.validate_funding(good, start, end, True) == good
    for bad in (good + [good[-1]], []):
        try:
            module.validate_funding(bad, start, end, True)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid funding coverage was accepted")


def synthetic_data(start: int, days: int) -> dict:
    bars = {}
    funding = {}
    for symbol in ("BTCUSDT", "ETHUSDT"):
        symbol_bars = {}
        for offset in range(days + 1):
            ts = start + offset * DAY_MS
            price = 100.0 + offset if symbol == "BTCUSDT" else 300.0 - offset * 0.25
            symbol_bars[ts] = {
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price + 0.5,
            }
        bars[symbol] = symbol_bars
        funding[symbol] = []
    return {"bars": bars, "funding": funding}


def test_signal_uses_previous_completed_daily_close() -> None:
    module = load("tsmom_runner_signal", RUNNER)
    monday = 1_704_067_200_000  # 2024-01-01 Monday
    start = monday - 200 * DAY_MS
    data = synthetic_data(start, 201)
    target = module.target_at(data, monday, 180)
    assert target == {"BTCUSDT": 1.0, "ETHUSDT": 0.0}
    # The current Monday candle is not complete at the decision. Reversing its
    # close must not alter the target.
    data["bars"]["BTCUSDT"][monday]["close"] = 1.0
    data["bars"]["ETHUSDT"][monday]["close"] = 1_000.0
    assert module.target_at(data, monday, 180) == target


def test_delayed_stress_applies_target_one_full_day_later() -> None:
    module = load("tsmom_runner_delay", RUNNER)
    monday = 1_704_067_200_000
    data = synthetic_data(monday - 200 * DAY_MS, 210)
    events, observations = module.build_target_events(data, monday, monday + 7 * DAY_MS, 180, delay_days=1)
    assert observations == 1
    assert monday not in events
    assert monday + DAY_MS in events


def test_portfolio_charges_entry_exit_cost_and_published_funding() -> None:
    module = load("tsmom_runner_accounting", RUNNER)
    start = 1_704_067_200_000
    bars = {
        "BTCUSDT": {
            start: {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            start + DAY_MS: {"open": 110.0, "high": 110.0, "low": 110.0, "close": 110.0},
        },
        "ETHUSDT": {
            start: {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            start + DAY_MS: {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
        },
    }
    no_funding = {"bars": bars, "funding": {"BTCUSDT": [], "ETHUSDT": []}}
    target = {start: {"BTCUSDT": 1.0, "ETHUSDT": 0.0}}
    base = module.simulate_portfolio(no_funding, start, start + DAY_MS, target, 1, 0.01, 1.0)
    assert abs(base["equity_curve"][-1] - 1078.11) < 1e-6

    with_funding = {
        "bars": bars,
        "funding": {
            "BTCUSDT": [(start + 8 * 3_600_000, 0.01, 100.0)],
            "ETHUSDT": [],
        },
    }
    funded = module.simulate_portfolio(with_funding, start, start + DAY_MS, target, 1, 0.01, 1.0)
    assert abs(funded["equity_curve"][-1] - 1068.21) < 1e-6


def test_runner_is_research_only_and_has_frozen_gate() -> None:
    text = RUNNER.read_text()
    assert "private" not in text.lower() or "private API calls" in text
    assert "BOOTSTRAP_SAMPLES = 2_000" in text
    assert "PRIMARY_LOOKBACK = 180" in text
    assert '"PASS" if all(checks.values()) else "REJECT"' in text
    for forbidden in ("/order", "apiKey", "GMAQ_READ_SECRET"):
        assert forbidden not in text
