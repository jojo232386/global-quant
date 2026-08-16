import importlib.util
import pathlib
from importlib.machinery import SourceFileLoader


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gmaq-auth-preflight"


def load_auth() -> object:
    loader = SourceFileLoader("gmaq_auth_preflight", str(SCRIPT))
    spec = importlib.util.spec_from_loader("gmaq_auth_preflight", loader)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_signing_matches_independent_hmac() -> None:
    module = load_auth()
    # sign() must equal HMAC-SHA256 over the exact sorted query string that
    # signed_get() sends. (The Binance docs example vector uses an unsorted
    # query as-written; our client signs precisely what it transmits.)
    import hashlib
    import hmac
    import urllib.parse

    secret = "test-secret-123"
    params = {"timestamp": 1499827319559, "symbol": "ETHUSDT", "recvWindow": 10000}
    query = urllib.parse.urlencode(sorted(params.items()))
    expected = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    assert module.sign(params, secret) == expected


def test_script_is_get_only_and_never_stores_secrets() -> None:
    text = SCRIPT.read_text()
    assert "X-MBX-APIKEY" in text
    assert "POST" not in text
    assert "data=" not in text
    assert "REDACTED" in text  # balances are not persisted in plaintext
    assert '"secrets": "none stored"' in text
    # The secret field name may only appear where credentials are read.
    assert text.count("GMAQ_READ_SECRET") == 1
    assert text.count('"GMAQ_READ_SECRET"') == 1


def test_endpoints_are_read_only_contracts() -> None:
    text = SCRIPT.read_text()
    for endpoint in (
        "/fapi/v2/account",
        "/fapi/v1/positionSide/dual",
        "/fapi/v1/multiAssetsMargin",
        "/fapi/v1/commissionRate",
        "/fapi/v1/leverageBracket",
        "/sapi/v1/account/apiRestrictions",
    ):
        assert endpoint in text, f"missing endpoint: {endpoint}"
    for forbidden in ("/order", "listenKey", '"/fapi/v1/leverage"'):
        assert forbidden not in text, f"mutation endpoint leaked: {forbidden}"


def test_live_readiness_fields_are_reported() -> None:
    text = SCRIPT.read_text()
    for field in (
        "dualSidePosition",
        "multiAssetsMargin",
        "makerCommissionRate",
        "maintMarginRatio",
        "canWithdraw",
        "ipRestrict",
    ):
        assert field in text, f"missing live-readiness field: {field}"
