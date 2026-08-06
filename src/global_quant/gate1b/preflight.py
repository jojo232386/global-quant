from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


REQUIRED_INSTRUMENTS = frozenset({"BTCUSDT", "ETHUSDT"})
MAX_SERVER_TIME_SKEW_MS = 5_000


class PreflightError(RuntimeError):
    """Raised when the Demo account cannot safely enter the frozen matrix."""


@dataclass(frozen=True)
class AccountPreflight:
    can_trade: bool
    dual_side_position: bool
    wallet_balance: Decimal
    nonzero_positions: tuple[tuple[str, Decimal], ...]
    open_regular_order_ids: tuple[str, ...]
    open_algo_order_ids: tuple[str, ...]
    server_time_skew_ms: int
    trading_instruments: frozenset[str]


@dataclass(frozen=True)
class PreflightResult:
    status: str
    reason_codes: tuple[str, ...]
    initial_wallet: Decimal
    automated_cleanup_allowed: bool = False


def evaluate_account_preflight(snapshot: AccountPreflight) -> PreflightResult:
    if snapshot.dual_side_position:
        raise PreflightError("HEDGE_MODE_FORBIDDEN")
    if (
        snapshot.nonzero_positions
        or snapshot.open_regular_order_ids
        or snapshot.open_algo_order_ids
    ):
        raise PreflightError("UNCLEAN_DEMO_ACCOUNT")
    if not snapshot.can_trade:
        raise PreflightError("DEMO_TRADING_PERMISSION_MISSING")
    if abs(snapshot.server_time_skew_ms) > MAX_SERVER_TIME_SKEW_MS:
        raise PreflightError("SERVER_TIME_SKEW")
    wallet = Decimal(snapshot.wallet_balance)
    if wallet <= 0:
        raise PreflightError("DEMO_BALANCE_UNAVAILABLE")
    if not REQUIRED_INSTRUMENTS.issubset(snapshot.trading_instruments):
        raise PreflightError("INSTRUMENT_UNAVAILABLE")
    return PreflightResult(
        status="PASS",
        reason_codes=(),
        initial_wallet=wallet,
        automated_cleanup_allowed=False,
    )

