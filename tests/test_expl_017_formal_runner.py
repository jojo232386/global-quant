from __future__ import annotations

import json
import pathlib
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research" / "exploration"))
import expl_017_formal_runner as runner  # noqa: E402
import expl_017_formal_consumer as consumer  # noqa: E402
import expl_017 as core  # noqa: E402


class Fixture:
    is_synthetic = True

    def __init__(self, base):
        self.base = base
        self.symbols = tuple(f"S{index:02d}" for index in range(30))
        self.bars = {symbol: {} for symbol in self.symbols}
        for offset in range(-120, 500):
            stamp = base + offset * core.DAY_MS
            for index, symbol in enumerate(self.symbols):
                price = (100 + index) * (1.0005 + index / 1_000_000) ** (offset + 120)
                self.bars[symbol][stamp] = core.CompletedBar(price, price * 1.0001)

    def universe(self, timestamp): return self.symbols
    def decision_bar(self, symbol, timestamp):
        bar = self.bars[symbol][timestamp]
        return core.DecisionBar(bar.close, 10_000 - self.symbols.index(symbol))
    def execution_open(self, symbol, timestamp): return self.bars[symbol][timestamp].open
    def completed_bar(self, symbol, timestamp): return self.bars[symbol][timestamp]
    def lifecycle_as_of(self, symbol, timestamp): return core.LifecycleStatus(True)
    def forward_return(self, symbol, execution, endpoint):
        return self.execution_open(symbol, endpoint) / self.execution_open(symbol, execution) - 1


def _stamp(year, month, day):
    import datetime as dt
    return int(dt.datetime(year, month, day, tzinfo=dt.UTC).timestamp() * 1000)


def test_runner_verifies_freeze_and_refuses_a_run_without_independent_approval(tmp_path):
    freeze = runner.load_freeze()
    assert freeze["formal_run_id"] == "EXPL-017-FORMAL-003"
    with pytest.raises(runner.FormalRunnerError, match="approval"):
        runner.require_independent_approval(review_path=tmp_path / "missing-review.json")


def test_durable_claim_is_canonical_and_rejects_every_later_invocation(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "CLAIM_PATH", tmp_path / "claim.json")
    monkeypatch.setattr(runner, "RESULT_PATH", tmp_path / "result.json")
    monkeypatch.setattr(runner, "FREEZE_PATH", ROOT / "research" / "exploration" / "expl-017-formal-003-freeze.json")
    runner._claim({"reviewed_implementation_sha": "d" * 40})
    assert json.loads(runner.CLAIM_PATH.read_text())["formal_run_count"] == 1
    with pytest.raises(runner.FormalRunnerError, match="ALREADY_CLAIMED"):
        runner._claim({"reviewed_implementation_sha": "d" * 40})


def test_summarizer_uses_only_consumer_ledger_for_required_report_sections():
    base = _stamp(2021, 4, 1)
    schedule = tuple(
        consumer.HorizonContract(
            base + 7 * index * core.DAY_MS,
            base + (7 * index + 1) * core.DAY_MS,
            base + (7 * index + 8) * core.DAY_MS,
            "train" if index < 9 else "oos",
            index >= 8,
        )
        for index in range(10)
    )
    outcome = consumer.FormalConsumer(Fixture(base), core.Config()).execute_schedule(
        schedule, base + 72 * core.DAY_MS
    )
    report = runner.summarize(outcome, schedule)
    assert set(report) >= {"portfolio", "cost_impact", "ic", "regimes", "concentration", "lifecycle", "turnover"}
    assert set(report["portfolio"][str(core.COST)]) >= {"train", "oos", "holdout", "oos_holdout", "oos_h1", "oos_h2", "holdout_h1", "holdout_h2"}
    assert "per_symbol_net_contribution" in report["concentration"]


def _entry(timestamp, nav, notionals=(), exits=(), phase="OPEN"):
    return core.AccountingEntry(timestamp, nav, nav, tuple(notionals), 0.0, 0.0, tuple(exits), phase == "FINAL_CLOSE", phase)


def test_lifecycle_denominator_is_portfolio_daily_pnl_and_excludes_final_liquidation():
    first, second = _stamp(2022, 1, 3), _stamp(2022, 1, 4)
    final_exit = core.Exit("A", second, 1.1, 0.5, 0.0)
    ledger = (_entry(first, 1.0, (("A", 0.5), ("B", -0.5))), _entry(second, 1.0, (("A", 0.6), ("B", -0.6))), _entry(second, 0.95, (), (final_exit,), "FINAL_CLOSE"))
    outcome = consumer.ConsumerSchedule((), {core.COST: ledger}, {core.COST: ()}, {core.COST: (final_exit,)})
    report = runner._contributions(outcome, ())
    assert report["lifecycle_event_count"] == 0
    assert report["total_absolute_daily_net_pnl"] == pytest.approx(0.05)
    assert report["largest_lifecycle_event_impact"] == 0.0
