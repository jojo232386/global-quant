from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import pathlib
import subprocess
from importlib.machinery import SourceFileLoader

import pytest

from gmaq_live import committed_candidate_sha
from gmaq_live.admission import evaluate_live_candidate, reconcile_binance_usdm_truth, validate_live_config


CANDIDATE = "a" * 40
CONFIG_SHA = "b" * 64
NOW = 1_800_000_000
CAPTURED = datetime.fromtimestamp(NOW - 30, timezone.utc).isoformat()


def load_admission_cli() -> object:
    script = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "gmaq-live-admission"
    loader = SourceFileLoader("gmaq_live_admission_cli", str(script))
    spec = importlib.util.spec_from_loader("gmaq_live_admission_cli", loader)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def write_valid_soak_package(
    path: pathlib.Path,
    *,
    candidate: str = CANDIDATE,
    contract: dict | None = None,
    hours: int = 48,
) -> None:
    path.mkdir()
    contract = contract or {
        "tree_sha": "1" * 40,
        "config_sha256": CONFIG_SHA,
        "compose_sha256": "2" * 64,
        "image_ref": "example.invalid/freqtrade@sha256:" + "3" * 64,
    }
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events = [
        {"ts_utc": start.isoformat(), "event": "E0", "verdict": "EXACT_MATCH"},
        {"ts_utc": start.isoformat(), "event": "E0", "verdict": "PASS"},
    ]
    schedule = {
        "E1": [(hour, "HEALTHY") for hour in range(0, hours, 6)],
        "E2": [(hour, "MATCH") for hour in range(0, hours, 6)],
        "E3": [(hour, "PASS") for hour in range(6, hours, 12)],
        "E4": [(hour, "PASS") for hour in range(12, hours, 24)],
        "E5": [(1, "PASS")],
        "E6": [(2, "PASS")],
        "E7": [(1, "PASS"), (12, "PASS")],
        "E8": [(hour, "PASS") for hour in range(0, hours, 12)],
    }
    for name, rows in schedule.items():
        for hour, verdict in rows:
            events.append(
                {"ts_utc": (start + timedelta(hours=hour)).isoformat(), "event": name, "verdict": verdict}
            )
    events.sort(key=lambda row: row["ts_utc"])

    audit_refs = {
        "candidate_sha": candidate,
        "tree_sha": contract["tree_sha"],
        "config_sha256": contract["config_sha256"],
        "run_id": "dryrun-test-0001",
        "image_digest": contract["image_ref"].split("@", 1)[-1],
    }
    anchor_sha = "0" * 64
    first_audit = {"seq": 11, "event": "start", "prev_sha": anchor_sha, "refs": audit_refs}
    previous = json.dumps(first_audit, sort_keys=True, ensure_ascii=False)
    audit = [
        first_audit,
        {
            "seq": 12,
            "event": "finish",
            "prev_sha": hashlib.sha256(previous.encode()).hexdigest(),
            "refs": audit_refs,
        },
    ]
    objects = {
        "manifest.json": {
            "schema_version": 1,
            "candidate_sha": candidate,
            "tree_sha": contract["tree_sha"],
            "config_sha256": contract["config_sha256"],
            "compose_sha256": contract["compose_sha256"],
            "run_id": "dryrun-test-0001",
            "container": {
                "image_ref": contract["image_ref"],
                "image_id": contract["image_ref"].split("@", 1)[-1],
            },
            "environment": "dry_run",
            "identity_verdict": "EXACT_MATCH",
            "contains_secrets": False,
        },
        "preflight.json": {"verdict": "PASS"},
        "preflight-after-kill.json": {"verdict": "PASS"},
        "exchange-preflight.json": {"verdict": "PASS_PUBLIC"},
        "liquidity.json": {"verdict": "PASS"},
        "trade-baseline.json": {"open_trades": 0, "open_orders": 0},
        "trade-lifecycle.json": {"verdict": "PASS", "complete_canary_trade_count": 1},
        "audit-start-anchor.json": {"seq": 10, "record_sha256": anchor_sha},
        "initial-audit-verify.json": {"verdict": "VERIFIED", "records": 10},
        "final-audit-verify.json": {"verdict": "VERIFIED", "records": 12},
        "final-exit.json": {
            "verdict": "ZERO_POSITIONS_AND_ORDERS",
            "open_trades": 0,
            "open_orders": 0,
            "partial_orders": 0,
            "unknown_outcomes": [],
            "matches_database": True,
        },
    }
    for name, value in objects.items():
        (path / name).write_text(json.dumps(value))
    lines = {
        "events.jsonl": events,
        "audit-journal.jsonl": audit,
        "health-samples.jsonl": [
            {"verdict": "HEALTHY", "captured_at_utc": (start + timedelta(hours=hour)).isoformat()}
            for hour in range(0, hours, 6)
        ],
        "reconcile-records.jsonl": [
            {"verdict": "MATCH", "captured_at_utc": (start + timedelta(hours=hour)).isoformat()}
            for hour in range(0, hours, 6)
        ],
    }
    for name, rows in lines.items():
        (path / name).write_text("".join(json.dumps(row) + "\n" for row in rows))
    (path / "backup-restore.md").write_text("DB_BACKUP_RESTORE=PASS\n")
    (path / "verdict.md").write_text(
        "# Reliability verdict\n\n"
        "- verdict: `PASS`\n"
        "- reason: complete\n"
        "- mode: `soak`\n"
        f"- ended_utc: `{(start + timedelta(hours=hours)).isoformat()}`\n"
    )


