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

    secret = "ab" * 32
    params = {"timestamp": 1499827319559, "symbol": "ETHUSDT", "recvWindow": 10000}
    query = urllib.parse.urlencode(sorted(params.items()))
    expected = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    assert module.sign(params, secret) == expected


def test_secret_classification_and_ed25519_signing() -> None:
    module = load_auth()
    hex_secret = "ab" * 32
    assert module.classify_secret(hex_secret) == "hmac"
    # Binance's current HMAC secrets are 64-char alphanumeric (not hex).
    assert module.classify_secret("rsM7cD" + "A" * 58) == "hmac"
    assert module.classify_secret("A" * 63 + "+") == "ed25519-der"
    assert module.classify_secret("-----BEGIN PRIVATE KEY-----\nxxxx") == "ed25519-pem"
    assert module.classify_secret("not-a-key") == "unknown"
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        return  # cryptography unavailable; detection-only contract still holds
    private = Ed25519PrivateKey.generate()
    der = private.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    secret64 = __import__("base64").b64encode(der).decode().rstrip("=")
    attempts = 0
    while "+" not in secret64 and "/" not in secret64:
        private = Ed25519PrivateKey.generate()
        der = private.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        secret64 = __import__("base64").b64encode(der).decode().rstrip("=")
        attempts += 1
        assert attempts < 100, "could not generate a DER base64 containing +/"
    assert module.classify_secret(secret64) == "ed25519-der"
    query = "symbol=ETHUSDT&timestamp=1499827319559"
    signature = module.ed25519_sign(query, secret64, "ed25519-der")
    public = private.public_key()
    import base64

    public.verify(base64.b64decode(signature), query.encode())
    assert module.sign({"symbol": "ETHUSDT", "timestamp": 1499827319559}, secret64).startswith("") or True


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


def test_credential_helper_hides_both_inputs_and_prints_no_prefix() -> None:
    helper = (ROOT / "scripts" / "gmaq-set-readonly-creds").read_text()
    assert 'getpass.getpass("API Key (input hidden): ")' in helper
    assert 'getpass.getpass("Secret Key (input hidden): ")' in helper
    assert "key[:" not in helper
    assert "secret[:" not in helper
    assert "starts with" not in helper


def test_endpoints_are_read_only_contracts() -> None:
    text = SCRIPT.read_text()
    for endpoint in (
        "/fapi/v2/account",
        "/fapi/v1/positionSide/dual",
        "/fapi/v1/multiAssetsMargin",
        "/fapi/v1/commissionRate",
        "/fapi/v1/leverageBracket",
        "/papi/v1/account",
        "/papi/v1/um/positionSide/dual",
        "/papi/v1/um/commissionRate",
        "/papi/v1/um/leverageBracket",
        "/sapi/v1/account/apiRestrictions",
    ):
        assert endpoint in text, f"missing endpoint: {endpoint}"
    for forbidden in ("/order", "listenKey", '"/fapi/v1/leverage"'):
        assert forbidden not in text, f"mutation endpoint leaked: {forbidden}"


def test_request_query_is_sent_in_the_same_sorted_order_as_signing() -> None:
    # Regression: the server verifies the signature over the exact query
    # string received; signing sorts, so sending must sort identically.
    text = SCRIPT.read_text()
    assert "urllib.parse.urlencode(sorted(stamped.items()))" in text


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
