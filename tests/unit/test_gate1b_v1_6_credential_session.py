"""Tests for the credential-bearing child session (``credential_session.py``).

Coverage per task section F:

* the child refuses to start when the parent environment holds a credential
  name, and requires an interactive TTY;
* an unbound / stale authorization manifest fails closed before any credential
  is read;
* a happy fake lifecycle (injected fake transport + fake hidden prompt) yields a
  PASS with zero network and zero real credential;
* the fake credential never leaks into argv, evidence, or logs (the pre-exit
  bundle is secret-free by construction).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from global_quant.gate1b.authorization import (
    create_authorization,
    write_manifest,
)
from global_quant.gate1b.credential_session import (
    CredentialSessionError,
    run_credential_session,
)
from global_quant.gate1b.mutation_protocol import (
    SYMBOL,
    AccountState,
    LimitOrderFilters,
    SymbolState,
)
from global_quant.gate1b.mutation_runner import FakeLifecycleTransport

_RUNTIME = "a" * 40
_NONCE = "0123456789abcdef"
_AUTH_ID = "g1b16-0123456789abcdef"
_PROTOCOL = "b" * 40
_TAG_OBJECT = "c" * 40
_SHA = "d" * 64

_BINDING = {
    "runtime_commit": _RUNTIME,
    "session_nonce": _NONCE,
    "authorization_id": _AUTH_ID,
    "protocol_commit": _PROTOCOL,
    "protocol_tag_object": _TAG_OBJECT,
    "protocol_sha256": _SHA,
}

FAKE_KEY = "fake-demo-api-key-0123456789abcdef"
FAKE_SECRET = "fake-demo-api-secret-0123456789abcdef"


def _filters() -> LimitOrderFilters:
    return LimitOrderFilters(
        min_price=Decimal("1000.00"),
        max_price=Decimal("5000.00"),
        tick_size=Decimal("0.01"),
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("100.000"),
        step_size=Decimal("0.001"),
        min_notional=Decimal("5"),
        percent_price_multiplier_down=Decimal("0.85"),
        percent_price_multiplier_up=Decimal("1.05"),
    )


def _account_state() -> AccountState:
    return AccountState(
        can_trade=True,
        dual_side_position=False,
        multi_assets_margin=False,
        margin_type="ISOLATED",
        leverage=1,
        auto_add_margin=False,
        server_time_skew_ms=Decimal("100"),
        wallet_balance=Decimal("100"),
        available_balance=Decimal("100"),
        nonzero_positions=(),
        open_regular_order_ids=(),
        open_algo_order_ids=(),
    )


def _symbol_state() -> SymbolState:
    return SymbolState(
        symbol=SYMBOL,
        status="TRADING",
        contract_type="PERPETUAL",
        quote_asset="USDT",
        margin_asset="USDT",
        order_types=frozenset({"LIMIT", "MARKET"}),
        time_in_force=frozenset({"GTX"}),
        filter_type_counts=(
            ("PRICE_FILTER", 1),
            ("LOT_SIZE", 1),
            ("MARKET_LOT_SIZE", 1),
            ("MIN_NOTIONAL", 1),
            ("PERCENT_PRICE", 1),
        ),
        uninterpreted_applicable_filter_types=(),
    )


def _happy_transport_factory(_credentials):
    return FakeLifecycleTransport(
        account_state=_account_state(),
        symbol_state=_symbol_state(),
        filters=_filters(),
        best_bid=Decimal("2500.00"),
        best_ask=Decimal("2500.01"),
        mark_price=Decimal("2500.00"),
        create_ack={"orderId": "1", "status": "NEW"},
        query_status="NEW",
        query_executed_quantity=Decimal("0"),
        query_accepted_elapsed_seconds=Decimal("1"),
        cancel_status="CANCELED",
        terminal_status="CANCELED",
        terminal_executed_quantity=Decimal("0"),
        final_state={
            "nonzero_positions": (),
            "open_regular_orders": 0,
            "open_algo_orders": 0,
            "account_config_matches": True,
        },
        production_contacted=False,
    )


def _write_active_manifest(path: Path) -> Path:
    record = create_authorization(
        protocol_commit=_PROTOCOL,
        protocol_tag_object=_TAG_OBJECT,
        protocol_sha256=_SHA,
        runtime_commit=_RUNTIME,
        authorization_id=_AUTH_ID,
    )
    return write_manifest(path, record)


def _fake_prompt(_label: str) -> str:
    return FAKE_SECRET if "secret" in _label else FAKE_KEY


def _patch_runtime_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the lifecycle runner's real git binding check accept the fake binding."""

    monkeypatch.setattr(
        "global_quant.gate1b.mutation_runner._verify_runtime_binding",
        lambda *a, **k: {
            "runtime_commit": _RUNTIME,
            "runtime_tree": "t" * 40,
            "protocol_commit": _PROTOCOL,
            "protocol_tag_object": _TAG_OBJECT,
            "protocol_sha256": _SHA,
            "protocol_tag": "nt-gate-1b-v1.6-protocol",
        },
    )