def truth_inputs() -> dict:
    client_id = "gmaq-live-eth-00000001"
    event_ms = (NOW - 31) * 1000
    return {
        "intent": {
            "exchange": "binance_usdm",
            "symbol": "ETHUSDT",
            "side": "BUY",
            "quantity": "0.01",
            "position_side": "BOTH",
            "reduce_only": False,
            "client_order_id": client_id,
            "adapter_id": "gmaq-binance-usdm-v1",
            "pre_submit_recorded": True,
            "intent_audit_sha256": "c" * 64,
        },
        "rest_orders": [
            {
                "symbol": "ETHUSDT",
                "clientOrderId": client_id,
                "orderId": 12345,
                "status": "FILLED",
                "origQty": "0.010",
                "executedQty": "0.010",
                "side": "BUY",
                "positionSide": "BOTH",
                "reduceOnly": False,
            }
        ],
        "user_stream_events": [
            {
                "e": "ORDER_TRADE_UPDATE",
                "E": event_ms - 1000,
                "o": {
                    "s": "ETHUSDT",
                    "c": client_id,
                    "S": "BUY",
                    "ps": "BOTH",
                    "R": False,
                    "X": "NEW",
                    "i": 12345,
                    "q": "0.010",
                    "z": "0",
                },
            },
            {
                "e": "ORDER_TRADE_UPDATE",
                "E": event_ms - 500,
                "o": {
                    "s": "ETHUSDT",
                    "c": client_id,
                    "S": "BUY",
                    "ps": "BOTH",
                    "R": False,
                    "X": "FILLED",
                    "i": 12345,
                    "q": "0.010",
                    "z": "0.010",
                },
            },
            {
                "e": "ACCOUNT_UPDATE",
                "E": event_ms,
                "a": {"P": [{"s": "ETHUSDT", "pa": "0.010", "ps": "BOTH"}]},
            },
        ],
        "rest_positions": [{"symbol": "ETHUSDT", "positionAmt": "0.010", "positionSide": "BOTH"}],
        "stream_state": {
            "connected": True,
            "gap_detected": False,
            "session_id": "stream-0001",
            "last_event_epoch_ms": event_ms,
        },
        "candidate_sha": CANDIDATE,
        "config_sha256": CONFIG_SHA,
        "captured_at_utc": CAPTURED,
    }


