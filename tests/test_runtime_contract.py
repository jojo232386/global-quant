import importlib.util
import json
import pathlib
import sys
import types
from datetime import datetime, timedelta, timezone


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "user_data" / "config.json"
STRATEGY_PATH = ROOT / "user_data" / "strategies" / "LiveExecutionCanaryStrategy.py"
IMAGE_DIGEST = "sha256:50720a4af314a812be2cfbf5cc6331c63e9332b06f3f4372241f54bc61a35486"


def load_strategy():
    freqtrade = types.ModuleType("freqtrade")
    persistence = types.ModuleType("freqtrade.persistence")
    strategy = types.ModuleType("freqtrade.strategy")
    pandas = types.ModuleType("pandas")
    persistence.Trade = type("Trade", (), {})
    strategy.IStrategy = type("IStrategy", (), {})
    pandas.DataFrame = type("DataFrame", (), {})
    sys.modules.update(
        {
            "freqtrade": freqtrade,
            "freqtrade.persistence": persistence,
            "freqtrade.strategy": strategy,
            "pandas": pandas,
        }
    )
    spec = importlib.util.spec_from_file_location("gmaq_canary", STRATEGY_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    strategy_dir = str(STRATEGY_PATH.parent)
    sys.path.insert(0, strategy_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(strategy_dir)
    return module.LiveExecutionCanaryStrategy


def test_active_runtime_is_credential_free_freqtrade() -> None:
    config = json.loads(CONFIG_PATH.read_text())
    compose = (ROOT / "docker-compose.yml").read_text()

    assert config["dry_run"] is True
    assert config["exchange"]["key"] == ""
    assert config["exchange"]["secret"] == ""
    assert config["exchange"]["name"] == "binance"
    assert config["trading_mode"] == "futures"
    assert config["margin_mode"] == "isolated"
    assert config["exchange"]["pair_whitelist"] == ["ETH/USDT:USDT"]
    assert config["max_open_trades"] == 1
    assert config["stake_amount"] == 25
    assert config["force_entry_enable"] is False
    assert "username" not in config["api_server"]
    assert "password" not in config["api_server"]
    assert "jwt_secret_key" not in config["api_server"]
    assert "ws_token" not in config["api_server"]
    assert IMAGE_DIGEST in compose
    assert "LiveExecutionCanaryStrategy" in compose
    assert "gmaq-freqtrade-p0-remediation" in compose
    # The remediation stack uses host port 8082; the original and continuation
    # runtimes own 8080 and 8081 respectively.
    assert "GMAQ_HOST_PORT:-8082" in compose
    for binding in ("GMAQ_GATE_ENVIRONMENT", "GMAQ_CANDIDATE_SHA", "GMAQ_CONFIG_SHA256", "GMAQ_RUN_ID"):
        assert binding in compose
    assert "sqlite:////freqtrade/runtime/tradesv3.dryrun.sqlite" in compose
    assert "runtime_data:/freqtrade/runtime" in compose
    assert "runtime_backups:/freqtrade/backups" in compose
    assert 'user: "0:0"' in compose
    assert "chown -R 1000:1000 /freqtrade/runtime /freqtrade/backups" in compose
    assert "FREQTRADE__API_SERVER__PASSWORD" in compose
    assert "FREQTRADE__API_SERVER__JWT_SECRET_KEY" in compose


def test_canary_is_explicitly_not_alpha_and_is_fixed_at_one_x() -> None:
    strategy_type = load_strategy()
    instance = strategy_type()
    instance.config = {"dry_run": True}

    assert strategy_type.NOT_PROVEN_ALPHA is True
    assert strategy_type.can_short is False
    assert strategy_type.timeframe == "15m"
    assert strategy_type.stoploss == -0.01
    assert instance.protections == [
        {"method": "CooldownPeriod", "stop_duration_candles": 24},
        {
            "method": "StoplossGuard",
            "lookback_period_candles": 48,
            "trade_limit": 2,
            "stop_duration_candles": 12,
            "only_per_pair": False,
        },
        {
            "method": "MaxDrawdown",
            "lookback_period_candles": 48,
            "trade_limit": 5,
            "max_allowed_drawdown": 0.05,
        },
    ]
    assert instance.leverage("ETH/USDT:USDT", None, 1.0, 5.0, 20.0, None, "long") == 1.0
    assert hasattr(instance, "confirm_trade_entry")


def test_canary_dry_run_timeout_is_bounded() -> None:
    strategy_type = load_strategy()
    instance = strategy_type()
    instance.config = {"dry_run": True}
    opened = datetime(2026, 8, 15, tzinfo=timezone.utc)
    trade = types.SimpleNamespace(open_date_utc=opened)

    assert instance.custom_exit("ETH/USDT:USDT", trade, opened + timedelta(seconds=119), 1, 0) is None
    assert instance.custom_exit("ETH/USDT:USDT", trade, opened + timedelta(seconds=120), 1, 0) == "canary_timeout"


def test_replaced_execution_trees_are_absent() -> None:
    for path in ("src", "protocols", "evidence", "reviews", "tools", "uv.lock"):
        assert not (ROOT / path).exists()
    assert not (ROOT / "spikes").exists()
    assert not (ROOT / "research" / "CUTOVER_REPORT_2026-08-15.md").exists()
    assert not (ROOT / "research" / "FREQTRADE_SPIKE_2026-08-15.md").exists()


def test_scripts_refuse_non_dry_run_configuration() -> None:
    gmaq = (ROOT / "scripts" / "gmaq").read_text()
    soak = (ROOT / "scripts" / "reliability-soak").read_text()
    assert 'config.get("dry_run") is not True' in gmaq
    assert 'exchange.get("key") or exchange.get("secret")' in gmaq
    assert 'config.get("dry_run") is not True' in soak
    assert 'exchange.get("key") or exchange.get("secret")' in soak
    assert "secrets.token_urlsafe" in gmaq
