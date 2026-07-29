from __future__ import annotations

import pytest

from global_quant.gate1a import scenario_oracle
from global_quant.gate1a.scenario_oracle import ScenarioOracleError
from global_quant.gate1a.scenario_oracle import load_frozen_oracle


def test_frozen_oracle_rejects_checksum_drift(tmp_path, monkeypatch) -> None:
    changed = tmp_path / "changed-oracle.json"
    changed.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(scenario_oracle, "ORACLE_PATH", changed)

    with pytest.raises(ScenarioOracleError, match="checksum mismatch"):
        load_frozen_oracle()