def matched_broker_evidence() -> dict:
    return reconcile_binance_usdm_truth(**truth_inputs())


def account_evidence() -> dict:
    return {
        "schema_version": 2,
        "scope": "authorized read-only session; GET requests only",
        "fetched_at_utc": CAPTURED,
        "candidate_sha": CANDIDATE,
        "config_sha256": CONFIG_SHA,
        "verdict": "PASS_READONLY",
        "facts": {
            "accountType": "usd_m_futures",
            "canTrade": True,
            "canWithdraw": False,
            "dualSidePosition": False,
            "multiAssetsMarginMode": False,
            "apiRestrictions": {
                "enableFutures": True,
                "enableWithdrawals": False,
                "ipRestrict": True,
            },
            "commission": {"taker": "0.0005", "status": "VERIFIED_ON_ACCOUNT"},
            "leverageBracketTier1": {"maintMarginRatio": "0.004"},
        },
    }


def strategy_result() -> dict:
    return {
        "study_id": "study-pass",
        "verdict": "PASS",
        "dataset_binding": {
            "data_layer_version": 1,
            "integrity_verdict": "VERIFIED",
            "stage": "curated",
            "quality_verdict": "PASS",
            "dataset": "btceth-test",
            "dataset_id": "1" * 64,
            "schema_id": "2" * 64,
            "snapshot_manifest_sha256": "3" * 64,
            "files": {"bars": "4" * 64},
        },
    }


def verified_dataset() -> dict:
    return {
        "integrity_verdict": "VERIFIED",
        "stage": "curated",
        "quality_verdict": "PASS",
        "dataset": "btceth-test",
        "snapshot_id": "1" * 64,
        "schema_id": "2" * 64,
        "manifest_sha256": "3" * 64,
        "files": [{"role": "bars", "sha256": "4" * 64}],
    }


