"""Static integrity checks for the EXPL-017-FORMAL-003 pre-metric freeze."""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research" / "exploration"))
sys.path.insert(0, str(ROOT / "research" / "data"))
import expl_017_formal_consumer as consumer  # noqa: E402
import expl_017_lifecycle_v1 as lifecycle  # noqa: E402


def digest(relative: str) -> str:
    return hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()


def test_formal_003_freeze_binds_the_reviewed_consumer_and_all_data_identities():
    payload = json.loads(
        (ROOT / "research" / "exploration" / "expl-017-formal-003-freeze.json").read_text()
    )
    assert (payload["formal_run_id"], payload["hypothesis_id"], payload["implementation_attempt_id"]) == (
        "EXPL-017-FORMAL-003", "EXPL-017", "EXPL-017-IMPL-016"
    )
    identity = payload["identity"]
    assert identity["price_v1"]["snapshot_sha256"] == lifecycle.PRICE_DATASET_SHA
    assert identity["price_v1"]["manifest_sha256"] == lifecycle.PRICE_MANIFEST_SHA
    assert identity["price_v1"]["pit_universe_sha256"] == lifecycle.PRICE_PIT_SHA
    assert identity["lifecycle_v1"]["dataset_sha256"] == digest(identity["lifecycle_v1"]["path"])
    assert identity["composite"]["sha256"] == digest(identity["composite"]["path"])
    assert identity["gold_oracle"]["sha256"] == digest(identity["gold_oracle"]["path"])
    assert identity["reviewed_core"]["sha256"] == digest(identity["reviewed_core"]["path"])
    assert identity["formal_consumer"]["sha256"] == digest(identity["formal_consumer"]["path"])
    assert identity["formal_runner"]["sha256"] == digest(identity["formal_runner"]["path"])
    assert identity["horizon_preflight"]["generator_sha256"] == digest(identity["horizon_preflight"]["generator_path"])
    assert consumer.FORMAL_METRICS_EXPOSED is False


def test_formal_003_is_a_single_run_pre_metric_contract_with_frozen_required_fields():
    payload = json.loads(
        (ROOT / "research" / "exploration" / "expl-017-formal-003-freeze.json").read_text()
    )
    assert payload["status"] == "FROZEN_AWAITING_INDEPENDENT_CONTRACT_REVIEW"
    assert payload["formal_execution"]["maximum_run_count"] == 1
    assert payload["formal_execution"]["current_run_count"] == 0
    assert payload["pre_freeze_performance_state"] == {
        "formal_run_count": 0,
        "formal_performance_computed": False,
        "formal_performance_printed": False,
        "formal_performance_serialized": False,
    }
    for field in ("mechanism", "formula", "parameters", "periods", "costs", "horizon_rules", "pass_fail_criteria"):
        assert payload[field]
