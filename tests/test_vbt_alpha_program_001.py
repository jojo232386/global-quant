from __future__ import annotations

import math
import hashlib
import json
from pathlib import Path

import pytest

from research.exploration.price_alpha_v1 import Bar, PriceDataset, load_dataset
from research.oss import vbt_alpha_program_001 as program
from research.oss.vbt_alpha_program_001_preflight import _inputs
from research.oss.vectorbt_pit_baseline import build_portfolio


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_1 = ROOT / "research/exploration/vbt-alpha-program-001-candidate-1-result.json"
CANDIDATE_2 = ROOT / "research/exploration/vbt-alpha-program-001-candidate-2-result.json"
PROGRAM_RESULT = ROOT / "research/exploration/vbt-alpha-program-001-result.json"
CHECKPOINT_B = ROOT / "research/exploration/vbt-alpha-program-001-checkpoint-b.json"
HISTORY = ROOT / "research/process/vbt-alpha-program-001-program-history.json"


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
            )
            for offset in range(31)
        }
    }
    ranges = {
        "AAAUSDT": {
            decision - offset * program.DAY_MS: (
                110.0 if offset == 0 else 101.0,
                90.0 if offset == 0 else 99.0,
            )
            for offset in range(31)
        }
    }
    signal = program.range_volume_signal(_dataset(bars), ranges, ("AAAUSDT",), decision, 20)
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
            points[timestamp] = Bar(value, value, 1.0)
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


def test_range_overlay_matches_the_unchanged_frozen_price_loader() -> None:
    dataset = load_dataset()
    symbol = sorted(dataset.bars)[0]
    overlay = program.load_range_overlay(dataset, (symbol,))
    timestamp = min(dataset.bars[symbol])
    bar = dataset.bars[symbol][timestamp]
    high, low = overlay[symbol][timestamp]
    assert high >= max(bar.open, bar.close)
    assert low <= min(bar.open, bar.close)


def test_both_candidate_failures_and_program_stop_are_recorded() -> None:
    first = json.loads(CANDIDATE_1.read_text(encoding="utf-8"))
    second = json.loads(CANDIDATE_2.read_text(encoding="utf-8"))
    history = json.loads(HISTORY.read_text(encoding="utf-8"))
    result = json.loads(PROGRAM_RESULT.read_text(encoding="utf-8"))
    assert first["candidate_id"] == "CAND-VBT-RANGE-VOLUME-ACCEPTANCE-001"
    assert second["candidate_id"] == "CAND-VBT-CORRELATION-CROWDING-001"
    assert first["result"] == second["result"] == "TIER1_FAIL"
    assert first["all_gates_pass"] is second["all_gates_pass"] is False
    assert history["CANDIDATES_TESTED"] == history["FAIL_COUNT"] == 2
    assert history["PROGRAM_RESULT"] == result["result"] == "VBT_ALPHA_PROGRAM_001_EXHAUSTED"
    assert result["first_pass_candidate"] is None
    assert history["PARAMETER_RESCUE"] is False
    assert hashlib.sha256(CANDIDATE_1.read_bytes()).hexdigest() == history["CANDIDATE_1"]["RESULT_SHA256"]
    assert hashlib.sha256(CANDIDATE_2.read_bytes()).hexdigest() == history["CANDIDATE_2"]["RESULT_SHA256"]


def test_checkpoint_b_independently_approved_the_frozen_closeout() -> None:
    review = json.loads(CHECKPOINT_B.read_text(encoding="utf-8"))
    assert review["program_id"] == "VBT_ALPHA_PROGRAM_001"
    assert review["preregistration_commit"] == "c2cb8661b52625234e9dd11065a05b6655cc8673"
    assert review["candidate_1_history_gate_commit"].startswith("a741139")
    assert review["final_verdict"] == "APPROVE"
    assert review["approve"] is True
    assert review["request_changes"] == "NONE"
    assert review["regression_risk"] == "CLEARED"
    assert all(review["verified"].values()) is False
    assert review["verified"]["preregistration_preceded_performance"] is True
    assert review["verified"]["candidate_limit_respected"] is True
    assert review["verified"]["candidate_1_failure_committed_before_candidate_2"] is True
    assert review["verified"]["parameter_rescue"] is False
    assert review["verified"]["holdout_used"] is False
    assert review["verified"]["freqtrade_runtime_modified"] is False
    assert review["verified"]["forward_capture_modified"] is False
    assert review["verified"]["strategy_or_live_promotion"] is False
    assert review["deterministic_replay"]["result"] == "BYTE_IDENTICAL"