def readiness() -> dict:
    value = {
        "schema_version": 1,
        "captured_at_utc": CAPTURED,
        "candidate_sha": CANDIDATE,
        "config_sha256": CONFIG_SHA,
        "dedicated_account": True,
        "sole_operator": True,
        "risk_limits_approved": True,
        "alert_route_verified": True,
        "secret_storage_approved": True,
        "soak_verdict": "PASS",
        "strategy_artifact_sha256": "",
        "soak_evidence_sha256": "e" * 64,
        "alert_evidence_sha256": "f" * 64,
        "risk_approval_id": "risk-approval-0001",
        "operator_attestation_id": "operator-attestation-0001",
        "secret_storage_approval_id": "secret-storage-0001",
    }
    value["strategy_artifact_sha256"] = hashlib.sha256(
        json.dumps(strategy_result(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return value


def verified_soak() -> dict:
    return {
        "schema_version": 1,
        "verdict": "VERIFIED",
        "candidate_sha": CANDIDATE,
        "tree_sha": "1" * 40,
        "config_sha256": CONFIG_SHA,
        "compose_sha256": "2" * 64,
        "image_digest": "sha256:" + "3" * 64,
        "run_id": "dryrun-test-0001",
        "package_sha256": "e" * 64,
        "duration_seconds": 48 * 3600,
    }


def evaluate(**overrides) -> dict:
    inputs = {
        "account_evidence": account_evidence(),
        "broker_evidence": matched_broker_evidence(),
        "readiness": readiness(),
        "strategy_result": strategy_result(),
        "verified_dataset": verified_dataset(),
        "verified_soak": verified_soak(),
        "candidate_sha": CANDIDATE,
        "config_sha256": CONFIG_SHA,
        "now_epoch": NOW,
    }
    inputs.update(overrides)
    return evaluate_live_candidate(**inputs)


def test_matching_rest_stream_and_position_truth() -> None:
    result = matched_broker_evidence()
    assert result["verdict"] == "MATCH"
    assert result["errors"] == []
    assert result["client_order_id"] == "gmaq-live-eth-00000001"
    assert result["exchange_order_id"] == "12345"
    assert len(result["capture_sha256"]) == 64
    assert result["does_not_authorize_live_trading"] is True


def test_unrelated_later_account_update_does_not_hide_relevant_position() -> None:
    inputs = truth_inputs()
    later = (NOW - 30) * 1000
    inputs["user_stream_events"].append(
        {
            "e": "ACCOUNT_UPDATE",
            "E": later,
            "a": {"P": [{"s": "BTCUSDT", "pa": "0.001", "ps": "BOTH"}]},
        }
    )
    inputs["stream_state"]["last_event_epoch_ms"] = later
    result = reconcile_binance_usdm_truth(**inputs)
    assert result["verdict"] == "MATCH"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda value: value["stream_state"].update(connected=False), "user_stream_not_connected"),
        (lambda value: value["stream_state"].update(gap_detected=True), "user_stream_gap_or_unknown"),
        (lambda value: value["stream_state"].update(last_event_epoch_ms=0), "user_stream_freshness_invalid"),
        (lambda value: value["rest_orders"].append(deepcopy(value["rest_orders"][0])), "rest_order_identity_not_unique"),
        (lambda value: value["rest_orders"][0].update(status="PARTIALLY_FILLED"), "order_status_mismatch"),
        (lambda value: value["rest_orders"][0].update(executedQty="0.005"), "order_filled_mismatch"),
        (lambda value: value["intent"].update(symbol="BTCUSDT"), "intent_symbol_mismatch"),
        (lambda value: value["intent"].update(side="SELL"), "intent_side_mismatch"),
        (lambda value: value["intent"].update(quantity="0.02"), "intent_quantity_mismatch"),
        (lambda value: value["intent"].update(reduce_only=True), "intent_reduce_only_mismatch"),
        (lambda value: value["rest_positions"][0].update(positionAmt="0"), "position_amount_mismatch"),
        (lambda value: value["user_stream_events"].pop(), "stream_position_identity_not_unique"),
        (lambda value: value["intent"].update(adapter_id=""), "exchange_bound_adapter_missing"),
    ],
)
def test_truth_mismatch_is_quarantined(mutation, expected) -> None:
    inputs = truth_inputs()
    mutation(inputs)
    result = reconcile_binance_usdm_truth(**inputs)
    assert result["verdict"] == "QUARANTINE"
    assert expected in result["errors"]


def test_malformed_capture_is_quarantined_without_exception() -> None:
    inputs = truth_inputs()
    inputs["user_stream_events"] = ["not-an-object"]
    result = reconcile_binance_usdm_truth(**inputs)
    assert result["verdict"] == "QUARANTINE"
    assert result["errors"] == ["user_stream_events_invalid"]


def test_nonfinite_numbers_and_naive_capture_time_fail_closed() -> None:
    inputs = truth_inputs()
    inputs["user_stream_events"][1]["o"]["z"] = "NaN"
    inputs["captured_at_utc"] = "2027-01-15T08:00:00"
    result = reconcile_binance_usdm_truth(**inputs)
    assert result["verdict"] == "QUARANTINE"
    assert "order_event_filled_progression_invalid" in result["errors"]
    assert "user_stream_freshness_invalid" in result["errors"]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda event: event.update(s="BTCUSDT"), "order_event_symbol_mismatch"),
        (lambda event: event.update(X="UNKNOWN"), "order_event_status_unknown"),
        (lambda event: event.update(z="0.02"), "order_event_filled_progression_invalid"),
        (lambda event: event.update(X="FILLED", z="0"), "order_event_status_fill_inconsistent"),
    ],
)
def test_earlier_contradictory_order_event_is_quarantined(mutation, expected) -> None:
    inputs = truth_inputs()
    mutation(inputs["user_stream_events"][0]["o"])
    result = reconcile_binance_usdm_truth(**inputs)
    assert result["verdict"] == "QUARANTINE"
    assert expected in result["errors"]


