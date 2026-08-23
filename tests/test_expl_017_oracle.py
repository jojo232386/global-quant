"""Validate the pre-production EXPL-017 oracle without importing a runner."""
from __future__ import annotations

import ast
import json
import pathlib
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
ORACLE_DIR = pathlib.Path(__file__).with_name("oracles")
sys.path.insert(0, str(ORACLE_DIR))
import expl_017_impl_002_oracle as oracle  # noqa: E402


def gold() -> dict:
    return json.loads(
        (ROOT / "research/exploration/expl-017-gold-sample.json").read_text(encoding="utf-8")
    )


def case(name: str) -> dict:
    return next(item for item in gold()["cases"] if item["case"] == name)


def test_oracle_is_independent_of_production_modules():
    path = ORACLE_DIR / "expl_017_impl_002_oracle.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    from_imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module != "__future__"
    }
    assert imports == {"math", "statistics"}
    assert from_imports == set()


def test_oracle_identity_predates_production_attempt():
    payload = gold()
    assert payload["hypothesis_id"] == "EXPL-017"
    assert payload["implementation_attempt_id"] == "EXPL-017-IMPL-002"
    assert payload["oracle_id"] == "EXPL-017-GOLD-ORACLE-002"


def test_rank_regime_and_target_expected_values_are_independently_derived():
    values = oracle.oracle_values()
    normal = case("A_NORMAL_CONTINUOUS_CALM")
    high = case("B_REBALANCE_TIMESTAMP_HIGH_VOL")
    assert values["momentum"] == pytest.approx(normal["momentum_value"])
    assert values["broad_volatility"] == pytest.approx(
        normal["volatility_regime_value"]["broad_statistic"]
    )
    assert values["calm_target"] == normal["target_position"]
    assert values["high_target"] == high["target_position"]


def test_nav_normalized_drift_expected_values_are_independently_derived():
    values = oracle.oracle_values()
    drift = case("D_NAV_NORMALIZED_INCUMBENT_DRIFT")
    assert values["drift_nav"] == pytest.approx(drift["expected_nav"])
    assert values["drift_weights"] == pytest.approx(drift["expected_drifted_weight"])
    assert values["drift_turnover"] == pytest.approx(drift["expected_turnover"])
    assert values["drift_cost_dollars"] == pytest.approx(
        drift["expected_rebalance_cost_dollars"]
    )


def test_terminal_entry_exit_expected_values_are_independently_derived():
    values = oracle.oracle_values()
    terminal = case("C_LIFECYCLE_TERMINAL_CONTRACT")
    assert values["terminal_nav_before_exit"] == pytest.approx(
        terminal["terminal_nav_before_exit"]
    )
    assert values["terminal_exit_turnover"] == pytest.approx(
        terminal["terminal_exit_turnover"]
    )
    assert values["terminal_exit_cost_dollars"] == pytest.approx(
        terminal["terminal_exit_cost_dollars"]
    )
    assert values["terminal_final_nav"] == pytest.approx(
        terminal["portfolio_nav_after_costs_and_marks"]
    )
