from __future__ import annotations

import datetime as dt
import pathlib
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research" / "data"))
import expl_017_lifecycle_v1 as lifecycle  # noqa: E402


def day(year: int, month: int, value: int) -> int:
    return int(dt.datetime(year, month, value, tzinfo=dt.UTC).timestamp() * 1000)


def test_sidecar_is_exception_only_and_bound_to_exact_price_v1_identity():
    events, sidecar_sha = lifecycle.load_sidecar()
    assert len(events) == 12
    assert len(sidecar_sha) == 64
    assert sum(
        event.classification == lifecycle.TERMINATED_CONFIRMED
        for event in events.values()
    ) == 11
    assert events["AKROUSDT"].classification == lifecycle.TERMINATED_UNCONFIRMED
    assert lifecycle.EVENT_TYPE == "USD_M_PERPETUAL_TERMINATION"
    assert len(lifecycle.verify_composite_identity(sidecar_sha)) == 64


def test_confirmed_event_becomes_terminal_only_after_completed_bar_close():
    event = lifecycle.LifecycleEvent(
        "OLD", lifecycle.TERMINATED_CONFIRMED, day(2022, 5, 13) + 4 * 60 * 60 * 1000,
        day(2022, 5, 13) + 2 * 60 * 60 * 1000, day(2022, 5, 13)
    )
    resolver = lifecycle.LifecycleResolver({"OLD": event})
    assert resolver.as_of("OLD", day(2022, 5, 12)).active
    terminal = resolver.as_of("OLD", day(2022, 5, 13))
    assert terminal.active is False
    assert terminal.terminal_timestamp == day(2022, 5, 13)


def test_unconfirmed_event_is_not_a_future_delivery_filter_and_fails_closed_on_missing():
    event = lifecycle.LifecycleEvent(
        "AKROUSDT", lifecycle.TERMINATED_UNCONFIRMED, day(2022, 5, 26),
        day(2022, 5, 18), day(2022, 5, 27)
    )
    resolver = lifecycle.LifecycleResolver({"AKROUSDT": event})
    assert resolver.as_of("AKROUSDT", day(2022, 5, 28)).active
    with pytest.raises(lifecycle.LifecycleDataUnavailable, match="DATA_UNAVAILABLE"):
        resolver.require_missing_bar_semantics("AKROUSDT", day(2022, 5, 28))


def test_malformed_or_post_effective_primary_evidence_fails_closed(tmp_path):
    target = tmp_path / "sidecar.json"
    target.write_text(lifecycle.SIDECAR_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    payload = target.read_text(encoding="utf-8")
    target.write_text(payload.replace("2022-04-04T05:21:50.096000Z", "2022-04-12T05:21:50.096000Z", 1), encoding="utf-8")
    with pytest.raises(lifecycle.LifecycleError, match="published after"):
        lifecycle.load_sidecar(target)


def test_every_exception_needs_a_typed_event_and_akro_keeps_auditable_conflict_evidence(tmp_path):
    payload = lifecycle.SIDECAR_PATH.read_text(encoding="utf-8")
    assert '"event_type":"USD_M_PERPETUAL_TERMINATION"' in payload
    assert '"symbol":"AKROUSDT"' in payload
    assert 'AKROUSDT/AKROUSDT-aggTrades-2022-05-27.zip' in payload
    target = tmp_path / "sidecar.json"
    target.write_text(payload.replace('"event_type":"USD_M_PERPETUAL_TERMINATION",', "", 1), encoding="utf-8")
    with pytest.raises(lifecycle.LifecycleError, match="event type"):
        lifecycle.load_sidecar(target)


def test_real_price_v1_structure_scope_is_read_only_when_local_snapshot_is_available():
    sys.path.insert(0, str(ROOT / "research" / "exploration"))
    import price_alpha_v1 as price  # noqa: E402

    data_root = ROOT.parent / "gmaq-data"
    if not (data_root / "registry.sqlite").is_file():
        pytest.skip("local immutable Price V1 snapshot is not installed")
    dataset = price.load_dataset(data_root)
    early = {symbol for symbol, last in dataset.last_timestamp.items() if last < day(2023, 12, 31)}
    events, _ = lifecycle.load_sidecar()
    assert len(dataset.bars) == 208
    assert len(early) == 12
    assert early == set(events)
    assert all(
        list(series) == list(range(min(series), max(series) + lifecycle.DAY_MS, lifecycle.DAY_MS))
        for series in dataset.bars.values()
    )
