import importlib.util
import json
import pathlib
from importlib.machinery import SourceFileLoader


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gmaq-research-backtest"
STUDY = ROOT / "research" / "backtests" / "study-2026-08-16-eth15m-momentum"


def load_backtest() -> object:
    loader = SourceFileLoader("gmaq_research_backtest", str(SCRIPT))
    spec = importlib.util.spec_from_loader("gmaq_research_backtest", loader)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_study_artifacts_exist_and_are_complete() -> None:
    for name in ("preregistration.md", "data-checklist.md", "results.json", "manifest.md", "verdict.md"):
        assert (STUDY / name).is_file(), f"missing study artifact: {name}"


def test_preregistration_locked_required_sections() -> None:
    text = (STUDY / "preregistration.md").read_text()
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
        assert section in text, f"missing: {section}"
    assert "does not authorize live trading" in text
    assert "Locked before any data fetch" in text


def test_no_lookahead_signal_executes_on_next_open() -> None:
    module = load_backtest()
    # Rising ladder: close[T] > close[T-4] always; entry must use open[T+1].
    n = 100
    bars = {
        "ts": [i * 900_000 for i in range(n)],
        "open": [100.0 + i for i in range(n)],
        "high": [101.0 + i for i in range(n)],
        "low": [99.0 + i for i in range(n)],
        "close": [100.5 + i for i in range(n)],
    }
    trades = module.run_rule(bars, 0.0)
    assert trades, "expected trades on rising ladder"
    first = trades[0]
    # First valid signal is at index 4 (lookback 4); entry at index 5.
    assert first["entry_ts_ms"] == bars["ts"][5]
    assert first["entry"] == bars["open"][5]


def test_flat_market_produces_no_trades() -> None:
    module = load_backtest()
    n = 100
    bars = {
        "ts": [i * 900_000 for i in range(n)],
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [100.0] * n,
    }
    assert module.run_rule(bars, 0.0) == []


def test_stoploss_uses_worse_price() -> None:
    module = load_backtest()
    # Entry 100; exit bar low breaches stop; exit must be stop price 99.
    n = 40
    ts = [i * 900_000 for i in range(n)]
    opens = [100.0] * n
    highs = [102.0] * n
    lows = [99.5] * n
    closes = [101.0] * n
    # force the signal: close[4] > close[0]
    closes[4] = 102.0
    bars = {"ts": ts, "open": opens, "high": highs, "low": lows, "close": closes}
    trades = module.run_rule(bars, 0.0)
    assert trades
    # entry at index 5 = 100; exit bar index 10 low 99.5 > 99 => no stop in this case
    # rebuild with low below stop on the exit bar:
    lows[9] = 98.0
    trades = module.run_rule(bars, 0.0)
    assert trades[0]["exit"] == 99.0


def test_metrics_math_on_synthetic_trades() -> None:
    module = load_backtest()
    trades = [
        {"entry_ts_ms": 0, "exit_ts_ms": 86_400_000, "net_return": 0.01, "notional": 100.0},
        {"entry_ts_ms": 86_400_000, "exit_ts_ms": 172_800_000, "net_return": -0.02, "notional": 100.0},
        {"entry_ts_ms": 172_800_000, "exit_ts_ms": 259_200_000, "net_return": 0.01, "notional": 100.0},
    ]
    m = module.metrics(trades, 1000.0)
    assert m["trade_count"] == 3
    assert abs(m["win_rate"] - 2 / 3) < 1e-4
    assert m["max_consecutive_losses"] == 1
    assert m["profit_factor"] is not None and m["profit_factor"] > 0
    assert m["span_days"] == 2


def test_max_drawdown_and_benchmark() -> None:
    module = load_backtest()
    assert module.max_drawdown([100.0, 110.0, 99.0, 120.0]) == 0.1
    bars = {
        "ts": [0, 86_400_000, 172_800_000],
        "open": [100.0, 110.0, 105.0],
        "high": [100.0, 110.0, 105.0],
        "low": [100.0, 110.0, 105.0],
        "close": [110.0, 105.0, 120.0],
    }
    b = module.benchmark_buy_hold(bars, 0, 172_800_000)
    assert b["gross_return"] == 0.2


