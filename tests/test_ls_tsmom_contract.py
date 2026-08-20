from importlib.machinery import SourceFileLoader
import pathlib

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gmaq-research-ls-tsmom"


def load_runner():
    return SourceFileLoader("gmaq_ls_tsmom_test", str(SCRIPT)).load_module()


def flat_data(module, start, end, *, price=100.0):
    bars = {}
    for symbol in module.SYMBOLS:
        bars[symbol] = {
            timestamp: {"open": price, "high": price, "low": price, "close": price}
            for timestamp in range(start, end + module.DAY_MS, module.DAY_MS)
        }
    return {"bars": bars, "funding": {symbol: {} for symbol in module.SYMBOLS}}


def test_runner_has_only_verified_curated_data_layer_v1_entrypoint():
    source = SCRIPT.read_text()
    assert 'verify_snapshot(' in source
    assert 'minimum_stage="curated"' in source
    assert 'parser.add_argument("--dataset-id", required=True)' in source
    assert "--data-dir" not in source
    for forbidden in ("urllib", "requests", "httpx", "api.binance", "fapi.binance"):
        assert forbidden not in source


def test_frozen_dataset_id_is_rejected_before_snapshot_verification(monkeypatch, tmp_path):
    module = load_runner()
    called = False

    def unexpected_verifier(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(module, "verify_snapshot", unexpected_verifier)
    with pytest.raises(module.DataLayerError, match="frozen binding"):
        module.load_curated_dataset(tmp_path, "0" * 64)
    assert called is False


def test_verified_curated_snapshot_binding_loads_real_v1_dataset():
    module = load_runner()
    data_root = pathlib.Path("/Users/ASUS/Desktop/gmaq-data")
    if not data_root.is_dir():
        pytest.skip("local Data Layer V1 warehouse is unavailable")
    data = module.load_curated_dataset(data_root, module.DATASET_ID)
    assert data["record"]["stage"] == "curated"
    assert data["record"]["integrity_verdict"] == "VERIFIED"
    assert data["record"]["quality_verdict"] == "PASS"
    assert data["record"]["snapshot_id"] == module.DATASET_ID
    assert data["file_bindings"] == module.EXPECTED_FILES


def test_target_uses_only_completed_sunday_data_and_executes_tuesday():
    module = load_runner()
    monday = 1_704_067_200_000  # 2024-01-01
    start = monday - 260 * module.DAY_MS
    end = monday + 3 * module.DAY_MS
    data = flat_data(module, start, end)
    for symbol in module.SYMBOLS:
        for index, timestamp in enumerate(sorted(data["bars"][symbol])):
            close = 100 + index * 0.1
            data["bars"][symbol][timestamp].update(
                {"open": close, "high": close, "low": close, "close": close}
            )
    expected = module.target_at(data, monday, 180, 63)
    for symbol in module.SYMBOLS:
        data["bars"][symbol][monday] = {
            "open": 1.0,
            "high": 1_000_000.0,
            "low": 0.5,
            "close": 999_999.0,
        }
    assert module.target_at(data, monday, 180, 63) == expected
    events, decisions = module.target_events(data, monday, end, 180, 63, 1)
    assert decisions == 1
    assert events == {monday + module.DAY_MS: expected}


@pytest.mark.parametrize(
    ("weights", "mode", "expected_end"),
    [
        ({"BTCUSDT": 0.5, "ETHUSDT": -0.5}, "actual", 1_000.0),
        ({"BTCUSDT": 0.5, "ETHUSDT": -0.5}, "adverse", 995.0),
    ],
)
def test_signed_funding_and_adverse_funding_accounting(weights, mode, expected_end):
    module = load_runner()
    start = 1_704_067_200_000
    end = start + module.DAY_MS
    data = flat_data(module, start, end)
    for symbol in module.SYMBOLS:
        data["funding"][symbol][start] = [(0.001, 100.0)]
    simulation = module.simulate(data, start, end, {start: weights}, 1, 0.0, mode)
    assert simulation["equity_curve"][-1] == pytest.approx(expected_end)


def test_long_to_short_flip_charges_close_plus_open_turnover():
    module = load_runner()
    start = 1_704_067_200_000
    end = start + 2 * module.DAY_MS
    data = flat_data(module, start, end)
    events = {
        start: {"BTCUSDT": 0.5, "ETHUSDT": 0.0},
        start + module.DAY_MS: {"BTCUSDT": -0.5, "ETHUSDT": 0.0},
    }
    simulation = module.simulate(data, start, end, events, 2, 0.0, "actual")
    # Entry .5, signed +.5 to -.5 flip 1.0, final liquidation .5.
    assert simulation["turnover"] == pytest.approx(2.0)
    assert simulation["executed_orders"] == 3


def test_short_weighted_average_entry_and_adverse_excursion():
    module = load_runner()
    start = 1_704_067_200_000
    end = start + 2 * module.DAY_MS
    data = flat_data(module, start, end)
    data["bars"]["BTCUSDT"][start + module.DAY_MS].update(
        {"open": 120.0, "high": 130.0, "low": 100.0, "close": 120.0}
    )
    data["bars"]["BTCUSDT"][end].update(
        {"open": 120.0, "high": 120.0, "low": 120.0, "close": 120.0}
    )
    events = {
        start: {"BTCUSDT": -0.25, "ETHUSDT": 0.0},
        start + module.DAY_MS: {"BTCUSDT": -0.50, "ETHUSDT": 0.0},
    }
    simulation = module.simulate(data, start, end, events, 2, 0.0, "actual")
    assert simulation["worst_short_excursion"] > 0.20
    assert simulation["worst_short_excursion"] < 0.30


def test_score_rejects_weak_synthetic_result():
    module = load_runner()
    metric = {
        "annualized_sharpe": 0.0,
        "net_total_return": 0.0,
        "max_drawdown": 0.0,
        "scheduled_decisions": 120,
        "largest_absolute_symbol_contribution_share": 0.5,
    }
    results = {
        "oos": {
            "baseline": metric,
            "stress": metric,
            "delayed_stress": metric,
            "benchmark_50_50": metric,
            "cash_benchmark_return": 0.0,
        },
        "train": {"baseline": metric},
        "walk_forward_halves": [metric, metric],
        "trend_neighbor_stress": {"126": metric, "252": metric},
        "vol_neighbor_stress": {"42": metric, "84": metric},
        "bootstrap_positive_probability": 0.0,
        "short_risk": {"maximum_adverse_excursion_all_runs": 0.0},
        "dataset_binding": {"integrity_verdict": "VERIFIED", "quality_verdict": "PASS"},
    }
    verdict, checks = module.score(results)
    assert verdict == "REJECT"
    assert checks["oos_return_positive"] is False
    assert checks["oos_sharpe_gte_0_70"] is False