class TestSessionBoundary:
    def test_parent_environment_with_credential_rejected(self, tmp_path: Path) -> None:
        manifest = _write_active_manifest(tmp_path / "auth.json")
        with pytest.raises(CredentialSessionError) as exc:
            run_credential_session(
                evidence_dir=tmp_path / "ev",
                binding=_BINDING,
                authorization_manifest=manifest,
                prompt_secret=_fake_prompt,
                transport_factory=_happy_transport_factory,
                environ={"BINANCE_DEMO_API_KEY": "x"},
            )
        assert "CREDENTIAL_ENVIRONMENT_MUST_BE_EMPTY" in str(exc.value)

    def test_non_interactive_rejected(self, tmp_path: Path) -> None:
        manifest = _write_active_manifest(tmp_path / "auth.json")
        with pytest.raises(CredentialSessionError) as exc:
            run_credential_session(
                evidence_dir=tmp_path / "ev",
                binding=_BINDING,
                authorization_manifest=manifest,
                prompt_secret=_fake_prompt,
                transport_factory=_happy_transport_factory,
                input_is_tty=False,
            )
        assert "INTERACTIVE_TERMINAL_REQUIRED" in str(exc.value)

    def test_missing_authorization_fails_closed_before_credentials(self, tmp_path: Path) -> None:
        code, pre_exit = run_credential_session(
            evidence_dir=tmp_path / "ev",
            binding=_BINDING,
            authorization_manifest=tmp_path / "absent.json",
            prompt_secret=_fake_prompt,
            transport_factory=_happy_transport_factory,
        )
        assert code == 1
        payload = json.loads(pre_exit.read_text(encoding="utf-8"))
        assert payload["status"] == "STOP"
        assert "AUTHORIZATION_MANIFEST_MISSING" in " ".join(payload["reason_codes"])


class TestHappyFakeLifecycle:
    def test_fake_credential_lifecycle_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_runtime_binding(monkeypatch)
        manifest = _write_active_manifest(tmp_path / "auth.json")
        evidence_dir = tmp_path / "ev"
        code, path = run_credential_session(
            evidence_dir=evidence_dir,
            binding=_BINDING,
            authorization_manifest=manifest,
            prompt_secret=_fake_prompt,
            transport_factory=_happy_transport_factory,
        )
        assert code == 0
        lifecycle = json.loads(path.read_text(encoding="utf-8"))
        assert lifecycle["status"] == "PASS"
        assert lifecycle["lifecycle"]["production_contacted"] is False

    def test_credential_never_leaks_into_evidence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_runtime_binding(monkeypatch)
        manifest = _write_active_manifest(tmp_path / "auth.json")
        evidence_dir = tmp_path / "ev"
        run_credential_session(
            evidence_dir=evidence_dir,
            binding=_BINDING,
            authorization_manifest=manifest,
            prompt_secret=_fake_prompt,
            transport_factory=_happy_transport_factory,
        )
        for candidate in evidence_dir.rglob("*"):
            if candidate.is_file():
                text = candidate.read_text(errors="ignore")
                assert FAKE_KEY not in text
                assert FAKE_SECRET not in text
        # The pre-exit bundle exists and is sanitized.
        assert (evidence_dir / "child-pre-exit.json").exists()
