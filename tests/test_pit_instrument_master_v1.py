"""Contracts for the bounded GMAQ PIT Instrument Master V1."""

from __future__ import annotations

import json
import pathlib

import pytest

from research.data import pit_instrument_master_v1 as master


def test_committed_master_matches_deterministic_rebuild() -> None:
    payload = master.load_master()

    assert payload["result_class"] == "PARTIAL_PIT_COHORT_CANDIDATE"
    assert payload["cohort"] == {
        "cohort_id": master.COHORT_ID,
        "definition": (
            "All 80 TRADING USDT-quoted PERPETUAL instruments in the archived official "
            "Binance USD-M exchangeInfo response at the frozen capture"
        ),
        "selection_uses_current_survivors": False,
        "selection_uses_price_outcomes": False,
        "symbol_count": 80,
        "formally_tier2_admitted_symbol_count": 0,
        "tier2_data_foundation_ready": False,
        "coverage_start_inclusive_utc": "2021-01-04T19:51:02.039Z",
        "coverage_end_exclusive_utc": "2023-11-14T00:00:00Z",
        "coverage_end_reason": (
            "TOMOUSDT_2023_11_14_DAILY_BAR_OPEN_CANNOT_PROVE_INTRADAY_STATUS; "
            "POST_BAR_ZERO_VOLUME_TAIL_NOT_TERMINAL_EVIDENCE"
        ),
        "scope_limit": "FIXED_COHORT_NOT_COMPLETE_DYNAMIC_MARKET_UNIVERSE",
    }
    assert len(payload["records"]) == 80


def test_snapshot_cohort_is_not_selected_from_current_survivors_or_price_outcomes() -> None:
    payload = master.load_master()
    records = {item["symbol"]: item for item in payload["records"]}

    assert {"BTCUSDT", "ETHUSDT", "BCHUSDT", "XRPUSDT", "EOSUSDT"} <= set(records)
    assert {item["contract_type"] for item in records.values()} == {"PERPETUAL"}
    assert {item["quote_asset"] for item in records.values()} == {"USDT"}
    assert {item["conflict_status"] for item in records.values()} == {"NONE"}
    assert all(item["exchange_reported_onboard_trusted"] is False for item in records.values())
    assert all(
        item["listing_timestamp_semantics"]
        == "CONSERVATIVE_CONFIRMED_ACTIVE_FROM; NOT_TRUE_ONBOARD_TIMESTAMP"
        for item in records.values()
    )
    assert "APTUSDT" not in records


def test_universe_at_preserves_past_delisted_members_and_excludes_future_terminals() -> None:
    payload = master.load_master()
    universe_2021 = master.universe_at(payload, "2021-06-30T23:59:59Z")
    universe_2022 = master.universe_at(payload, "2022-06-30T23:59:59Z")
    universe_2023 = master.universe_at(payload, "2023-06-30T23:59:59Z")

    assert len(universe_2021) == 80
    assert {"BZRXUSDT", "YFIIUSDT"} <= set(universe_2021)
    assert len(universe_2022) == 78
    assert "BZRXUSDT" not in universe_2022
    assert "YFIIUSDT" not in universe_2022
    assert len(universe_2023) == 75
    assert set(universe_2023) < set(universe_2022)
    assert {"CVCUSDT", "HNTUSDT", "SRMUSDT"}.isdisjoint(universe_2023)
    assert {"BTCUSDT", "ETHUSDT", "ETCUSDT", "LTCUSDT", "XRPUSDT"} <= set(
        universe_2023
    )


@pytest.mark.parametrize(
    ("symbol", "before_timestamp", "at_timestamp"),
    [
        ("BZRXUSDT", "2021-12-19T01:59:59Z", "2021-12-19T02:00:00Z"),
        ("YFIIUSDT", "2022-04-12T08:59:59Z", "2022-04-12T09:00:00Z"),
        ("SRMUSDT", "2022-11-15T04:29:59Z", "2022-11-15T04:30:00Z"),
        ("CVCUSDT", "2022-11-29T08:59:59Z", "2022-11-29T09:00:00Z"),
        ("HNTUSDT", "2023-03-20T08:59:59Z", "2023-03-20T09:00:00Z"),
    ],
)
def test_terminal_boundaries_are_effective_time_not_last_bar_inference(
    symbol: str, before_timestamp: str, at_timestamp: str
) -> None:
    payload = master.load_master()

    assert symbol in master.universe_at(payload, before_timestamp)
    assert symbol not in master.universe_at(payload, at_timestamp)


def test_akro_conflict_is_quarantined_without_blocking_the_cohort() -> None:
    payload = master.load_master()

    assert payload["quarantine"] == [
        {
            **payload["quarantine"][0],
            "symbol": "AKROUSDT",
            "cohort_member": False,
            "conflict_status": "QUARANTINED",
            "universe_eligible": False,
        }
    ]
    assert "continued" in payload["quarantine"][0]["reason"]
    assert all(record["symbol"] != "AKROUSDT" for record in payload["records"])


