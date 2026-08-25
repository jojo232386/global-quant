from __future__ import annotations

import json
from pathlib import Path

from research.oss.vbt_alpha_program_001_preflight import run_preflight


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "research/oss/vbt-alpha-program-001-preflight.json"


def test_metric_free_long_short_preflight_is_deterministic() -> None:
    first = run_preflight()
    second = run_preflight()
    stored = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert first == second == stored
    assert stored["result"] == "PASS_METRIC_FREE"
    assert stored["synthetic_only"] is True
    assert stored["target_percent"] == {"long": 0.5, "short": -0.5, "gross": 1.0, "net": 0.0}
    assert stored["terminal_exit_once"] is True
    assert stored["post_terminal_fill_count"] == 0
    assert stored["metrics_exposed"] == []


def test_preflight_artifact_contains_no_performance_fields() -> None:
    stored = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    forbidden = {"return", "returns", "pnl", "sharpe", "drawdown", "ic", "turnover"}
    assert forbidden.isdisjoint(key.lower() for key in stored)