def test_rule_registry_covers_all_preregistered_studies() -> None:
    module = load_backtest()
    assert set(module.RULE_FUNCTIONS.keys()) == {
        "momentum",
        "mean_reversion",
        "breakout",
        "session",
        "volfiltered_momentum",
    }
    for study in module.RULE_STUDY.values():
        assert (ROOT / "research" / "backtests" / study).is_dir(), study


def _ladder(n=100, step=1.0, start=100.0):
    return {
        "ts": [i * 900_000 for i in range(n)],
        "open": [start + i * step for i in range(n)],
        "high": [start + 1.0 + i * step for i in range(n)],
        "low": [start - 1.0 + i * step for i in range(n)],
        "close": [start + 0.5 + i * step for i in range(n)],
    }


def test_mean_reversion_triggers_on_falling_and_not_rising() -> None:
    module = load_backtest()
    falling = _ladder(step=-1.0)
    assert module.run_mean_reversion(falling, 0.0), "falling ladder must trigger mean reversion"
    rising = _ladder(step=1.0)
    assert module.run_mean_reversion(rising, 0.0) == []


def test_breakout_triggers_above_prior_high() -> None:
    module = load_backtest()
    n = 60
    bars = {
        "ts": [i * 900_000 for i in range(n)],
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [100.0] * n,
    }
    bars["close"][30] = 105.0  # above max(high[6..29]) = 101
    trades = module.run_breakout(bars, 0.0)
    assert trades and trades[0]["entry_ts_ms"] == bars["ts"][31]


def test_session_rule_only_enters_inside_utc_window() -> None:
    module = load_backtest()
    module.PARAMS["session_start_hour"] = 0
    module.PARAMS["session_end_hour"] = 8
    # Bars aligned to 2026-02-01T00:00Z, 15m apart.
    n = 200
    base = 1769904000000
    bars = {
        "ts": [base + i * 900_000 for i in range(n)],
        "open": [100.0 + 0.01 * i for i in range(n)],
        "high": [101.0 + 0.01 * i for i in range(n)],
        "low": [99.0 + 0.01 * i for i in range(n)],
        "close": [100.5 + 0.01 * i for i in range(n)],
    }
    trades = module.run_session(bars, 0.0)
    assert trades
    for trade in trades:
        hour = (trade["entry_ts_ms"] // 3_600_000) % 24
        assert 0 <= hour < 8, hour


def test_volatility_filter_helpers() -> None:
    module = load_backtest()
    returns = [0.0, 0.01, -0.01, 0.0, 0.02, -0.02, 0.0, 0.0]
    vols = module.rolling_volatility(returns, 3)
    assert vols[3] > 0
    assert module.vol_below_median(vols, 3, 2) is False  # index < window
    # Construct: early high vol, later low vol -> later values below median.
    noisy = [0.0] + [0.05, -0.05, 0.04, -0.04, 0.06, -0.06] + [0.0001] * 20
    vols2 = module.rolling_volatility(noisy, 4)
    idx = 10
    assert module.vol_below_median(vols2, idx, 6) is True


def test_all_studies_artifacts_complete_and_verdict_consistent() -> None:
    module = load_backtest()
    for study in module.RULE_STUDY.values():
        d = ROOT / "research" / "backtests" / study
        for name in ("preregistration.md", "data-checklist.md", "results.json", "manifest.md", "verdict.md"):
            assert (d / name).is_file(), f"{study}/{name} missing"
        results = json.loads((d / "results.json").read_text())
        expected, _ = module.verdict(results["out_of_sample"], results["out_of_sample"]["stress"], results["train"])
        assert results["verdict"] in ("PASS", "REJECT")
        assert results["verdict"] == expected, study


def test_results_verdict_is_consistent_with_rule() -> None:
    module = load_backtest()
    results = json.loads((STUDY / "results.json").read_text())
    oos = results["out_of_sample"]
    stress = oos["stress"]
    train = results["train"]
    expected, _ = module.verdict(oos, stress, train)
    assert results["verdict"] in ("PASS", "REJECT")
    assert results["verdict"] == expected
    assert results["note"].startswith("This study does not authorize live trading")