def test_price_activity_is_hash_bound_gap_free_and_vintage_fail_closed() -> None:
    payload = master.load_master()
    activity = json.loads(master.ACTIVITY_PATH.read_text(encoding="utf-8"))

    assert payload["input_lineage"]["price_activity_sha256"] == master.ACTIVITY_SHA256
    assert master.sha256_file(master.ACTIVITY_PATH) == master.ACTIVITY_SHA256
    assert activity["source"]["integrity_verdict"] == "VERIFIED"
    assert activity["source"]["quality_verdict"] == "PASS"
    assert activity["source"]["numeric_vintage_lineage"] == "VINTAGE_UNVERIFIED"
    assert len(activity["records"]) == 80
    assert all(row["internal_gap_count"] == 0 for row in activity["records"])
    assert all(
        row["first_bar_semantics"] == "PROXY_EVIDENCE_NOT_LISTING_TIMESTAMP"
        for row in activity["records"]
    )
    rows = {row["symbol"]: row for row in activity["records"]}
    assert {
        symbol: row["trailing_zero_quote_volume_day_count"]
        for symbol, row in rows.items()
        if row["trailing_zero_quote_volume_day_count"]
    } == master.EXPECTED_ZERO_TAILS
    assert all(
        "ZERO_VOLUME_ROWS_ARE_NOT_ACTIVITY" in row["activity_semantics"]
        for row in activity["records"]
    )
    assert rows["TOMOUSDT"]["last_positive_quote_volume_bar_open_utc"] == (
        "2023-11-14T00:00:00.000Z"
    )
    assert payload["availability_contract"]["funding_oi_vintage"] == "VINTAGE_UNVERIFIED"
    assert payload["availability_contract"]["numeric_price_vintage"] == "VINTAGE_UNVERIFIED"
    assert payload["availability_contract"]["zero_volume_tail_policy"] == (
        "NOT_TRADING_ACTIVITY_OR_TERMINAL_EVIDENCE; "
        "FAIL_CLOSED_AT_FIRST_UNRESOLVED_TAIL"
    )
    assert (
        payload["availability_contract"]["publication_timestamp"]
        == "NOT_APPLICABLE_TO_REST_STATUS_RESPONSE"
    )
    assert payload["availability_contract"]["historical_status_as_of_timestamp"] == (
        "2021-01-04T19:51:02.039Z"
    )
    assert all(
        record["source"]["source_publication_timestamp_utc"] is None
        and record["source"]["source_publication_semantics"]
        == "NOT_APPLICABLE_TO_REST_STATUS_RESPONSE"
        for record in payload["records"]
    )


def test_supplemental_terminals_are_hash_bound_and_tomo_stops_global_coverage() -> None:
    payload = master.load_master()
    records = {record["symbol"]: record for record in payload["records"]}
    evidence = json.loads(
        master.SUPPLEMENTAL_LIFECYCLE_PATH.read_text(encoding="utf-8")
    )

    assert payload["input_lineage"]["supplemental_terminal_evidence_sha256"] == (
        master.sha256_file(master.SUPPLEMENTAL_LIFECYCLE_PATH)
    )
    assert {row["symbol"] for row in evidence["events"]} == master.SUPPLEMENTAL_TERMINALS
    assert {
        row["symbol"]: (
            row["trade_archive"]["first_trade_at_utc"],
            row["trade_archive"]["last_trade_at_utc"],
            row["trade_archive"]["row_count"],
        )
        for row in evidence["events"]
    } == master.EXPECTED_SUPPLEMENTAL_ARCHIVE_SUMMARIES
    assert evidence["coverage_stop"]["symbol"] == "TOMOUSDT"
    assert evidence["coverage_stop"]["classification"] == (
        "LIFECYCLE_UNRESOLVED_NO_TERMINAL_INFERENCE"
    )
    assert all(
        records[symbol]["terminal_evidence"]["evidence_type"]
        == "TIER_A_OFFICIAL_ANNOUNCEMENT_PLUS_EVENT_ARCHIVE"
        and records[symbol]["terminal_evidence"]["cms_api_url"].startswith(
            "https://www.binance.com/bapi/"
        )
        for symbol in master.SUPPLEMENTAL_TERMINALS
    )
    assert records["TOMOUSDT"]["terminal_evidence"] is None
    assert "TOMOUSDT" in master.universe_at(payload, "2023-11-13T23:59:59Z")
    with pytest.raises(master.InstrumentMasterError, match="outside the proven cohort window"):
        master.universe_at(payload, "2023-11-14T00:00:00Z")


