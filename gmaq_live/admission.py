"""Pure, non-ordering checks for a future Binance USD-M live candidate.

This module never connects to an exchange and never authorizes an entry.  It
validates captured evidence so missing or contradictory facts remain explicit
instead of being inferred from the dry-run runtime.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any


GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,36}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:-]{8,96}$")
TERMINAL_ORDER_STATES = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}
KNOWN_ORDER_STATES = TERMINAL_ORDER_STATES | {"NEW", "PARTIALLY_FILLED"}
SENSITIVE_CONFIG_TOKENS = {
    "credential",
    "credentials",
    "key",
    "password",
    "passphrase",
    "private",
    "secret",
    "token",
}
LIVE_CONFIG_FIELDS = {
    "dry_run",
    "trading_mode",
    "margin_mode",
    "stake_currency",
    "max_open_trades",
    "exchange",
}
LIVE_EXCHANGE_FIELDS = {"name", "pair_whitelist"}
MAX_EVIDENCE_AGE_SECONDS = 15 * 60


def _contains_secret_material(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    for key, child in value.items():
        tokens = set(re.sub(r"[^a-z0-9]+", " ", str(key).lower()).split())
        if tokens & SENSITIVE_CONFIG_TOKENS and child not in (None, "", False, [], {}):
            return True
        if isinstance(child, dict) and _contains_secret_material(child):
            return True
        if isinstance(child, list) and any(_contains_secret_material(item) for item in child):
            return True
    return False


def validate_live_config(config: object) -> list[str]:
    """Validate the non-secret, one-pair shape of a proposed live config."""

    if not isinstance(config, dict):
        return ["config_not_object"]
    errors = []
    exchange = config.get("exchange") if isinstance(config.get("exchange"), dict) else {}
    if set(config) - LIVE_CONFIG_FIELDS:
        errors.append("config_unknown_fields")
    if set(exchange) - LIVE_EXCHANGE_FIELDS:
        errors.append("config_exchange_unknown_fields")
    expected = {
        "dry_run": False,
        "trading_mode": "futures",
        "margin_mode": "isolated",
        "stake_currency": "USDT",
        "max_open_trades": 1,
    }
    for field, value in expected.items():
        if type(config.get(field)) is not type(value) or config.get(field) != value:
            errors.append(f"config_{field}_invalid")
    if exchange.get("name") != "binance":
        errors.append("config_exchange_invalid")
    if _contains_secret_material(config):
        errors.append("config_contains_secret_material")
    if exchange.get("pair_whitelist") != ["ETH/USDT:USDT"]:
        errors.append("config_pair_scope_invalid")
    return errors


def _iso_epoch(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return int(parsed.timestamp())
    except (ValueError, TypeError):
        return None


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _fresh(value: object, now_epoch: int, max_age_seconds: int) -> bool:
    captured = _iso_epoch(value)
    return captured is not None and -60 <= now_epoch - captured <= max_age_seconds


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _event_epoch_ms(event: dict) -> int | None:
    value = event.get("E")
    return value if isinstance(value, int) and value >= 0 else None


def _identity_errors(evidence: dict, candidate_sha: str, config_sha256: str) -> list[str]:
    errors = []
    if evidence.get("candidate_sha") != candidate_sha:
        errors.append("candidate_sha_mismatch")
    if evidence.get("config_sha256") != config_sha256:
        errors.append("config_sha256_mismatch")
    return errors


def reconcile_binance_usdm_truth(
    *,
    intent: dict,
    rest_orders: list[dict],
    user_stream_events: list[dict],
    rest_positions: list[dict],
    stream_state: dict,
    candidate_sha: str,
    config_sha256: str,
    captured_at_utc: str,
) -> dict:
    """Cross-check one canary order across intent, REST and user stream.

    The function accepts already captured payloads.  Capture transport and
    listen-key lifecycle remain separate, credential-bound work.
    """

    errors: list[str] = []
    for label, value in (
        ("intent", intent),
        ("stream_state", stream_state),
    ):
        if not isinstance(value, dict):
            return {
                "schema_version": 1,
                "scope": "broker_truth_evidence_only_no_order_authority",
                "captured_at_utc": captured_at_utc,
                "candidate_sha": candidate_sha,
                "config_sha256": config_sha256,
                "verdict": "QUARANTINE",
                "errors": [f"{label}_invalid"],
                "does_not_authorize_live_trading": True,
            }
    for label, rows in (
        ("rest_orders", rest_orders),
        ("user_stream_events", user_stream_events),
        ("rest_positions", rest_positions),
    ):
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            errors.append(f"{label}_invalid")
    if errors:
        return {
            "schema_version": 1,
            "scope": "broker_truth_evidence_only_no_order_authority",
            "captured_at_utc": captured_at_utc,
            "candidate_sha": candidate_sha,
            "config_sha256": config_sha256,
            "verdict": "QUARANTINE",
            "errors": errors,
            "does_not_authorize_live_trading": True,
        }
    client_id = intent.get("client_order_id")
    if not isinstance(client_id, str) or not CLIENT_ID_RE.fullmatch(client_id):
        errors.append("client_order_id_invalid")
    elif not client_id.startswith("gmaq-live-"):
        errors.append("client_order_id_namespace_invalid")
    if intent.get("exchange") != "binance_usdm":
        errors.append("intent_exchange_mismatch")
    if intent.get("pre_submit_recorded") is not True:
        errors.append("intent_not_recorded_before_submit")
    if not SHA256_RE.fullmatch(str(intent.get("intent_audit_sha256", ""))):
        errors.append("intent_audit_binding_invalid")
    if not intent.get("adapter_id"):
        errors.append("exchange_bound_adapter_missing")

    if stream_state.get("connected") is not True:
        errors.append("user_stream_not_connected")
    if stream_state.get("gap_detected") is not False:
        errors.append("user_stream_gap_or_unknown")
    if not stream_state.get("session_id"):
        errors.append("user_stream_session_missing")

    matching_rest = [row for row in rest_orders if row.get("clientOrderId") == client_id]
    rest_order_ids = {str(row.get("orderId")) for row in matching_rest if row.get("orderId") is not None}
    if len(matching_rest) != 1 or len(rest_order_ids) != 1:
        errors.append("rest_order_identity_not_unique")

    order_events = []
    account_events = []
    for event in user_stream_events:
        event_type = event.get("e")
        if event_type == "ORDER_TRADE_UPDATE":
            order = event.get("o")
            if isinstance(order, dict) and order.get("c") == client_id:
                event_epoch = _event_epoch_ms(event)
                if event_epoch is None:
                    errors.append("stream_event_time_invalid")
                else:
                    order_events.append((event_epoch, order))
        elif event_type == "ACCOUNT_UPDATE":
            event_epoch = _event_epoch_ms(event)
            if event_epoch is None:
                errors.append("stream_event_time_invalid")
            else:
                account_events.append((event_epoch, event))

    captured_epoch = _iso_epoch(captured_at_utc)
    last_event_epoch_ms = stream_state.get("last_event_epoch_ms")
    observed_event_times = [item[0] for item in order_events + account_events]
    if (
        captured_epoch is None
        or not isinstance(last_event_epoch_ms, int)
        or not observed_event_times
        or last_event_epoch_ms != max(observed_event_times)
        or not -60_000 <= captured_epoch * 1000 - last_event_epoch_ms <= 60_000
    ):
        errors.append("user_stream_freshness_invalid")

    ws_order_ids = {str(order.get("i")) for _, order in order_events if order.get("i") is not None}
    if not order_events or len(ws_order_ids) != 1:
        errors.append("stream_order_identity_not_unique")

    if len(matching_rest) == 1 and order_events:
        rest = matching_rest[0]
        ordered_events = sorted(order_events, key=lambda item: item[0])
        ws = ordered_events[-1][1]
        previous_status = None
        previous_filled = Decimal("-1")
        for _, observed in ordered_events:
            observed_status = observed.get("X")
            observed_filled = _decimal(observed.get("z"))
            observed_quantity = _decimal(observed.get("q"))
            if observed_status not in KNOWN_ORDER_STATES:
                errors.append("order_event_status_unknown")
            if (
                observed_filled is None
                or observed_quantity is None
                or observed_quantity <= 0
                or observed_filled < 0
                or observed_filled < previous_filled
                or observed_filled > observed_quantity
            ):
                errors.append("order_event_filled_progression_invalid")
            else:
                previous_filled = observed_filled
                if observed_status == "NEW" and observed_filled != 0:
                    errors.append("order_event_status_fill_inconsistent")
                elif observed_status == "PARTIALLY_FILLED" and not 0 < observed_filled < observed_quantity:
                    errors.append("order_event_status_fill_inconsistent")
                elif observed_status == "FILLED" and observed_filled != observed_quantity:
                    errors.append("order_event_status_fill_inconsistent")
                elif observed_status == "REJECTED" and observed_filled != 0:
                    errors.append("order_event_status_fill_inconsistent")
                elif (
                    observed_status in {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"}
                    and observed_filled >= observed_quantity
                ):
                    errors.append("order_event_status_fill_inconsistent")
            if previous_status in TERMINAL_ORDER_STATES and observed_status != previous_status:
                errors.append("order_event_status_progression_invalid")
            elif previous_status == "PARTIALLY_FILLED" and observed_status == "NEW":
                errors.append("order_event_status_progression_invalid")
            previous_status = observed_status
            event_comparisons = (
                ("order_id", str(rest.get("orderId")), str(observed.get("i"))),
                ("symbol", intent.get("symbol"), observed.get("s")),
                ("side", intent.get("side"), observed.get("S")),
                ("position_side", intent.get("position_side"), observed.get("ps", "BOTH")),
                ("reduce_only", intent.get("reduce_only"), observed.get("R")),
            )
            for label, expected, actual in event_comparisons:
                if expected != actual:
                    errors.append(f"order_event_{label}_mismatch")
            if _decimal(intent.get("quantity")) != observed_quantity:
                errors.append("order_event_quantity_mismatch")
        intent_comparisons = (
            ("symbol", intent.get("symbol"), rest.get("symbol"), ws.get("s")),
            ("side", intent.get("side"), rest.get("side"), ws.get("S")),
            (
                "position_side",
                intent.get("position_side"),
                rest.get("positionSide", "BOTH"),
                ws.get("ps", "BOTH"),
            ),
            ("reduce_only", intent.get("reduce_only"), rest.get("reduceOnly"), ws.get("R")),
        )
        for label, expected, rest_value, ws_value in intent_comparisons:
            if expected != rest_value or expected != ws_value:
                errors.append(f"intent_{label}_mismatch")
        intent_quantity = _decimal(intent.get("quantity"))
        rest_quantity = _decimal(rest.get("origQty"))
        ws_quantity = _decimal(ws.get("q"))
        if (
            intent_quantity is None
            or rest_quantity is None
            or ws_quantity is None
            or intent_quantity != rest_quantity
            or intent_quantity != ws_quantity
        ):
            errors.append("intent_quantity_mismatch")
        comparisons = (
            ("order_id", str(rest.get("orderId")), str(ws.get("i"))),
            ("symbol", rest.get("symbol"), ws.get("s")),
            ("side", rest.get("side"), ws.get("S")),
            ("status", rest.get("status"), ws.get("X")),
        )
        for label, left, right in comparisons:
            if left != right:
                errors.append(f"order_{label}_mismatch")
        status = rest.get("status")
        if status not in KNOWN_ORDER_STATES:
            errors.append("order_status_unknown")
        for label, left, right in (
            ("quantity", rest.get("origQty"), ws.get("q")),
            ("filled", rest.get("executedQty"), ws.get("z")),
        ):
            left_value, right_value = _decimal(left), _decimal(right)
            if left_value is None or right_value is None or left_value != right_value:
                errors.append(f"order_{label}_mismatch")

    symbol = intent.get("symbol")
    rest_position_rows = [
        row for row in rest_positions if row.get("symbol") == symbol and row.get("positionSide", "BOTH") == "BOTH"
    ]
    if len(rest_position_rows) != 1:
        errors.append("rest_position_identity_not_unique")
    relevant_account_events = []
    for event_epoch, event in account_events:
        account = event.get("a")
        if isinstance(account, dict):
            positions = account.get("P", [])
            if isinstance(positions, list):
                rows = [
                    row
                    for row in positions
                    if isinstance(row, dict)
                    and row.get("s") == symbol
                    and row.get("ps", "BOTH") == "BOTH"
                ]
                if rows:
                    relevant_account_events.append((event_epoch, rows))
    ws_position_rows = max(relevant_account_events, key=lambda item: item[0])[1] if relevant_account_events else []
    if len(ws_position_rows) != 1:
        errors.append("stream_position_identity_not_unique")
    if len(rest_position_rows) == 1 and len(ws_position_rows) == 1:
        rest_amount = _decimal(rest_position_rows[0].get("positionAmt"))
        ws_amount = _decimal(ws_position_rows[0].get("pa"))
        if rest_amount is None or ws_amount is None or rest_amount != ws_amount:
            errors.append("position_amount_mismatch")

    return {
        "schema_version": 1,
        "scope": "broker_truth_evidence_only_no_order_authority",
        "captured_at_utc": captured_at_utc,
        "candidate_sha": candidate_sha,
        "config_sha256": config_sha256,
        "verdict": "MATCH" if not errors else "QUARANTINE",
        "errors": sorted(set(errors)),
        "client_order_id": client_id if isinstance(client_id, str) else None,
        "exchange_order_id": next(iter(rest_order_ids), None) if len(rest_order_ids) == 1 else None,
        "stream_session_id": stream_state.get("session_id"),
        "exchange_bound_adapter": intent.get("adapter_id"),
        "intent_audit_sha256": intent.get("intent_audit_sha256"),
        "capture_sha256": _canonical_sha256(
            {
                "intent": intent,
                "rest_orders": rest_orders,
                "user_stream_events": user_stream_events,
                "rest_positions": rest_positions,
                "stream_state": stream_state,
            }
        ),
        "does_not_authorize_live_trading": True,
    }


def evaluate_live_candidate(
    *,
    account_evidence: dict,
    broker_evidence: dict,
    readiness: dict,
    candidate_sha: str,
    config_sha256: str,
    now_epoch: int,
    max_evidence_age_seconds: int = 15 * 60,
) -> dict:
    """Return candidate eligibility, never live-entry authority."""

    blockers: list[str] = []
    if (
        not isinstance(max_evidence_age_seconds, int)
        or isinstance(max_evidence_age_seconds, bool)
        or not 1 <= max_evidence_age_seconds <= MAX_EVIDENCE_AGE_SECONDS
    ):
        blockers.append("evidence_age_limit_invalid")
        max_evidence_age_seconds = MAX_EVIDENCE_AGE_SECONDS
    if not GIT_SHA_RE.fullmatch(candidate_sha):
        blockers.append("candidate_sha_invalid")
    if not SHA256_RE.fullmatch(config_sha256):
        blockers.append("config_sha256_invalid")
    blockers.extend(f"account_{item}" for item in _identity_errors(account_evidence, candidate_sha, config_sha256))
    if account_evidence.get("schema_version") != 2:
        blockers.append("account_schema_invalid")
    if account_evidence.get("scope") != "authorized read-only session; GET requests only":
        blockers.append("account_scope_invalid")
    if account_evidence.get("verdict") != "PASS_READONLY":
        blockers.append("account_readonly_preflight_not_passed")
    if not _fresh(account_evidence.get("fetched_at_utc"), now_epoch, max_evidence_age_seconds):
        blockers.append("account_evidence_stale_or_invalid")
    facts = account_evidence.get("facts") if isinstance(account_evidence.get("facts"), dict) else {}
    required_facts = {
        "accountType": "usd_m_futures",
        "canTrade": True,
        "canWithdraw": False,
        "dualSidePosition": False,
        "multiAssetsMarginMode": False,
    }
    for field, expected in required_facts.items():
        if type(facts.get(field)) is not type(expected) or facts.get(field) != expected:
            blockers.append(f"account_{field}_invalid")
    restrictions = facts.get("apiRestrictions") if isinstance(facts.get("apiRestrictions"), dict) else {}
    for field, expected in {"enableFutures": True, "enableWithdrawals": False, "ipRestrict": True}.items():
        if type(restrictions.get(field)) is not type(expected) or restrictions.get(field) != expected:
            blockers.append(f"account_{field}_invalid")
    commission = facts.get("commission") if isinstance(facts.get("commission"), dict) else {}
    if commission.get("status") != "VERIFIED_ON_ACCOUNT" or commission.get("taker") is None:
        blockers.append("account_fee_unverified")
    bracket = facts.get("leverageBracketTier1") if isinstance(facts.get("leverageBracketTier1"), dict) else {}
    if bracket.get("maintMarginRatio") is None:
        blockers.append("account_mmr_unverified")

    blockers.extend(f"broker_{item}" for item in _identity_errors(broker_evidence, candidate_sha, config_sha256))
    if broker_evidence.get("schema_version") != 1:
        blockers.append("broker_schema_invalid")
    if broker_evidence.get("verdict") != "MATCH" or broker_evidence.get("errors"):
        blockers.append("broker_truth_not_matched")
    if not broker_evidence.get("exchange_bound_adapter"):
        blockers.append("broker_exchange_bound_adapter_missing")
    if not SHA256_RE.fullmatch(str(broker_evidence.get("intent_audit_sha256", ""))):
        blockers.append("broker_intent_audit_binding_invalid")
    if not SHA256_RE.fullmatch(str(broker_evidence.get("capture_sha256", ""))):
        blockers.append("broker_capture_digest_invalid")
    if not _fresh(broker_evidence.get("captured_at_utc"), now_epoch, max_evidence_age_seconds):
        blockers.append("broker_evidence_stale_or_invalid")

    blockers.extend(f"readiness_{item}" for item in _identity_errors(readiness, candidate_sha, config_sha256))
    if readiness.get("schema_version") != 1:
        blockers.append("readiness_schema_invalid")
    if not _fresh(readiness.get("captured_at_utc"), now_epoch, max_evidence_age_seconds):
        blockers.append("readiness_evidence_stale_or_invalid")
    required_readiness = {
        "dedicated_account": True,
        "sole_operator": True,
        "risk_limits_approved": True,
        "alert_route_verified": True,
        "secret_storage_approved": True,
        "strategy_verdict": "PASS",
        "soak_verdict": "PASS",
    }
    for field, expected in required_readiness.items():
        if type(readiness.get(field)) is not type(expected) or readiness.get(field) != expected:
            blockers.append(f"readiness_{field}_invalid")
    for field in ("strategy_artifact_sha256", "soak_evidence_sha256", "alert_evidence_sha256"):
        if not SHA256_RE.fullmatch(str(readiness.get(field, ""))):
            blockers.append(f"readiness_{field}_invalid")
    for field in ("risk_approval_id", "operator_attestation_id", "secret_storage_approval_id"):
        if not IDENTIFIER_RE.fullmatch(str(readiness.get(field, ""))):
            blockers.append(f"readiness_{field}_invalid")

    # These cannot be proven by caller-authored fixtures. They stay structural
    # blockers until reviewed, credential-bound production adapters exist.
    blockers.extend(
        (
            "implementation_authenticated_capture_adapter_missing",
            "implementation_exchange_bound_submission_adapter_missing",
            "implementation_live_credential_account_binding_missing",
        )
    )

    blockers = sorted(set(blockers))
    return {
        "schema_version": 1,
        "verdict": "CANDIDATE_ELIGIBLE" if not blockers else "BLOCKED",
        "candidate_sha": candidate_sha,
        "config_sha256": config_sha256,
        "blockers": blockers,
        "entry_authorized": False,
        "order_submission_enabled": False,
        "note": "Candidate eligibility is not live authorization and cannot arm or submit orders.",
    }
