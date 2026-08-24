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
        "coverage_end_exclusive_utc": "2024-01-01T00:00:00Z",
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
    assert universe_2023 == universe_2022
    assert {"BTCUSDT", "ETHUSDT", "ETCUSDT", "LTCUSDT", "XRPUSDT"} <= set(
        universe_2023
    )


def test_terminal_boundaries_are_effective_time_not_last_bar_inference() -> None:
    payload = master.load_master()

    before = master.universe_at(payload, "2021-12-19T01:59:59Z")
    at = master.universe_at(payload, "2021-12-19T02:00:00Z")
    assert "BZRXUSDT" in before
    assert "BZRXUSDT" not in at

    before_yfii = master.universe_at(payload, "2022-04-12T08:59:59Z")
    at_yfii = master.universe_at(payload, "2022-04-12T09:00:00Z")
    assert "YFIIUSDT" in before_yfii
    assert "YFIIUSDT" not in at_yfii


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

    assert payload["input_lineage"]["price_activity_sha256"] == master.sha256_file(
        master.ACTIVITY_PATH
    )
    assert activity["source"]["integrity_verdict"] == "VERIFIED"
    assert activity["source"]["quality_verdict"] == "PASS"
    assert activity["source"]["numeric_vintage_lineage"] == "VINTAGE_UNVERIFIED"
    assert len(activity["records"]) == 80
    assert all(row["internal_gap_count"] == 0 for row in activity["records"])
    assert all(
        row["first_bar_semantics"] == "PROXY_EVIDENCE_NOT_LISTING_TIMESTAMP"
        for row in activity["records"]
    )
    assert payload["availability_contract"]["funding_oi_vintage"] == "VINTAGE_UNVERIFIED"
    assert payload["availability_contract"]["numeric_price_vintage"] == "VINTAGE_UNVERIFIED"
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


@pytest.mark.parametrize(
    "timestamp",
    [
        "2021-01-04T19:51:02.038Z",
        "2024-01-01T00:00:00Z",
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

    with pytest.raises(master.InstrumentMasterError, match="duplicate"):
        master._load_activity()


def test_overlapping_or_multiple_status_intervals_fail_closed() -> None:
    payload = master.load_master()
    payload["records"][0]["status_intervals"].append(
        dict(payload["records"][0]["status_intervals"][0])
    )

    with pytest.raises(master.InstrumentMasterError, match="status interval is malformed"):
        master.universe_at(payload, "2022-06-30T23:59:59Z")
