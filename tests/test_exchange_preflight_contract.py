import importlib.util
import pathlib
from importlib.machinery import SourceFileLoader


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gmaq-exchange-preflight"


def load_preflight() -> object:
    loader = SourceFileLoader("gmaq_exchange_preflight", str(SCRIPT))
    spec = importlib.util.spec_from_loader("gmaq_exchange_preflight", loader)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_script_uses_only_public_market_data_endpoints() -> None:
    text = SCRIPT.read_text()
    for endpoint in (
        "/fapi/v1/time",
        "/fapi/v1/exchangeInfo",
        "/fapi/v1/fundingInfo",
        "/fapi/v1/fundingRate",
        "/fapi/v1/depth",
        "/fapi/v1/ticker/bookTicker",
    ):
        assert endpoint in text, f"missing endpoint: {endpoint}"
    # Auth-required endpoints are not queried by this credential-free script.
    assert "leverageBracket" not in text
    assert "commissionRate" not in text
    assert "X-MBX-APIKEY" not in text
    # "secret" may appear only inside the credential-presence guard.
    assert text.count('get("secret")') == 1
    assert "signature" not in text
    assert "HMAC" not in text


def test_script_refuses_credential_bearing_config() -> None:
    module = load_preflight()
    config = module.load_config()
    assert module.config_is_credential_free(config) is True
    text = SCRIPT.read_text()
    assert "config carries credentials" in text


def test_symbol_resolution_matches_config() -> None:
    module = load_preflight()
    config = module.load_config()
    assert module.symbol_from_config(config) == "ETHUSDT"


def test_filter_and_contract_extraction() -> None:
    module = load_preflight()
    sample = {
        "symbol": "ETHUSDT",
        "status": "TRADING",
        "contractType": "PERPETUAL",
        "onboardDate": 1600000000000,
        "baseAsset": "ETH",
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "pricePrecision": 2,
        "quantityPrecision": 3,
        "requiredMarginPercent": "5.0",
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
            {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
            {"filterType": "MIN_NOTIONAL", "notional": "100"},
        ],
    }
    contract = module.extract_contract(sample)
    assert contract["status"] == "TRADING"
    assert contract["contractType"] == "PERPETUAL"
    filters = module.extract_filters(sample)
    assert filters == {"tickSize": "0.01", "stepSize": "0.001", "minQty": "0.001", "minNotional": "100"}


def test_leverage_funding_and_spread_extraction() -> None:
    module = load_preflight()
    implied = module.extract_implied_max_leverage({"requiredMarginPercent": "5.0000"})
    assert implied == 20.0
    assert module.extract_implied_max_leverage({"requiredMarginPercent": ""}) is None
    assert module.extract_implied_max_leverage({}) is None

    info_item = module.filter_funding_info(
        [
            {"symbol": "GTCUSDT", "fundingIntervalHours": 8},
            {"symbol": "ETHUSDT", "fundingIntervalHours": 8, "adjustedFundingRateCap": "0.02"},
        ],
        "ETHUSDT",
    )
    assert info_item["symbol"] == "ETHUSDT"
    funding = module.extract_funding(
        info_item,
        {"symbol": "ETHUSDT", "fundingRate": "0.0001", "fundingTime": 1700000000000},
    )
    assert funding["fundingRate"] == "0.0001"
    assert funding["fundingIntervalHours"] == 8

    spread = module.extract_spread_bps({"bidPrice": "3000.00", "askPrice": "3000.90"})
    assert spread is not None and 2.9 < spread < 3.1
    assert module.extract_spread_bps({"bidPrice": "", "askPrice": "3000.90"}) is None


def test_commission_is_placeholder_until_authenticated_check() -> None:
    module = load_preflight()
    placeholder = module.build_commission_placeholder()
    assert placeholder["status"] == "PLACEHOLDER_UNVERIFIED"
    assert placeholder["taker"] == 0.0005
    assert "fee_rates_on_account" in module.AUTH_REQUIRED_ITEMS


def test_depth_extraction_marks_25usdt_fill_price() -> None:
    module = load_preflight()
    depth = module.extract_depth(
        {
            "bids": [["3000.0", "1.0"], ["2999.5", "2.0"]],
            "asks": [["3000.9", "1.0"], ["3001.4", "2.0"]],
        }
    )
    assert depth["bids"]["best_price_where_25usdt_fills"] == 3000.0
    assert depth["asks"]["cumulative_notional_top5"] > 0
    assert len(depth["bids"]["levels"]) == 2


def test_verdict_is_fail_closed_and_auth_items_block_live() -> None:
    module = load_preflight()
    assert module.final_verdict([{"passed": True}, {"passed": True}]) == "PASS_PUBLIC"
    assert module.final_verdict([{"passed": True}, {"passed": False}]) == "UNKNOWN"
    for item in (
        "margin_mode_on_account",
        "position_mode_on_account",
        "asset_mode_on_account",
        "api_permissions",
        "account_sole_operator",
        "regional_eligibility",
    ):
        assert item in module.AUTH_REQUIRED_ITEMS, item
    manifest = module.run_preflight
    text = SCRIPT.read_text()
    assert '"liveReadiness": "BLOCKED" if AUTH_REQUIRED_ITEMS else "REVIEW"' in text
    assert "user_data" in text and "audit" in text