@pytest.mark.parametrize("terminal_status", ["CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"])
def test_nonfilled_terminal_state_cannot_claim_full_fill(terminal_status) -> None:
    inputs = truth_inputs()
    inputs["rest_orders"][0]["status"] = terminal_status
    inputs["user_stream_events"][1]["o"]["X"] = terminal_status
    result = reconcile_binance_usdm_truth(**inputs)
    assert result["verdict"] == "QUARANTINE"
    assert "order_event_status_fill_inconsistent" in result["errors"]


def test_zero_quantity_order_is_quarantined() -> None:
    inputs = truth_inputs()
    inputs["intent"]["quantity"] = "0"
    inputs["rest_orders"][0].update(origQty="0", executedQty="0")
    for event in inputs["user_stream_events"][:2]:
        event["o"].update(q="0", z="0")
    inputs["rest_positions"][0]["positionAmt"] = "0"
    inputs["user_stream_events"][2]["a"]["P"][0]["pa"] = "0"
    result = reconcile_binance_usdm_truth(**inputs)
    assert result["verdict"] == "QUARANTINE"
    assert "order_event_filled_progression_invalid" in result["errors"]


def test_complete_fixture_contract_remains_structurally_blocked() -> None:
    result = evaluate()
    assert result["verdict"] == "BLOCKED"
    assert result["blockers"] == [
        "implementation_authenticated_capture_adapter_missing",
        "implementation_exchange_bound_submission_adapter_missing",
        "implementation_live_credential_account_binding_missing",
    ]
    assert result["entry_authorized"] is False
    assert result["order_submission_enabled"] is False


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("account_evidence", None, "account_evidence_invalid"),
        ("broker_evidence", [], "broker_evidence_invalid"),
        ("readiness", "invalid", "readiness_evidence_invalid"),
        ("verified_soak", None, "soak_evidence_invalid"),
        ("candidate_sha", None, "candidate_sha_invalid"),
        ("config_sha256", [], "config_sha256_invalid"),
        ("now_epoch", True, "evaluation_time_invalid"),
    ],
)
def test_malformed_candidate_inputs_fail_closed(field, value, expected) -> None:
    result = evaluate(**{field: value})
    assert result["verdict"] == "BLOCKED"
    assert expected in result["blockers"]
    assert result["entry_authorized"] is False
    assert result["order_submission_enabled"] is False


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"now_epoch": NOW + 3600}, "account_evidence_stale_or_invalid"),
        ({"candidate_sha": "c" * 40}, "account_candidate_sha_mismatch"),
    ],
)
def test_missing_or_stale_evidence_blocks(overrides, expected) -> None:
    result = evaluate(**overrides)
    assert result["verdict"] == "BLOCKED"
    assert expected in result["blockers"]
    assert result["entry_authorized"] is False


def test_failed_account_and_operational_predicates_block() -> None:
    account = account_evidence()
    account["facts"]["dualSidePosition"] = True
    account["facts"]["canWithdraw"] = True
    account["facts"]["apiRestrictions"]["enableWithdrawals"] = True
    operations = readiness()
    operations["soak_verdict"] = "RUNNING"
    failed_strategy = strategy_result()
    failed_strategy["verdict"] = "REJECT"
    result = evaluate(account_evidence=account, readiness=operations, strategy_result=failed_strategy)
    assert result["verdict"] == "BLOCKED"
    assert "account_dualSidePosition_invalid" in result["blockers"]
    assert "account_canWithdraw_invalid" in result["blockers"]
    assert "account_enableWithdrawals_invalid" in result["blockers"]
    assert "strategy_verdict_invalid" in result["blockers"]
    assert "strategy_artifact_digest_mismatch" in result["blockers"]
    assert "readiness_soak_verdict_invalid" in result["blockers"]


def test_soak_evidence_digest_and_candidate_must_match() -> None:
    wrong_digest = verified_soak()
    wrong_digest["package_sha256"] = "0" * 64
    result = evaluate(verified_soak=wrong_digest)
    assert "soak_evidence_digest_mismatch" in result["blockers"]

    wrong_candidate = verified_soak()
    wrong_candidate["candidate_sha"] = "c" * 40
    result = evaluate(verified_soak=wrong_candidate)
    assert "soak_candidate_sha_mismatch" in result["blockers"]