@pytest.mark.parametrize(
    "timestamp",
    [
        "2021-01-04T19:51:02.038Z",
        "2023-11-14T00:00:00Z",
        "2023-11-15T00:00:00Z",
        "2023-12-31T00:00:00Z",
        "2025-01-01T00:00:00Z",
    ],
)
def test_universe_queries_outside_proven_window_fail_closed(timestamp: str) -> None:
    with pytest.raises(master.InstrumentMasterError, match="outside the proven cohort window"):
        master.universe_at(master.load_master(), timestamp)


def test_raw_source_symlink_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    link = tmp_path / "snapshot.json"
    link.symlink_to(master.SNAPSHOT_PATH)
    monkeypatch.setattr(master, "SNAPSHOT_PATH", link)

    with pytest.raises(master.InstrumentMasterError, match="not a regular file"):
        master.build_master()


def test_raw_source_parent_symlink_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    linked_parent = trusted / "raw"
    linked_parent.symlink_to(master.RAW_DIR, target_is_directory=True)
    monkeypatch.setattr(master, "ROOT", trusted)
    monkeypatch.setattr(
        master,
        "SNAPSHOT_PATH",
        linked_parent / master.SNAPSHOT_PATH.name,
    )

    with pytest.raises(master.InstrumentMasterError, match="contains a symlink"):
        master.build_master()


def test_cdx_digest_must_bind_saved_response_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    cdx = json.loads(master.CDX_PATH.read_text(encoding="utf-8"))
    row = next(item for item in cdx[1:] if item[0] == master.WAYBACK_TIMESTAMP)
    row[2] = "A" * len(row[2])
    path = tmp_path / "cdx.json"
    path.write_text(json.dumps(cdx), encoding="utf-8")
    monkeypatch.setattr(master, "CDX_PATH", path)
    monkeypatch.setattr(master, "CDX_SHA256", master.sha256_file(path))

    with pytest.raises(master.InstrumentMasterError, match="does not bind"):
        master.build_master()


def test_duplicate_activity_identity_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    activity = json.loads(master.ACTIVITY_PATH.read_text(encoding="utf-8"))
    activity["records"][-1] = dict(activity["records"][0])
    path = tmp_path / "activity.json"
    path.write_text(json.dumps(activity), encoding="utf-8")
    monkeypatch.setattr(master, "ACTIVITY_PATH", path)
    monkeypatch.setattr(master, "ACTIVITY_SHA256", master.sha256_file(path))

    with pytest.raises(master.InstrumentMasterError, match="duplicate"):
        master._load_activity()


def test_activity_artifact_tampering_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    activity = json.loads(master.ACTIVITY_PATH.read_text(encoding="utf-8"))
    activity["records"][0]["source_file_sha256"] = "0" * 64
    path = tmp_path / "activity.json"
    path.write_text(json.dumps(activity), encoding="utf-8")
    monkeypatch.setattr(master, "ACTIVITY_PATH", path)

    with pytest.raises(master.InstrumentMasterError, match="artifact SHA-256 mismatch"):
        master._load_activity()


def test_canonical_lifecycle_tampering_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    payload = master.LIFECYCLE_PATH.read_text(encoding="utf-8")
    path = tmp_path / "lifecycle.json"
    path.write_text(
        payload.replace("2021-12-19T02:00:00Z", "2021-12-19T02:01:00Z", 1),
        encoding="utf-8",
    )
    monkeypatch.setattr(master, "LIFECYCLE_PATH", path)

    with pytest.raises(master.InstrumentMasterError, match="Lifecycle V1 SHA-256 mismatch"):
        master.build_master()


def test_supplemental_terminal_evidence_tampering_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    evidence = json.loads(
        master.SUPPLEMENTAL_LIFECYCLE_PATH.read_text(encoding="utf-8")
    )
    evidence["events"][0]["terminal_effective_at_utc"] = "2022-11-30T09:00:00Z"
    path = tmp_path / "supplemental.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    monkeypatch.setattr(master, "SUPPLEMENTAL_LIFECYCLE_PATH", path)

    with pytest.raises(master.InstrumentMasterError, match="SHA-256 mismatch"):
        master.build_master()


def test_noncanonical_master_bytes_fail_closed(tmp_path: pathlib.Path) -> None:
    payload = master.load_master()
    path = tmp_path / "minified-master.json"
    path.write_text(json.dumps(payload, sort_keys=False), encoding="utf-8")

    with pytest.raises(master.InstrumentMasterError, match="deterministic rebuild"):
        master.load_master(path)


def test_overlapping_or_multiple_status_intervals_fail_closed() -> None:
    payload = master.load_master()
    payload["records"][0]["status_intervals"].append(
        dict(payload["records"][0]["status_intervals"][0])
    )

    with pytest.raises(master.InstrumentMasterError, match="status interval is malformed"):
        master.universe_at(payload, "2022-06-30T23:59:59Z")
