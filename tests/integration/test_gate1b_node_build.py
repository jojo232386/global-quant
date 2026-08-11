from __future__ import annotations

import inspect
from decimal import Decimal

import global_quant.gate1b.runtime as runtime_module
from global_quant.gate1a.strategy import FixedTargetStrategy
from global_quant.gate1b.runtime import (
    DemoRuntimeInputs,
    OfflineBuildNode,
    build_demo_node,
    build_demo_node_config,
)
from global_quant.gate1b.safety import DemoCredentials


def runtime_inputs(tmp_path) -> DemoRuntimeInputs:
    return DemoRuntimeInputs(
        credentials=DemoCredentials(
            api_key="demo-key-test-only",
            api_secret="demo-secret-test-only",
        ),
        evidence_dir=tmp_path / "evidence",
        ledger_path=tmp_path / "events.jsonl",
        initial_wallet=Decimal("10000"),
        source_hash="source-hash",
        config_hash="config-hash",
    )


def test_build_config_is_credential_free_offline_and_risk_bounded(tmp_path) -> None:
    config = build_demo_node_config(runtime_inputs(tmp_path))

    assert config.network_enabled is False
    assert config.mutation_enabled is False
    assert config.instrument_ids == (
        runtime_module.BTC_ID,
        runtime_module.ETH_ID,
    )
    assert config.max_notional_per_instrument == Decimal("200")
    assert config.max_submitted_orders == 32
    assert "demo-key-test-only" not in repr(config)
    assert "demo-secret-test-only" not in repr(config)


def test_node_builds_offline_with_shared_strategy_and_does_not_connect(tmp_path) -> None:
    node, strategy = build_demo_node(runtime_inputs(tmp_path))
    try:
        assert isinstance(node, OfflineBuildNode)
        assert node.config.network_enabled is False
        assert node.config.mutation_enabled is False
        assert isinstance(strategy, FixedTargetStrategy)
        assert strategy.config.max_notional_per_instrument == Decimal("200")
        assert set(strategy.config.external_order_claims) == {
            strategy.config.btc_instrument_id,
            strategy.config.eth_instrument_id,
        }
    finally:
        node.dispose()
    assert node.disposed is True


def test_runtime_inputs_do_not_retain_compatibility_credentials(tmp_path) -> None:
    inputs = runtime_inputs(tmp_path)

    assert inputs.credentials is None
    assert "demo-key-test-only" not in repr(inputs)
    assert "demo-secret-test-only" not in repr(inputs)


def test_legacy_runtime_has_no_live_execution_factory_or_public_mutation_node() -> None:
    source = inspect.getsource(runtime_module)

    assert "BinanceLiveExecClientFactory" not in source
    assert "BinanceLiveDataClientFactory" not in source
    assert "add_exec_client_factory" not in source
    assert "exec_clients" not in source
    assert "TradingNode" not in source