def test_completed_soak_package_is_replayed_and_tampering_fails(tmp_path) -> None:
    module = load_admission_cli()
    candidate = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True, cwd=pathlib.Path(__file__).parents[1]
    ).strip()
    package = tmp_path / "soak"
    write_valid_soak_package(
        package,
        candidate=candidate,
        contract=module._candidate_runtime_contract(candidate),
    )

    verified = module.load_verified_soak_evidence(str(package), candidate)
    assert verified["verdict"] == "VERIFIED"
    assert verified["duration_seconds"] == 48 * 3600
    assert verified["event_counts"]["E1"] == 8

    extended = tmp_path / "soak-49h"
    write_valid_soak_package(
        extended,
        candidate=candidate,
        contract=module._candidate_runtime_contract(candidate),
        hours=49,
    )
    assert module.load_verified_soak_evidence(str(extended), candidate)["event_counts"]["E1"] == 9

    final_exit = json.loads((package / "final-exit.json").read_text())
    final_exit["open_orders"] = 1
    (package / "final-exit.json").write_text(json.dumps(final_exit))
    with pytest.raises(ValueError, match="did not satisfy"):
        module.load_verified_soak_evidence(str(package), candidate)


def test_soak_package_rejects_wrong_candidate_and_symlink(tmp_path) -> None:
    module = load_admission_cli()
    candidate = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True, cwd=pathlib.Path(__file__).parents[1]
    ).strip()
    package = tmp_path / "soak"
    write_valid_soak_package(
        package,
        candidate=candidate,
        contract=module._candidate_runtime_contract(candidate),
    )
    wrong_candidate = subprocess.check_output(
        ["git", "rev-parse", f"{candidate}^"], text=True, cwd=pathlib.Path(__file__).parents[1]
    ).strip()
    with pytest.raises(ValueError, match="did not satisfy"):
        module.load_verified_soak_evidence(str(package), wrong_candidate)

    link = tmp_path / "soak-link"
    link.symlink_to(package, target_is_directory=True)
    with pytest.raises(ValueError, match="cannot be a symlink"):
        module.load_verified_soak_evidence(str(link), candidate)

    unsafe = tmp_path / "unsafe"
    write_valid_soak_package(
        unsafe,
        candidate=candidate,
        contract=module._candidate_runtime_contract(candidate),
    )
    (unsafe / "manifest.json").unlink()
    (unsafe / "manifest.json").symlink_to(package / "manifest.json")
    with pytest.raises(OSError):
        module.load_verified_soak_evidence(str(unsafe), candidate)


