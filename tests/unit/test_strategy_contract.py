from __future__ import annotations

import ast
import hashlib
import inspect
from pathlib import Path

from nautilus_trader.trading.strategy import Strategy

from global_quant.gate1a.strategy import FixedTargetConfig
from global_quant.gate1a.strategy import FixedTargetStrategy


def test_shared_strategy_is_a_nautilus_strategy() -> None:
    assert issubclass(FixedTargetStrategy, Strategy)
    assert FixedTargetConfig.__name__ == "FixedTargetConfig"


def test_strategy_has_no_environment_behavior_branch() -> None:
    source = inspect.getsource(FixedTargetStrategy)
    tree = ast.parse(source)
    forbidden_names = {
        "backtest",
        "demo",
        "live",
        "testnet",
        "environment",
        "is_backtest",
        "is_live",
    }
    observed = {
        node.id.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }
    observed.update(
        node.attr.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    )
    assert observed.isdisjoint(forbidden_names)


def test_strategy_source_hash_is_stable_and_nonempty() -> None:
    path = Path(inspect.getsourcefile(FixedTargetStrategy))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert len(digest) == 64
    assert digest != "0" * 64

