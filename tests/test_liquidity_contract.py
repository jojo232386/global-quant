import importlib.util
import pathlib
from importlib.machinery import SourceFileLoader


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gmaq-liquidity"
SPEC = ROOT / "configs" / "EXECUTION_COST_MODEL.md"


def load_liquidity() -> object:
    loader = SourceFileLoader("gmaq_liquidity", str(SCRIPT))
    spec = importlib.util.spec_from_loader("gmaq_liquidity", loader)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_cost_model_spec_covers_all_components() -> None:
    text = SPEC.read_text()
    for component in (
        "Market-order fill model",
        "taker fee",
        "spread",
        "slippage",
        "market impact",
        "latency",
        "partial fill",
        "funding carry",
        "liquidation",
        "ADL",
        "Stress methodology",
        "Evidence requirements",
    ):
        assert component in text, f"missing: {component}"
    assert "UNFILLABLE_WITHIN_SNAPSHOT" in text
    assert "does not authorize live trading" in text
    assert "PLACEHOLDER_UNVERIFIED = TRUE" in text


def test_walk_book_fills_vwap_and_remainder() -> None:
    module = load_liquidity()
    levels = [(100.0, 1.0), (99.0, 1.0)]
    fill = module.walk_book(levels, 150.0)
    assert fill["notional_filled"] == 150.0
    assert fill["notional_remaining"] == 0.0
    assert fill["levels_consumed"] == 2
    expected_vwap = 150.0 / (1.0 + 50.0 / 99.0)
    assert abs(fill["vwap"] - expected_vwap) < 1e-6

    short = module.walk_book(levels, 1000.0)
    assert short["notional_remaining"] == 801.0
    assert short["levels_consumed"] == 2

    empty = module.walk_book([], 100.0)
    assert empty["vwap"] is None
    assert empty["notional_filled"] == 0.0


def test_slippage_and_spread_math() -> None:
    module = load_liquidity()
    assert module.slippage_bps(101.0, 100.0) == 100.0
    assert module.slippage_bps(None, 100.0) is None
    assert module.slippage_bps(101.0, 0) is None
    spread = module.spread_from_book(3000.0, 3000.9)
    assert spread["spreadBps"] is not None and 2.9 < spread["spreadBps"] < 3.1
    assert module.spread_from_book(None, 3000.9)["spreadBps"] is None
    assert module.spread_from_book(3000.9, 3000.0)["spreadBps"] is None


def test_depth_stress_scales_quantities() -> None:
    module = load_liquidity()
    levels = [(100.0, 10.0)]
    half = module.scale_depth(levels, 0.5)
    assert half == [(100.0, 5.0)]
    tenth = module.scale_depth(levels, 0.1)
    assert tenth == [(100.0, 1.0)]


def test_spread_and_latency_stress_change_executable_fills() -> None:
    module = load_liquidity()
    bids = [(99.0, 10.0), (98.0, 10.0)]
    asks = [(101.0, 10.0), (102.0, 10.0)]
    spread = module.spread_from_book(99.0, 101.0)
    baseline = module.size_report(100.0, bids, asks, spread, 100.0)
    stressed_spread = {"bid": 98.0, "ask": 102.0, "mid": 100.0, "spreadBps": 400.0}
    stressed = module.size_report(
        100.0,
        module.shift_book_to_best(bids, 98.0),
        module.shift_book_to_best(asks, 102.0),
        stressed_spread,
        100.0,
    )
    assert stressed["buy"]["vwap"] > baseline["buy"]["vwap"]
    assert stressed["sell"]["vwap"] < baseline["sell"]["vwap"]
    assert stressed["all_in_roundtrip_bps"] > baseline["all_in_roundtrip_bps"]

    delayed = module.apply_latency_drift(baseline, 10.0)
    assert delayed["buy"]["vwap"] > baseline["buy"]["vwap"]
    assert delayed["sell"]["vwap"] < baseline["sell"]["vwap"]
    assert delayed["all_in_roundtrip_bps"] == baseline["all_in_roundtrip_bps"] + 20.0


def test_fee_source_is_machine_readable_and_unverified() -> None:
    module = load_liquidity()
    assert module.COST_INPUTS["taker_fee_source"].startswith("PLACEHOLDER_UNVERIFIED")
    assert module.COST_INPUTS["live_authorization"] is False
    assert module.TAKER_FEE == module.COST_INPUTS["taker_fee_rate"]


def test_liquidation_distance_matches_leverage() -> None:
    module = load_liquidity()
    entry = 3000.0
    liq_1x_long = module.liquidation_price(entry, 1.0, 0.005, "long")
    assert liq_1x_long == entry * 0.005
    liq_3x_long = module.liquidation_price(entry, 3.0, 0.005, "long")
    assert abs(liq_3x_long - entry * (1 - 1 / 3 + 0.005)) < 1e-9
    liq_2x_short = module.liquidation_price(entry, 2.0, 0.005, "short")
    assert abs(liq_2x_short - entry * (1 + 1 / 2 - 0.005)) < 1e-9
    assert module.liquidation_price(entry, 0, 0.005, "long") is None


def test_funding_carry_per_interval() -> None:
    module = load_liquidity()
    carry = module.funding_carry(0.0001, 8.0, 100.0, 24.0)
    assert carry == 0.0001 * 100.0 * 24.0 / 8.0
    assert module.funding_carry(None, 8.0, 100.0, 24.0) is None
    assert module.funding_carry(0.0001, 0, 100.0, 24.0) is None


def test_script_is_credential_free_and_fail_closed() -> None:
    text = SCRIPT.read_text()
    for endpoint in ("/fapi/v1/depth", "/fapi/v1/ticker/bookTicker", "/fapi/v1/premiumIndex", "/fapi/v1/fundingRate", "/fapi/v1/fundingInfo"):
        assert endpoint in text, f"missing endpoint: {endpoint}"
    assert "X-MBX-APIKEY" not in text
    assert "signature" not in text
    assert "HMAC" not in text
    assert text.count('get("secret")') == 1
    assert "config carries credentials" in text
    assert '"UNKNOWN"' in text
    assert "does not authorize live trading" in text
