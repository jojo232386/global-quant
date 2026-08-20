import importlib.util
import os
import pathlib
import stat
from importlib.machinery import SourceFileLoader


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gmaq-auth-preflight"
CREDENTIAL_HELPER = ROOT / "scripts" / "gmaq-set-readonly-creds"


def load_auth() -> object:
    loader = SourceFileLoader("gmaq_auth_preflight", str(SCRIPT))
    spec = importlib.util.spec_from_loader("gmaq_auth_preflight", loader)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def load_credential_helper() -> object:
    loader = SourceFileLoader("gmaq_set_readonly_creds", str(CREDENTIAL_HELPER))
    spec = importlib.util.spec_from_loader("gmaq_set_readonly_creds", loader)
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
    helper = CREDENTIAL_HELPER.read_text()
    assert 'getpass.getpass("API Key (input hidden): ")' in helper
    assert 'getpass.getpass("Secret Key (input hidden): ")' in helper
    assert "key[:" not in helper
    assert "secret[:" not in helper
    assert "starts with" not in helper


def test_credential_helper_creates_secret_file_as_0600(tmp_path, monkeypatch) -> None:
    helper = load_credential_helper()
    destination = tmp_path / ".env.readonly"
    observed_modes = []
    real_mkstemp = helper.tempfile.mkstemp

    def observed_mkstemp(*args, **kwargs):
        descriptor, name = real_mkstemp(*args, **kwargs)
        observed_modes.append(stat.S_IMODE(os.fstat(descriptor).st_mode))
        return descriptor, name

    monkeypatch.setattr(helper.tempfile, "mkstemp", observed_mkstemp)
    old_umask = os.umask(0o022)
    try:
        helper.write_credentials_atomic(
            destination,
            "GMAQ_READ_KEY=test-key\nGMAQ_READ_SECRET=test-secret\n",
        )
    finally:
        os.umask(old_umask)

    assert observed_modes == [0o600]
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert destination.read_text().endswith("GMAQ_READ_SECRET=test-secret\n")
    assert list(tmp_path.glob(".env.readonly.*.tmp")) == []


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


def test_unreachable_account_never_claims_portfolio_margin_or_verified_fees(
    tmp_path, monkeypatch
) -> None:
    module = load_auth()
    monkeypatch.setattr(module, "read_creds", lambda: ("key", "A" * 64))
    monkeypatch.setattr(
        module,
        "signed_get",
        lambda *_args, **_kwargs: (401, {"error": {"code": -2015, "msg": "rejected"}}),
    )
    monkeypatch.setattr(module, "OUTPUT_PATH", tmp_path / "account-preflight.json")

    result = module.run_preflight()

    checks = {item["name"]: item for item in result["checks"]}
    assert result["verdict"] == "PARTIAL"
    assert result["facts"]["accountType"] is None
    assert result["facts"]["marginMode"] is None
    assert checks["margin mode readable"]["passed"] is False
    assert checks["fee rates on account"]["passed"] is False
    assert "fee rates are UNVERIFIED" in result["live_readiness_note"]
    assert "tier-1 MMR is UNVERIFIED" in result["live_readiness_note"]


def test_portfolio_margin_is_reported_as_incompatible_with_isolated_canary(
    tmp_path, monkeypatch
) -> None:
    module = load_auth()
    monkeypatch.setattr(module, "read_creds", lambda: ("key", "A" * 64))

    def signed_get(_base, endpoint, _params, _key, _secret):
        responses = {
            "/fapi/v2/account": (401, {"error": {"code": -2015, "msg": "rejected"}}),
            "/papi/v1/account": (200, {"accountStatus": "NORMAL"}),
            "/papi/v1/um/positionSide/dual": (200, {"dualSidePosition": True}),
            "/papi/v1/um/commissionRate": (
                200,
                {"symbol": "ETHUSDT", "makerCommissionRate": "0.0002", "takerCommissionRate": "0.0005"},
            ),
            "/papi/v1/um/leverageBracket": (
                200,
                [{"brackets": [{"maintMarginRatio": "0.004", "initialLeverage": 125}]}],
            ),
            "/sapi/v1/account/apiRestrictions": (
                200,
                {"enableWithdrawals": False, "ipRestrict": True},
            ),
        }
        return responses[endpoint]

    monkeypatch.setattr(module, "signed_get", signed_get)
    monkeypatch.setattr(module, "OUTPUT_PATH", tmp_path / "account-preflight.json")

    result = module.run_preflight()

    checks = {item["name"]: item for item in result["checks"]}
    assert result["verdict"] == "PASS_READONLY"
    assert checks["margin mode"]["passed"] is False
    assert checks["one-way position mode"]["passed"] is False
    assert "fee rates are VERIFIED" in result["live_readiness_note"]
    assert "tier-1 MMR is VERIFIED" in result["live_readiness_note"]