def test_soak_package_binds_cadence_audit_anchor_and_committed_config(tmp_path) -> None:
    module = load_admission_cli()
    repo = pathlib.Path(__file__).parents[1]
    candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, cwd=repo).strip()
    contract = module._candidate_runtime_contract(candidate)

    cadence = tmp_path / "cadence"
    write_valid_soak_package(cadence, candidate=candidate, contract=contract)
    events = [json.loads(line) for line in (cadence / "events.jsonl").read_text().splitlines()]
    first_e1 = next(row["ts_utc"] for row in events if row["event"] == "E1")
    for row in events:
        if row["event"] == "E1":
            row["ts_utc"] = first_e1
    events.sort(key=lambda row: row["ts_utc"])
    (cadence / "events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
    with pytest.raises(ValueError, match="did not satisfy"):
        module.load_verified_soak_evidence(str(cadence), candidate)

    anchor = tmp_path / "anchor"
    write_valid_soak_package(anchor, candidate=candidate, contract=contract)
    (anchor / "audit-start-anchor.json").write_text(
        json.dumps({"seq": 10, "record_sha256": "9" * 64})
    )
    with pytest.raises(ValueError, match="did not satisfy"):
        module.load_verified_soak_evidence(str(anchor), candidate)

    config = tmp_path / "config"
    write_valid_soak_package(config, candidate=candidate, contract=contract)
    manifest = json.loads((config / "manifest.json").read_text())
    manifest["config_sha256"] = "8" * 64
    (config / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="did not satisfy"):
        module.load_verified_soak_evidence(str(config), candidate)


def test_cli_is_non_ordering_and_does_not_print_credentials() -> None:
    script = (pathlib.Path(__file__).parents[1] / "scripts" / "gmaq-live-admission").read_text()
    assert "urllib" not in script
    assert "requests" not in script
    assert "create_order" not in script
    assert "GMAQ_LIVE_KEY" not in script
    assert "GMAQ_LIVE_SECRET" not in script
    assert '"entry_authorized": True' not in script
    assert '"order_submission_enabled": True' not in script
    assert "--config-sha256" not in script
    assert "validated_config_sha256(args.config)" in script
    assert "committed_candidate_sha(ROOT)" in script
    assert "load_committed_strategy_result(args.strategy_result, candidate_sha)" in script
    assert '"merge-base", "--is-ancestor", implementation_sha, candidate_sha' in script
    assert 'minimum_stage="curated"' in script
    assert 'evaluate.add_argument("--soak-evidence", required=True' in script
    assert "load_verified_soak_evidence(args.soak_evidence, candidate_sha)" in script
    assert "--max-age-seconds" not in script


def test_dry_run_authorization_cannot_satisfy_live_candidate() -> None:
    operations = readiness()
    operations["authorization_scope"] = "DEMO_DRY_RUN_ENTRY"
    failed_strategy = strategy_result()
    failed_strategy["verdict"] = "NOT_PROVEN_ALPHA"
    result = evaluate(readiness=operations, strategy_result=failed_strategy)
    assert result["verdict"] == "BLOCKED"
    assert "strategy_verdict_invalid" in result["blockers"]


def test_strategy_pass_requires_verified_curated_v1_registry_binding() -> None:
    unbound = strategy_result()
    unbound.pop("dataset_binding")
    result = evaluate(strategy_result=unbound)
    assert "strategy_dataset_binding_invalid" in result["blockers"]

    mismatched = verified_dataset()
    mismatched["manifest_sha256"] = "9" * 64
    result = evaluate(verified_dataset=mismatched)
    assert "strategy_dataset_registry_mismatch" in result["blockers"]


def test_current_rejected_v1_result_cannot_satisfy_admission() -> None:
    path = (
        pathlib.Path(__file__).parents[1]
        / "research/backtests/study-2026-08-20-btceth-spot-perp-carry/results.json"
    )
    rejected = json.loads(path.read_text())
    result = evaluate(strategy_result=rejected)
    assert "strategy_verdict_invalid" in result["blockers"]


def test_proposed_live_config_is_non_secret_and_minimal() -> None:
    config = {
        "dry_run": False,
        "trading_mode": "futures",
        "margin_mode": "isolated",
        "stake_currency": "USDT",
        "max_open_trades": 1,
        "exchange": {
            "name": "binance",
            "pair_whitelist": ["ETH/USDT:USDT"],
        },
    }
    assert validate_live_config(config) == []
    config["exchange"]["secret"] = "must-not-be-in-config"
    assert "config_exchange_unknown_fields" in validate_live_config(config)
    assert "config_contains_secret_material" in validate_live_config(config)
    config["exchange"].pop("secret")
    config["dry_run"] = True
    assert "config_dry_run_invalid" in validate_live_config(config)
    config["dry_run"] = False
    config["exchange"]["password"] = "must-not-be-stored"
    assert "config_contains_secret_material" in validate_live_config(config)
    assert "config_exchange_unknown_fields" in validate_live_config(config)
    config["exchange"].pop("password")
    config["opaque"] = "secret-under-an-arbitrary-name"
    assert "config_unknown_fields" in validate_live_config(config)
    config.pop("opaque")
    config["exchange"]["key"] = False
    assert "config_exchange_unknown_fields" in validate_live_config(config)
    config["exchange"].pop("key")
    config["max_open_trades"] = True
    assert "config_max_open_trades_invalid" in validate_live_config(config)


def test_numeric_permission_values_cannot_impersonate_booleans() -> None:
    account = account_evidence()
    account["facts"]["canWithdraw"] = 0
    account["facts"]["apiRestrictions"]["enableFutures"] = 1
    operations = readiness()
    operations["dedicated_account"] = 1
    result = evaluate(account_evidence=account, readiness=operations)
    assert "account_canWithdraw_invalid" in result["blockers"]
    assert "account_enableFutures_invalid" in result["blockers"]
    assert "readiness_dedicated_account_invalid" in result["blockers"]


def test_oversized_evidence_age_cannot_relax_freshness() -> None:
    result = evaluate(now_epoch=NOW + 3600, max_evidence_age_seconds=86_400)
    assert "evidence_age_limit_invalid" in result["blockers"]
    assert "account_evidence_stale_or_invalid" in result["blockers"]


def test_admission_cli_bounds_evidence_bytes_and_rows(tmp_path, monkeypatch) -> None:
    module = load_admission_cli()
    oversized = tmp_path / "oversized.json"
    oversized.write_text('{"value":"too-large"}')
    monkeypatch.setattr(module, "MAX_EVIDENCE_FILE_BYTES", 8)
    with pytest.raises(ValueError, match="evidence size limit"):
        module.load_object(str(oversized))

    rows = tmp_path / "rows.json"
    rows.write_text('[{"id":1},{"id":2}]')
    monkeypatch.setattr(module, "MAX_EVIDENCE_FILE_BYTES", 1024)
    monkeypatch.setattr(module, "MAX_EVIDENCE_ROWS", 1)
    with pytest.raises(ValueError, match="evidence row limit"):
        module.load_list(str(rows))


def test_strategy_implementation_must_be_candidate_ancestor(tmp_path, monkeypatch) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "GMAQ Test"], cwd=tmp_path, check=True)
    marker = tmp_path / "marker"
    marker.write_text("root\n")
    subprocess.run(["git", "add", "marker"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "root"], cwd=tmp_path, check=True)
    root_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()

    subprocess.run(["git", "switch", "-qc", "candidate"], cwd=tmp_path, check=True)
    marker.write_text("candidate\n")
    subprocess.run(["git", "commit", "-qam", "candidate"], cwd=tmp_path, check=True)
    candidate_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()

    subprocess.run(["git", "switch", "-qc", "result", root_sha], cwd=tmp_path, check=True)
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("branch\n")
    subprocess.run(["git", "add", "unrelated"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "unrelated"], cwd=tmp_path, check=True)
    unrelated_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()

    result_path = tmp_path / "research" / "backtests" / "study-test" / "results.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(json.dumps({"implementation_candidate_sha": root_sha}))
    subprocess.run(["git", "add", str(result_path.relative_to(tmp_path))], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "result"], cwd=tmp_path, check=True)

    module = load_admission_cli()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    loaded = module.load_committed_strategy_result(str(result_path), candidate_sha)
    assert loaded["implementation_candidate_sha"] == root_sha

    result_path.write_text(json.dumps({"implementation_candidate_sha": unrelated_sha}))
    subprocess.run(["git", "commit", "-qam", "unrelated result"], cwd=tmp_path, check=True)
    with pytest.raises(ValueError, match="not an ancestor"):
        module.load_committed_strategy_result(str(result_path), candidate_sha)


def test_candidate_sha_rejects_hidden_index_flags(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "GMAQ Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("committed\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    subprocess.run(["git", "update-index", "--assume-unchanged", "tracked.txt"], cwd=tmp_path, check=True)
    tracked.write_text("hidden change\n")
    with pytest.raises(RuntimeError, match="hidden-index flags"):
        committed_candidate_sha(tmp_path)
