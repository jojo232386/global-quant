from __future__ import annotations

import math

import pytest

from research.exploration.price_alpha_v1 import Bar, PriceDataset, load_dataset
from research.oss import vbt_alpha_program_001 as program
from research.oss.vbt_alpha_program_001_preflight import _inputs
from research.oss.vectorbt_pit_baseline import build_portfolio


def _dataset(bars: dict[str, dict[int, Bar]]) -> PriceDataset:
    return PriceDataset(
        bars=bars,
        last_timestamp={symbol: max(values) for symbol, values in bars.items()},
        pit={},
        artifact_path=program.ROOT,
        manifest_sha256="a" * 64,
        pit_sha256="b" * 64,
        labels=(),
    )


def test_range_volume_signal_uses_completed_range_and_prior_volume_only() -> None:
    decision = 40 * program.DAY_MS
    bars = {
        "AAAUSDT": {
            decision - offset * program.DAY_MS: Bar(
                open=100.0,
                close=110.0 if offset == 0 else 100.0,
                quote_volume=math.e * 100.0 if offset == 0 else 100.0,
                high=110.0 if offset == 0 else 101.0,
                low=90.0 if offset == 0 else 99.0,
            )
            for offset in range(31)
        }
    }
    signal = program.range_volume_signal(_dataset(bars), ("AAAUSDT",), decision, 20)
    assert signal["AAAUSDT"] == pytest.approx(1.0)


def test_correlation_crowding_signal_is_finite_and_uses_exact_return_count() -> None:
    decision = 40 * program.DAY_MS
    members = ("AAAUSDT", "BBBUSDT", "CCCUSDT")
    bars: dict[str, dict[int, Bar]] = {}
    for column, symbol in enumerate(members):
        points = {}
        value = 100.0
        for timestamp in range(decision - 30 * program.DAY_MS, decision + program.DAY_MS, program.DAY_MS):
            value *= 1.0 + (0.001 * (column + 1) + 0.0002 * math.sin(timestamp / program.DAY_MS + column))
            points[timestamp] = Bar(value, value, 1.0, value, value)
        bars[symbol] = points
    signal = program.correlation_crowding_signal(_dataset(bars), members, decision, 20)
    assert set(signal) == set(members)
    assert all(math.isfinite(value) for value in signal.values())


def test_portfolio_weights_are_deterministic_market_neutral() -> None:
    signal = {f"S{index:02d}USDT": float(index) for index in range(20)}
    weights = program._weights(signal)
    assert len(weights) == 8
    assert sum(value for value in weights.values() if value > 0) == pytest.approx(0.5)
    assert sum(value for value in weights.values() if value < 0) == pytest.approx(-0.5)
    assert sum(abs(value) for value in weights.values()) == pytest.approx(1.0)
    assert set(symbol for symbol, value in weights.items() if value < 0) == {
        "S00USDT", "S01USDT", "S02USDT", "S03USDT"
    }


def test_hac_direction_is_positive_for_positive_ic_series() -> None:
    result = program._hac(tuple(0.01 + index * 0.0001 for index in range(120)))
    assert result["mean_rank_ic"] > 0
    assert result["one_sided_normal_p"] < 0.05


def test_result_cleaner_preserves_boolean_type() -> None:
    assert program._clean({"passed": True, "failed": False}) == {
        "passed": True,
        "failed": False,
    }


def test_turnover_reads_vectorbt_assets_without_a_grouping_override() -> None:
    inputs = _inputs()
    spec = program.SPECS["1"]
    first = int(inputs.size.index[0].timestamp() * 1000)
    built = program.CandidateInputs(spec, 20, inputs, (first,), (), "a" * 64)
    result = program._turnover(build_portfolio(inputs), built)
    assert result["scheduled_observations"] == 1
    assert result["median_one_way_turnover"] == pytest.approx(1.0)


def test_frozen_price_loader_exposes_manifest_bound_high_and_low() -> None:
    dataset = load_dataset()
    symbol = sorted(dataset.bars)[0]
    bar = dataset.bars[symbol][min(dataset.bars[symbol])]
    assert bar.high is not None and bar.low is not None
    assert bar.high >= max(bar.open, bar.close)
    assert bar.low <= min(bar.open, bar.close)
