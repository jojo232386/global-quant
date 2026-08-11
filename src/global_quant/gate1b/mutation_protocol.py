"""Pure offline contract helpers for the NT-GATE-1B v1.6 mutation draft."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from enum import StrEnum

PROTOCOL_VERSION = "1.7"
PROTOCOL_STATUS = "FROZEN_OPTION_A_APPROVED"
SYMBOL = "ETHUSDT"
MAX_NOTIONAL_USDT = Decimal("25")
PRICE_DISCOUNT_BPS = 100
NORMAL_MUTATION_REQUESTS = 2
MAX_HARD_MUTATION_REQUESTS = 4
MAX_READ_RETRIES = 1
NORMAL_PRE_CREATE_HTTP_REQUESTS = 11
NORMAL_POST_CREATE_HTTP_REQUESTS = 9
AMBIGUOUS_CANCEL_EXTRA_HTTP_REQUESTS = 2
EMERGENCY_CONTAINMENT_EXTRA_HTTP_REQUESTS = 6
MAX_HTTP_REQUESTS = 31
REQUEST_TIMEOUT_SECONDS = 5
TOTAL_RUNTIME_SECONDS = 180
CREATE_DEADLINE_SECONDS = 60
MAX_ACCEPTED_TO_CANCEL_SECONDS = 3
POST_CREATE_HTTP_RESERVE = (
    NORMAL_POST_CREATE_HTTP_REQUESTS
    + AMBIGUOUS_CANCEL_EXTRA_HTTP_REQUESTS
    + EMERGENCY_CONTAINMENT_EXTRA_HTTP_REQUESTS
    + MAX_READ_RETRIES
)
MAX_POST_CREATE_READ_REQUESTS = POST_CREATE_HTTP_RESERVE - 3
NORMAL_TOTAL_HTTP_REQUESTS = NORMAL_PRE_CREATE_HTTP_REQUESTS + 1 + NORMAL_POST_CREATE_HTTP_REQUESTS
RECEIVE_WINDOW_MS = 5_000
DEMO_HTTP_ORIGIN = "https://demo-fapi.binance.com"

_ALLOWED_READ_PATHS = frozenset(
    {
        "/fapi/v1/exchangeInfo",
        "/fapi/v1/openAlgoOrders",
        "/fapi/v1/openOrders",
        "/fapi/v1/order",
        "/fapi/v1/premiumIndex",
        "/fapi/v1/positionSide/dual",
        "/fapi/v1/symbolConfig",
        "/fapi/v1/ticker/bookTicker",
        "/fapi/v1/time",
        "/fapi/v1/userTrades",
        "/fapi/v2/account",
    }
)
_REQUIRED_FILTER_TYPES = frozenset(
    {"PRICE_FILTER", "LOT_SIZE", "MARKET_LOT_SIZE", "MIN_NOTIONAL", "PERCENT_PRICE"}
)
_OWNED_POSITION_READ_PATHS = frozenset(
    {
        "/fapi/v1/exchangeInfo",
        "/fapi/v1/order",
        "/fapi/v1/premiumIndex",
        "/fapi/v1/userTrades",
        "/fapi/v2/account",
    }
)

_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SESSION_NONCE = re.compile(r"^[0-9a-f]{16}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_AUTHORIZATION_ID = re.compile(r"^g1b16-[0-9a-f]{16}$")


class MutationProtocolError(ValueError):
    """Raised when the draft mutation contract cannot be satisfied safely."""


@dataclass(frozen=True)
class AccountState:
    """Only the exact read-only account state allowed before a probe create."""

    can_trade: bool
    dual_side_position: bool
    multi_assets_margin: bool
    margin_type: str
    leverage: int
    auto_add_margin: bool
    server_time_skew_ms: Decimal
    wallet_balance: Decimal
    available_balance: Decimal
    nonzero_positions: tuple[tuple[str, Decimal], ...]
    open_regular_order_ids: tuple[str, ...]
    open_algo_order_ids: tuple[str, ...]


@dataclass(frozen=True)
class SymbolState:
    """Venue metadata which removes runtime instrument and order-field choice."""

    symbol: str
    status: str
    contract_type: str
    quote_asset: str
    margin_asset: str
    order_types: frozenset[str]
    time_in_force: frozenset[str]
    filter_type_counts: tuple[tuple[str, int], ...]
    uninterpreted_applicable_filter_types: tuple[str, ...]


class RequestPurpose(StrEnum):
    """The only request purposes available to a future thin runner."""

    READ = "READ"
    CREATE = "CREATE"
    CANCEL = "CANCEL"
    EMERGENCY_CLOSE = "EMERGENCY_CLOSE"


class RequestStage(StrEnum):
    """Mutation stages reconstructed from the durable ledger and fresh proofs."""

    CREATE_READY = "CREATE_READY"
    CREATE_ATTEMPTED = "CREATE_ATTEMPTED"
    OWNED_ORDER_OPEN = "OWNED_ORDER_OPEN"
    CANCEL_ATTEMPTED = "CANCEL_ATTEMPTED"
    OWNED_POSITION_PROVEN = "OWNED_POSITION_PROVEN"
    EMERGENCY_CLOSE_ATTEMPTED = "EMERGENCY_CLOSE_ATTEMPTED"


def validate_account_state(state: AccountState, *, required_notional: Decimal) -> None:
    """Fail closed without changing position, margin, leverage, or account mode."""

    if (
        type(state.can_trade) is not bool
        or type(state.dual_side_position) is not bool
        or type(state.multi_assets_margin) is not bool
        or type(state.leverage) is not int
        or type(state.auto_add_margin) is not bool
    ):
        raise MutationProtocolError("INVALID_ACCOUNT_STATE_TYPE")
    if (
        not state.can_trade
        or state.dual_side_position
        or state.multi_assets_margin
        or state.margin_type != "ISOLATED"
        or state.leverage != 1
        or state.auto_add_margin
    ):
        raise MutationProtocolError("ACCOUNT_CONFIG_MISMATCH")
    decimals = (
        state.server_time_skew_ms,
        state.wallet_balance,
        state.available_balance,
        required_notional,
    )
    if any(type(value) is not Decimal or not value.is_finite() for value in decimals):
        raise MutationProtocolError("INVALID_ACCOUNT_STATE_TYPE")
    if state.server_time_skew_ms.copy_abs() > Decimal("5000"):
        raise MutationProtocolError("SERVER_TIME_SKEW_EXCEEDED")
    if state.wallet_balance <= 0 or required_notional <= 0:
        raise MutationProtocolError("ACCOUNT_BALANCE_INVALID")
    if state.available_balance < required_notional:
        raise MutationProtocolError("ACCOUNT_BALANCE_INSUFFICIENT")
    if state.nonzero_positions or state.open_regular_order_ids or state.open_algo_order_ids:
        raise MutationProtocolError("ACCOUNT_NOT_CLEAN")


def validate_symbol_state(state: SymbolState) -> None:
    """Require the one frozen perpetual and every filter used by the derivation."""

    if (
        type(state) is not SymbolState
        or any(
            type(value) is not str
            for value in (
                state.symbol,
                state.status,
                state.contract_type,
                state.quote_asset,
                state.margin_asset,
            )
        )
        or type(state.order_types) is not frozenset
        or any(type(value) is not str for value in state.order_types)
        or type(state.time_in_force) is not frozenset
        or any(type(value) is not str for value in state.time_in_force)
        or type(state.filter_type_counts) is not tuple
        or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not int
            for item in state.filter_type_counts
        )
        or type(state.uninterpreted_applicable_filter_types) is not tuple
        or any(type(value) is not str for value in state.uninterpreted_applicable_filter_types)
    ):
        raise MutationProtocolError("INVALID_SYMBOL_STATE_TYPE")
    if (
        state.symbol != SYMBOL
        or state.status != "TRADING"
        or state.contract_type != "PERPETUAL"
        or state.quote_asset != "USDT"
        or state.margin_asset != "USDT"
        or "LIMIT" not in state.order_types
        or "MARKET" not in state.order_types
        or "GTX" not in state.time_in_force
    ):
        raise MutationProtocolError("SYMBOL_CONTRACT_MISMATCH")
    counts: dict[str, int] = {}
    for filter_type, count in state.filter_type_counts:
        if filter_type in counts or type(count) is not int or count < 0:
            raise MutationProtocolError("FILTER_CARDINALITY_MISMATCH")
        counts[filter_type] = count
    if any(counts.get(filter_type) != 1 for filter_type in _REQUIRED_FILTER_TYPES):
        raise MutationProtocolError("FILTER_CARDINALITY_MISMATCH")
    if state.uninterpreted_applicable_filter_types:
        raise MutationProtocolError("UNKNOWN_APPLICABLE_FILTER")


@dataclass(frozen=True)
class LimitOrderFilters:
    """Required live venue filters for the single frozen LIMIT order."""

    min_price: Decimal
    max_price: Decimal
    tick_size: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    step_size: Decimal
    min_notional: Decimal
    percent_price_multiplier_down: Decimal
    percent_price_multiplier_up: Decimal
    price_filter_count: int = 1
    lot_size_filter_count: int = 1
    min_notional_filter_count: int = 1
    percent_price_filter_count: int = 1
    uninterpreted_applicable_filter_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(
            type(value) is not Decimal
            for value in (
                self.min_price,
                self.max_price,
                self.tick_size,
                self.min_quantity,
                self.max_quantity,
                self.step_size,
                self.min_notional,
                self.percent_price_multiplier_down,
                self.percent_price_multiplier_up,
            )
        ):
            raise MutationProtocolError("INVALID_FILTER_TYPE")
        positive = {
            "MIN_PRICE": self.min_price,
            "MAX_PRICE": self.max_price,
            "TICK_SIZE": self.tick_size,
            "MIN_QUANTITY": self.min_quantity,
            "MAX_QUANTITY": self.max_quantity,
            "STEP_SIZE": self.step_size,
            "MIN_NOTIONAL": self.min_notional,
            "PERCENT_PRICE_MULTIPLIER_DOWN": self.percent_price_multiplier_down,
            "PERCENT_PRICE_MULTIPLIER_UP": self.percent_price_multiplier_up,
        }
        for name, value in positive.items():
            if not value.is_finite() or value <= 0:
                raise MutationProtocolError(f"INVALID_{name}")
        if self.max_price < self.min_price:
            raise MutationProtocolError("INVALID_PRICE_RANGE")
        if self.max_quantity < self.min_quantity:
            raise MutationProtocolError("INVALID_QUANTITY_RANGE")
        if self.percent_price_multiplier_down > 1 or self.percent_price_multiplier_up < 1:
            raise MutationProtocolError("INVALID_PERCENT_PRICE_RANGE")
        if (
            type(self.price_filter_count) is not int
            or type(self.lot_size_filter_count) is not int
            or type(self.min_notional_filter_count) is not int
            or type(self.percent_price_filter_count) is not int
            or self.price_filter_count != 1
            or self.lot_size_filter_count != 1
            or self.min_notional_filter_count != 1
            or self.percent_price_filter_count != 1
        ):
            raise MutationProtocolError("FILTER_CARDINALITY_MISMATCH")
        if (
            type(self.uninterpreted_applicable_filter_types) is not tuple
            or self.uninterpreted_applicable_filter_types
        ):
            raise MutationProtocolError("UNKNOWN_APPLICABLE_FILTER")

    @property
    def canonical_sha256(self) -> str:
        canonical = {
            "lot_size_filter_count": self.lot_size_filter_count,
            "max_price": format(self.max_price, "f"),
            "max_quantity": format(self.max_quantity, "f"),
            "min_notional": format(self.min_notional, "f"),
            "min_notional_filter_count": self.min_notional_filter_count,
            "min_price": format(self.min_price, "f"),
            "min_quantity": format(self.min_quantity, "f"),
            "percent_price_filter_count": self.percent_price_filter_count,
            "percent_price_multiplier_down": format(
                self.percent_price_multiplier_down,
                "f",
            ),
            "percent_price_multiplier_up": format(self.percent_price_multiplier_up, "f"),
            "price_filter_count": self.price_filter_count,
            "step_size": format(self.step_size, "f"),
            "tick_size": format(self.tick_size, "f"),
            "uninterpreted_applicable_filter_types": list(
                self.uninterpreted_applicable_filter_types
            ),
        }
        encoded = json.dumps(
            canonical,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class OrderDerivationProof:
    """Hash-bound live inputs from which the only probe order is recomputed."""

    best_bid: Decimal
    best_ask: Decimal
    mark_price: Decimal
    filters: LimitOrderFilters
    filter_snapshot_sha256: str
    filter_contract_sha256: str
    book_age_ms: Decimal
    mark_age_ms: Decimal
    observed_elapsed_seconds: Decimal

    def __post_init__(self) -> None:
        values = (
            self.best_bid,
            self.best_ask,
            self.mark_price,
            self.book_age_ms,
            self.mark_age_ms,
            self.observed_elapsed_seconds,
        )
        if any(type(value) is not Decimal or not value.is_finite() for value in values):
            raise MutationProtocolError("INVALID_ORDER_DERIVATION_PROOF")
        if (
            self.book_age_ms < 0
            or self.mark_age_ms < 0
            or self.book_age_ms > Decimal("1000")
            or self.mark_age_ms > Decimal("1000")
            or self.observed_elapsed_seconds < 0
            or self.observed_elapsed_seconds > TOTAL_RUNTIME_SECONDS
            or type(self.filter_snapshot_sha256) is not str
            or _SHA256.fullmatch(self.filter_snapshot_sha256) is None
            or type(self.filter_contract_sha256) is not str
            or type(self.filters) is not LimitOrderFilters
            or self.filter_contract_sha256 != self.filters.canonical_sha256
        ):
            raise MutationProtocolError("INVALID_ORDER_DERIVATION_PROOF")
        derive_limit_order(
            best_bid=self.best_bid,
            best_ask=self.best_ask,
            mark_price=self.mark_price,
            filters=self.filters,
        )

    @property
    def order(self) -> FrozenLimitOrder:
        return derive_limit_order(
            best_bid=self.best_bid,
            best_ask=self.best_ask,
            mark_price=self.mark_price,
            filters=self.filters,
        )

    @property
    def canonical_sha256(self) -> str:
        canonical = {
            "best_ask": format(self.best_ask, "f"),
            "best_bid": format(self.best_bid, "f"),
            "book_age_ms": format(self.book_age_ms, "f"),
            "filter_contract_sha256": self.filter_contract_sha256,
            "filter_snapshot_sha256": self.filter_snapshot_sha256,
            "mark_age_ms": format(self.mark_age_ms, "f"),
            "mark_price": format(self.mark_price, "f"),
            "observed_elapsed_seconds": format(self.observed_elapsed_seconds, "f"),
        }
        encoded = json.dumps(
            canonical,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    def validate_fresh_at(self, elapsed_seconds: Decimal) -> None:
        if (
            type(elapsed_seconds) is not Decimal
            or not elapsed_seconds.is_finite()
            or elapsed_seconds < self.observed_elapsed_seconds
        ):
            raise MutationProtocolError("ORDER_INPUT_STALE")
        with localcontext() as context:
            context.prec = 50
            elapsed_ms = (elapsed_seconds - self.observed_elapsed_seconds) * 1000
            if self.book_age_ms + elapsed_ms > Decimal(
                "1000"
            ) or self.mark_age_ms + elapsed_ms > Decimal("1000"):
                raise MutationProtocolError("ORDER_INPUT_STALE")


@dataclass(frozen=True)
class MarketCloseFilters:
    """Exact applicable filters for the sole contingency MARKET close."""

    min_quantity: Decimal
    max_quantity: Decimal
    step_size: Decimal
    min_notional: Decimal
    market_lot_size_filter_count: int
    min_notional_filter_count: int
    uninterpreted_applicable_filter_types: tuple[str, ...]

    def __post_init__(self) -> None:
        values = (
            self.min_quantity,
            self.max_quantity,
            self.step_size,
            self.min_notional,
        )
        if any(
            type(value) is not Decimal or not value.is_finite() or value <= 0 for value in values
        ):
            raise MutationProtocolError("MARKET_CLOSE_FILTER_VIOLATION")
        if self.max_quantity < self.min_quantity:
            raise MutationProtocolError("MARKET_CLOSE_FILTER_VIOLATION")
        if (
            type(self.market_lot_size_filter_count) is not int
            or type(self.min_notional_filter_count) is not int
            or self.market_lot_size_filter_count != 1
            or self.min_notional_filter_count != 1
            or type(self.uninterpreted_applicable_filter_types) is not tuple
            or self.uninterpreted_applicable_filter_types
        ):
            raise MutationProtocolError("MARKET_CLOSE_FILTER_VIOLATION")

    @property
    def canonical_sha256(self) -> str:
        canonical = {
            "market_lot_size": {
                "max_quantity": format(self.max_quantity, "f"),
                "min_quantity": format(self.min_quantity, "f"),
                "step_size": format(self.step_size, "f"),
            },
            "market_lot_size_filter_count": self.market_lot_size_filter_count,
            "min_notional": format(self.min_notional, "f"),
            "min_notional_filter_count": self.min_notional_filter_count,
            "uninterpreted_applicable_filter_types": list(
                self.uninterpreted_applicable_filter_types
            ),
        }
        encoded = json.dumps(
            canonical,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class MarketCloseProof:
    """Fresh, hash-bound proof that the exact owned residual needs no rounding."""

    filter_snapshot_sha256: str
    filter_contract_sha256: str
    filters: MarketCloseFilters
    quantity: Decimal
    mark_price: Decimal
    mark_price_age_ms: Decimal
    observed_elapsed_seconds: Decimal

    def __post_init__(self) -> None:
        if (
            type(self.filter_snapshot_sha256) is not str
            or _SHA256.fullmatch(self.filter_snapshot_sha256) is None
            or type(self.filter_contract_sha256) is not str
            or type(self.filters) is not MarketCloseFilters
            or self.filter_contract_sha256 != self.filters.canonical_sha256
        ):
            raise MutationProtocolError("MARKET_CLOSE_FILTER_VIOLATION")

    @property
    def canonical_sha256(self) -> str:
        canonical = {
            "filter_snapshot_sha256": self.filter_snapshot_sha256,
            "filter_contract_sha256": self.filter_contract_sha256,
            "mark_price": format(self.mark_price, "f"),
            "mark_price_age_ms": format(self.mark_price_age_ms, "f"),
            "quantity": format(self.quantity, "f"),
            "observed_elapsed_seconds": format(self.observed_elapsed_seconds, "f"),
        }
        encoded = json.dumps(
            canonical,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()


def validate_market_close_proof(
    proof: MarketCloseProof,
    *,
    max_owned_quantity: Decimal,
    expected_filter_snapshot_sha256: str | None = None,
    reservation_elapsed_seconds: Decimal | None = None,
) -> None:
    """Prove the exact residual meets MARKET filters without changing quantity."""

    if type(proof) is not MarketCloseProof:
        raise MutationProtocolError("MARKET_CLOSE_FILTER_VIOLATION")
    if (
        type(max_owned_quantity) is not Decimal
        or not max_owned_quantity.is_finite()
        or max_owned_quantity <= 0
        or (
            expected_filter_snapshot_sha256 is not None
            and proof.filter_snapshot_sha256 != expected_filter_snapshot_sha256
        )
    ):
        raise MutationProtocolError("MARKET_CLOSE_FILTER_VIOLATION")
    values = (
        proof.quantity,
        proof.mark_price,
        proof.mark_price_age_ms,
        proof.observed_elapsed_seconds,
    )
    if any(type(value) is not Decimal or not value.is_finite() for value in values):
        raise MutationProtocolError("MARKET_CLOSE_FILTER_VIOLATION")
    filters = proof.filters
    with localcontext() as context:
        context.prec = 50
        if (
            proof.quantity <= 0
            or proof.quantity > max_owned_quantity
            or proof.quantity < filters.min_quantity
            or proof.quantity > filters.max_quantity
            or (proof.quantity - filters.min_quantity) % filters.step_size != 0
            or proof.mark_price <= 0
            or proof.mark_price_age_ms < 0
            or proof.mark_price_age_ms > Decimal("1000")
            or proof.observed_elapsed_seconds < 0
            or proof.observed_elapsed_seconds > TOTAL_RUNTIME_SECONDS
        ):
            raise MutationProtocolError("MARKET_CLOSE_FILTER_VIOLATION")
        if reservation_elapsed_seconds is not None and (
            type(reservation_elapsed_seconds) is not Decimal
            or not reservation_elapsed_seconds.is_finite()
            or reservation_elapsed_seconds < proof.observed_elapsed_seconds
            or proof.mark_price_age_ms
            + (reservation_elapsed_seconds - proof.observed_elapsed_seconds) * 1000
            > Decimal("1000")
        ):
            raise MutationProtocolError("MARKET_CLOSE_FILTER_VIOLATION")
        if proof.mark_price * proof.quantity < filters.min_notional:
            raise MutationProtocolError("MARKET_CLOSE_FILTER_VIOLATION")


@dataclass(frozen=True)
class FrozenLimitOrder:
    """The exact order fields which may be sent by a later approved runner."""

    price: Decimal
    quantity: Decimal
    symbol: str = SYMBOL
    side: str = "BUY"
    order_type: str = "LIMIT"
    time_in_force: str = "GTX"
    position_side: str = "BOTH"
    reduce_only: bool = False
    response_type: str = "ACK"

    def __post_init__(self) -> None:
        with localcontext() as context:
            context.prec = 50
            notional_over_cap = (
                type(self.price) is Decimal
                and type(self.quantity) is Decimal
                and self.price.is_finite()
                and self.quantity.is_finite()
                and self.price * self.quantity > MAX_NOTIONAL_USDT
            )
        if (
            self.symbol != SYMBOL
            or self.side != "BUY"
            or self.order_type != "LIMIT"
            or self.time_in_force != "GTX"
            or self.position_side != "BOTH"
            or self.reduce_only is not False
            or self.response_type != "ACK"
            or type(self.price) is not Decimal
            or type(self.quantity) is not Decimal
            or not self.price.is_finite()
            or not self.quantity.is_finite()
            or self.price <= 0
            or self.quantity <= 0
            or notional_over_cap
        ):
            raise MutationProtocolError("FROZEN_ORDER_CONTRACT_MISMATCH")

    @property
    def notional(self) -> Decimal:
        with localcontext() as context:
            context.prec = 50
            return self.price * self.quantity

    def as_payload(self, *, client_order_id: str) -> dict[str, str]:
        """Return the pre-signing payload; credentials and signatures are out of scope."""

        return {
            "symbol": self.symbol,
            "side": self.side,
            "type": self.order_type,
            "timeInForce": self.time_in_force,
            "quantity": format(self.quantity, "f"),
            "price": format(self.price, "f"),
            "positionSide": self.position_side,
            "reduceOnly": "false",
            "newClientOrderId": client_order_id,
            "newOrderRespType": self.response_type,
            "recvWindow": str(RECEIVE_WINDOW_MS),
        }


@dataclass(frozen=True)
class MutationLedger:
    """Write-ahead request counters which must be atomically persisted before I/O."""

    total_http_requests: int = 0
    create_requests: int = 0
    cancel_requests: int = 0
    emergency_close_requests: int = 0
    read_retry_requests: int = 0
    post_create_read_requests: int = 0
    stage: RequestStage = RequestStage.CREATE_READY
    last_elapsed_seconds: Decimal = Decimal("0")
    retryable_read_sha256: str | None = None

    def __post_init__(self) -> None:
        counters = (
            self.total_http_requests,
            self.create_requests,
            self.cancel_requests,
            self.emergency_close_requests,
            self.read_retry_requests,
            self.post_create_read_requests,
        )
        if any(type(value) is not int or value < 0 for value in counters):
            raise MutationProtocolError("INVALID_MUTATION_LEDGER")
        if self.retryable_read_sha256 is not None and (
            type(self.retryable_read_sha256) is not str
            or _SHA256.fullmatch(self.retryable_read_sha256) is None
        ):
            raise MutationProtocolError("INVALID_MUTATION_LEDGER")
        if self.create_requests == 0:
            if (
                self.cancel_requests
                or self.emergency_close_requests
                or self.post_create_read_requests
                or self.stage is not RequestStage.CREATE_READY
            ):
                raise MutationProtocolError("INVALID_MUTATION_LEDGER")
        elif self.emergency_close_requests == 1:
            if self.stage is not RequestStage.EMERGENCY_CLOSE_ATTEMPTED:
                raise MutationProtocolError("INVALID_MUTATION_LEDGER")
        elif self.stage not in {
            RequestStage.CREATE_ATTEMPTED,
            RequestStage.OWNED_ORDER_OPEN,
            RequestStage.CANCEL_ATTEMPTED,
            RequestStage.OWNED_POSITION_PROVEN,
        }:
            raise MutationProtocolError("INVALID_MUTATION_LEDGER")
        if (
            type(self.last_elapsed_seconds) is not Decimal
            or not self.last_elapsed_seconds.is_finite()
            or self.last_elapsed_seconds < 0
            or self.last_elapsed_seconds > TOTAL_RUNTIME_SECONDS
        ):
            raise MutationProtocolError("INVALID_MUTATION_LEDGER")
        mutation_requests = (
            self.create_requests + self.cancel_requests + self.emergency_close_requests
        )
        if (
            self.total_http_requests
            < mutation_requests + max(self.post_create_read_requests, self.read_retry_requests)
            or self.total_http_requests > MAX_HTTP_REQUESTS
            or self.create_requests > 1
            or self.cancel_requests > 2
            or self.emergency_close_requests > 1
            or self.read_retry_requests > MAX_READ_RETRIES
            or self.post_create_read_requests > MAX_POST_CREATE_READ_REQUESTS
            or mutation_requests > MAX_HARD_MUTATION_REQUESTS
            or type(self.stage) is not RequestStage
        ):
            raise MutationProtocolError("INVALID_MUTATION_LEDGER")


def _logical_request_sha256(
    *,
    intent_sha256: str,
    origin: str,
    method: str,
    path: str,
    purpose: RequestPurpose,
    parameters: tuple[tuple[str, str], ...],
) -> str:
    canonical = {
        "intent_sha256": intent_sha256,
        "method": method,
        "origin": origin,
        "parameters": dict(parameters),
        "path": path,
        "purpose": purpose.value,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ReservedRequest:
    """Canonical pre-I/O reservation which a runner must atomically write and fsync."""

    ledger: MutationLedger
    intent_sha256: str
    origin: str
    method: str
    path: str
    purpose: RequestPurpose
    parameters: tuple[tuple[str, str], ...]
    elapsed_seconds: Decimal
    retry_index: int

    def __post_init__(self) -> None:
        if (
            type(self.ledger) is not MutationLedger
            or type(self.intent_sha256) is not str
            or _SHA256.fullmatch(self.intent_sha256) is None
            or self.origin != DEMO_HTTP_ORIGIN
            or self.method not in {"GET", "POST", "DELETE"}
            or type(self.purpose) is not RequestPurpose
            or type(self.parameters) is not tuple
            or any(
                type(pair) is not tuple
                or len(pair) != 2
                or type(pair[0]) is not str
                or type(pair[1]) is not str
                for pair in self.parameters
            )
            or tuple(sorted(self.parameters)) != self.parameters
            or len({key for key, _ in self.parameters}) != len(self.parameters)
            or type(self.elapsed_seconds) is not Decimal
            or not self.elapsed_seconds.is_finite()
            or self.elapsed_seconds < 0
            or self.elapsed_seconds > TOTAL_RUNTIME_SECONDS
            or self.elapsed_seconds != self.ledger.last_elapsed_seconds
            or type(self.retry_index) is not int
            or self.retry_index < 0
            or self.retry_index > MAX_READ_RETRIES
        ):
            raise MutationProtocolError("INVALID_REQUEST_RESERVATION")

    @property
    def request_sha256(self) -> str:
        canonical = {
            "elapsed_seconds": format(self.elapsed_seconds, "f"),
            "intent_sha256": self.intent_sha256,
            "ledger": {
                "cancel_requests": self.ledger.cancel_requests,
                "create_requests": self.ledger.create_requests,
                "emergency_close_requests": self.ledger.emergency_close_requests,
                "last_elapsed_seconds": format(self.ledger.last_elapsed_seconds, "f"),
                "post_create_read_requests": self.ledger.post_create_read_requests,
                "read_retry_requests": self.ledger.read_retry_requests,
                "retryable_read_sha256": self.ledger.retryable_read_sha256,
                "stage": self.ledger.stage.value,
                "total_http_requests": self.ledger.total_http_requests,
            },
            "method": self.method,
            "origin": self.origin,
            "parameters": dict(self.parameters),
            "path": self.path,
            "purpose": self.purpose.value,
            "retry_index": self.retry_index,
        }
        encoded = json.dumps(
            canonical,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def logical_request_sha256(self) -> str:
        return _logical_request_sha256(
            intent_sha256=self.intent_sha256,
            origin=self.origin,
            method=self.method,
            path=self.path,
            purpose=self.purpose,
            parameters=self.parameters,
        )


@dataclass(frozen=True)
class DurableIntent:
    """Non-secret immutable intent bound to one explicit future runtime authorization."""

    authorization_id: str
    protocol_commit: str
    protocol_tag_object: str
    protocol_sha256: str
    runtime_commit: str
    session_nonce: str
    order_derivation: OrderDerivationProof
    persisted: bool

    def __post_init__(self) -> None:
        if (
            type(self.authorization_id) is not str
            or _AUTHORIZATION_ID.fullmatch(self.authorization_id) is None
        ):
            raise MutationProtocolError("INVALID_AUTHORIZATION_ID")
        if (
            type(self.protocol_commit) is not str
            or type(self.protocol_tag_object) is not str
            or type(self.protocol_sha256) is not str
            or _GIT_COMMIT.fullmatch(self.protocol_commit) is None
            or _GIT_COMMIT.fullmatch(self.protocol_tag_object) is None
            or _SHA256.fullmatch(self.protocol_sha256) is None
        ):
            raise MutationProtocolError("INVALID_PROTOCOL_BINDING")
        if (
            type(self.runtime_commit) is not str
            or _GIT_COMMIT.fullmatch(self.runtime_commit) is None
        ):
            raise MutationProtocolError("INVALID_RUNTIME_COMMIT")
        if (
            type(self.session_nonce) is not str
            or _SESSION_NONCE.fullmatch(self.session_nonce) is None
        ):
            raise MutationProtocolError("INVALID_SESSION_NONCE")
        if type(self.order_derivation) is not OrderDerivationProof:
            raise MutationProtocolError("INVALID_ORDER_DERIVATION_PROOF")
        if type(self.persisted) is not bool:
            raise MutationProtocolError("INVALID_INTENT_PERSISTENCE")

    @property
    def probe_order(self) -> FrozenLimitOrder:
        return self.order_derivation.order

    @property
    def filter_snapshot_sha256(self) -> str:
        return self.order_derivation.filter_snapshot_sha256

    @property
    def client_order_id(self) -> str:
        return build_client_order_id(self.runtime_commit, self.session_nonce)

    @property
    def emergency_client_order_id(self) -> str:
        return build_emergency_client_order_id(self.runtime_commit, self.session_nonce)

    @property
    def intent_sha256(self) -> str:
        canonical = {
            "authorization_id": self.authorization_id,
            "budgets": {
                "max_hard_mutation_requests": MAX_HARD_MUTATION_REQUESTS,
                "max_http_requests": MAX_HTTP_REQUESTS,
                "post_create_http_reserve": POST_CREATE_HTTP_RESERVE,
            },
            "filter_snapshot_sha256": self.filter_snapshot_sha256,
            "order_derivation_sha256": self.order_derivation.canonical_sha256,
            "probe_payload": self.probe_payload,
            "protocol_commit": self.protocol_commit,
            "protocol_sha256": self.protocol_sha256,
            "protocol_tag": "nt-gate-1b-v1.6-protocol",
            "protocol_tag_object": self.protocol_tag_object,
            "protocol_version": PROTOCOL_VERSION,
            "runtime_commit": self.runtime_commit,
            "session_nonce": self.session_nonce,
        }
        encoded = json.dumps(
            canonical,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def probe_payload(self) -> dict[str, str]:
        return self.probe_order.as_payload(client_order_id=self.client_order_id)

    @property
    def query_parameters(self) -> dict[str, str]:
        return {
            "symbol": SYMBOL,
            "origClientOrderId": self.client_order_id,
            "recvWindow": str(RECEIVE_WINDOW_MS),
        }

    @property
    def cancel_parameters(self) -> dict[str, str]:
        return self.query_parameters

    @property
    def emergency_query_parameters(self) -> dict[str, str]:
        return {
            "symbol": SYMBOL,
            "origClientOrderId": self.emergency_client_order_id,
            "recvWindow": str(RECEIVE_WINDOW_MS),
        }

    def emergency_close_payload(self, residual_quantity: Decimal) -> dict[str, str]:
        if (
            type(residual_quantity) is not Decimal
            or not residual_quantity.is_finite()
            or residual_quantity <= 0
            or residual_quantity > self.probe_order.quantity
        ):
            raise MutationProtocolError("INVALID_OWNED_POSITION_QUANTITY")
        return {
            "symbol": SYMBOL,
            "side": "SELL",
            "type": "MARKET",
            "quantity": format(residual_quantity, "f"),
            "positionSide": "BOTH",
            "reduceOnly": "true",
            "newClientOrderId": self.emergency_client_order_id,
            "newOrderRespType": "ACK",
            "recvWindow": str(RECEIVE_WINDOW_MS),
        }


@dataclass(frozen=True)
class OwnedOrderProof:
    """Fresh allowlisted order observation tied to the exact durable intent."""

    intent_sha256: str
    symbol: str
    client_order_id: str
    status: str
    executed_quantity: Decimal
    observed_after_http_attempt: int
    source_request_sha256: str
    accepted_elapsed_seconds: Decimal
    observed_elapsed_seconds: Decimal


@dataclass(frozen=True)
class OwnedPositionProof:
    """Fresh residual position proof attributable only to the probe fill."""

    intent_sha256: str
    symbol: str
    residual_quantity: Decimal
    owned_executed_quantity: Decimal
    position_direction: str
    probe_terminal_status: str
    open_remainder_quantity: Decimal
    other_activity_absent: bool
    market_close_proof: MarketCloseProof
    observed_after_http_attempt: int
    source_request_sha256s: tuple[tuple[str, str], ...]
    observed_elapsed_seconds: Decimal


@dataclass
class MutationRequestGuard:
    """Reserve an exact request and return its canonical record for pre-I/O fsync.

    This offline guard does not perform persistence or network I/O. A future runner must
    atomically persist the returned reservation before it is allowed to send the request.
    """

    intent: DurableIntent
    ledger: MutationLedger = field(default_factory=MutationLedger)
    _owned_order_proof: OwnedOrderProof | None = field(default=None, init=False, repr=False)
    _owned_position_proof: OwnedPositionProof | None = field(default=None, init=False, repr=False)
    _post_create_read_reservations: dict[str, str] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _pending_read_reservations: dict[str, ReservedRequest] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    @property
    def stage(self) -> RequestStage:
        return self.ledger.stage

    def note_owned_order_proof(self, proof: OwnedOrderProof) -> None:
        if (
            self.ledger.create_requests != 1
            or self.stage not in {RequestStage.CREATE_ATTEMPTED, RequestStage.CANCEL_ATTEMPTED}
            or proof.intent_sha256 != self.intent.intent_sha256
            or proof.symbol != SYMBOL
            or proof.client_order_id != self.intent.client_order_id
            or proof.status not in {"NEW", "PARTIALLY_FILLED"}
            or type(proof.executed_quantity) is not Decimal
            or not proof.executed_quantity.is_finite()
            or proof.executed_quantity < 0
            or proof.observed_after_http_attempt != self.ledger.total_http_requests
            or self._post_create_read_reservations.get("/fapi/v1/order")
            != proof.source_request_sha256
            or type(proof.accepted_elapsed_seconds) is not Decimal
            or type(proof.observed_elapsed_seconds) is not Decimal
            or not proof.accepted_elapsed_seconds.is_finite()
            or not proof.observed_elapsed_seconds.is_finite()
            or proof.accepted_elapsed_seconds < 0
            or proof.observed_elapsed_seconds < proof.accepted_elapsed_seconds
            or proof.observed_elapsed_seconds < self.ledger.last_elapsed_seconds
            or proof.observed_elapsed_seconds > TOTAL_RUNTIME_SECONDS
        ):
            raise MutationProtocolError("OWNERSHIP_PROOF_MISMATCH")
        self._owned_order_proof = proof
        self.ledger = replace(
            self.ledger,
            stage=RequestStage.OWNED_ORDER_OPEN,
            last_elapsed_seconds=proof.observed_elapsed_seconds,
        )

    def note_owned_position_proof(self, proof: OwnedPositionProof) -> None:
        if (
            self.ledger.create_requests != 1
            or proof.intent_sha256 != self.intent.intent_sha256
            or proof.symbol != SYMBOL
            or type(proof.residual_quantity) is not Decimal
            or type(proof.owned_executed_quantity) is not Decimal
            or type(proof.open_remainder_quantity) is not Decimal
            or not proof.residual_quantity.is_finite()
            or not proof.owned_executed_quantity.is_finite()
            or not proof.open_remainder_quantity.is_finite()
            or proof.residual_quantity <= 0
            or proof.residual_quantity != proof.owned_executed_quantity
            or proof.residual_quantity > self.intent.probe_order.quantity
            or proof.position_direction != "LONG"
            or proof.probe_terminal_status
            not in {"CANCELED", "FILLED", "EXPIRED", "EXPIRED_IN_MATCH"}
            or proof.open_remainder_quantity != 0
            or proof.other_activity_absent is not True
            or type(proof.market_close_proof) is not MarketCloseProof
            or proof.market_close_proof.quantity != proof.residual_quantity
            or proof.observed_after_http_attempt != self.ledger.total_http_requests
            or set(self._post_create_read_reservations) < _OWNED_POSITION_READ_PATHS
            or proof.source_request_sha256s
            != tuple(
                sorted(
                    (path, self._post_create_read_reservations[path])
                    for path in _OWNED_POSITION_READ_PATHS
                )
            )
            or type(proof.observed_elapsed_seconds) is not Decimal
            or not proof.observed_elapsed_seconds.is_finite()
            or proof.observed_elapsed_seconds < 0
            or proof.observed_elapsed_seconds < self.ledger.last_elapsed_seconds
            or proof.observed_elapsed_seconds > TOTAL_RUNTIME_SECONDS
        ):
            raise MutationProtocolError("OWNERSHIP_PROOF_MISMATCH")
        validate_market_close_proof(
            proof.market_close_proof,
            max_owned_quantity=self.intent.probe_order.quantity,
            expected_filter_snapshot_sha256=self.intent.filter_snapshot_sha256,
            reservation_elapsed_seconds=proof.observed_elapsed_seconds,
        )
        self._owned_position_proof = proof
        self.ledger = replace(
            self.ledger,
            stage=RequestStage.OWNED_POSITION_PROVEN,
            last_elapsed_seconds=proof.observed_elapsed_seconds,
        )

    def note_read_succeeded(self, reservation: ReservedRequest) -> MutationLedger:
        """Admit a parsed allowlisted response as a later ownership-proof source."""

        if (
            type(reservation) is not ReservedRequest
            or reservation.purpose is not RequestPurpose.READ
            or reservation.intent_sha256 != self.intent.intent_sha256
            or reservation.ledger != self.ledger
            or self._pending_read_reservations.get(reservation.path) != reservation
        ):
            raise MutationProtocolError("READ_SUCCESS_PROOF_MISMATCH")
        self._pending_read_reservations.pop(reservation.path, None)
        if reservation.ledger.create_requests == 1:
            self._post_create_read_reservations[reservation.path] = reservation.request_sha256
        return self.ledger

    def note_read_failed(self, reservation: ReservedRequest) -> MutationLedger:
        """Persist a same-request retry token only for the latest failed read."""

        if (
            type(reservation) is not ReservedRequest
            or reservation.purpose is not RequestPurpose.READ
            or reservation.intent_sha256 != self.intent.intent_sha256
            or reservation.ledger != self.ledger
            or self._pending_read_reservations.get(reservation.path) != reservation
        ):
            raise MutationProtocolError("READ_FAILURE_PROOF_MISMATCH")
        self._pending_read_reservations.pop(reservation.path, None)
        self._post_create_read_reservations.pop(reservation.path, None)
        self.ledger = replace(
            self.ledger,
            retryable_read_sha256=reservation.logical_request_sha256,
        )
        return self.ledger

    def reserve(
        self,
        *,
        origin: str,
        method: str,
        path: str,
        purpose: RequestPurpose,
        parameters: Mapping[str, object],
        elapsed_seconds: Decimal,
        retry_index: int,
    ) -> ReservedRequest:
        """Consume one slot and return the exact record caller must fsync before send."""

        if origin != DEMO_HTTP_ORIGIN:
            raise MutationProtocolError("DEMO_ENDPOINT_MISMATCH")
        if (
            type(elapsed_seconds) is not Decimal
            or not elapsed_seconds.is_finite()
            or elapsed_seconds < 0
            or elapsed_seconds < self.ledger.last_elapsed_seconds
            or elapsed_seconds > TOTAL_RUNTIME_SECONDS
        ):
            raise MutationProtocolError("REQUEST_DEADLINE_EXCEEDED")
        if type(retry_index) is not int or retry_index < 0 or retry_index > MAX_READ_RETRIES:
            raise MutationProtocolError("REQUEST_RETRY_BUDGET_EXCEEDED")
        if self.ledger.total_http_requests >= MAX_HTTP_REQUESTS:
            raise MutationProtocolError("HTTP_REQUEST_BUDGET_EXCEEDED")

        normalized_method = method.upper()
        normalized_parameters = tuple(
            sorted((str(key), str(value)) for key, value in parameters.items())
        )
        logical_request_sha256 = _logical_request_sha256(
            intent_sha256=self.intent.intent_sha256,
            origin=origin,
            method=normalized_method,
            path=path,
            purpose=purpose,
            parameters=normalized_parameters,
        )
        new_ledger = replace(
            self.ledger,
            total_http_requests=self.ledger.total_http_requests + 1,
            last_elapsed_seconds=elapsed_seconds,
            retryable_read_sha256=None,
        )
        if purpose is RequestPurpose.READ:
            self._validate_read(normalized_method, path, parameters)
            if self.ledger.create_requests == 1:
                if self.ledger.post_create_read_requests >= MAX_POST_CREATE_READ_REQUESTS:
                    raise MutationProtocolError("POST_CREATE_READ_BUDGET_EXCEEDED")
                new_ledger = replace(
                    new_ledger,
                    post_create_read_requests=self.ledger.post_create_read_requests + 1,
                )
            if retry_index == 1:
                if self.ledger.read_retry_requests >= MAX_READ_RETRIES:
                    raise MutationProtocolError("READ_RETRY_BUDGET_EXCEEDED")
                if self.ledger.retryable_read_sha256 != logical_request_sha256:
                    raise MutationProtocolError("READ_RETRY_NOT_PROVEN")
                new_ledger = replace(
                    new_ledger,
                    read_retry_requests=self.ledger.read_retry_requests + 1,
                )
        else:
            if retry_index != 0:
                raise MutationProtocolError("MUTATION_RETRY_FORBIDDEN")
            if not self.intent.persisted:
                raise MutationProtocolError("DURABLE_INTENT_REQUIRED")
            if normalized_method not in {"POST", "DELETE"} or path != "/fapi/v1/order":
                raise MutationProtocolError("REQUEST_NOT_ALLOWLISTED")

            if purpose is RequestPurpose.CREATE:
                if self.ledger.create_requests >= 1:
                    raise MutationProtocolError("CREATE_BUDGET_EXCEEDED")
                if self.stage is not RequestStage.CREATE_READY:
                    raise MutationProtocolError("REQUEST_STAGE_MISMATCH")
                if elapsed_seconds > CREATE_DEADLINE_SECONDS:
                    raise MutationProtocolError("CREATE_DEADLINE_EXCEEDED")
                self.intent.order_derivation.validate_fresh_at(elapsed_seconds)
                if (
                    self.ledger.total_http_requests + 1 + POST_CREATE_HTTP_RESERVE
                    > MAX_HTTP_REQUESTS
                ):
                    raise MutationProtocolError("CLEANUP_RESERVE_EXHAUSTED")
                if normalized_method != "POST":
                    raise MutationProtocolError("REQUEST_NOT_ALLOWLISTED")
                self._match_parameters(parameters, self.intent.probe_payload)
                new_ledger = replace(
                    new_ledger,
                    create_requests=self.ledger.create_requests + 1,
                    stage=RequestStage.CREATE_ATTEMPTED,
                )
            elif purpose is RequestPurpose.CANCEL:
                if self.ledger.cancel_requests >= 2:
                    raise MutationProtocolError("CANCEL_BUDGET_EXCEEDED")
                if (
                    self.stage is not RequestStage.OWNED_ORDER_OPEN
                    or self._owned_order_proof is None
                ):
                    raise MutationProtocolError("OPEN_ORDER_PROOF_REQUIRED")
                if normalized_method != "DELETE":
                    raise MutationProtocolError("REQUEST_NOT_ALLOWLISTED")
                self._match_parameters(parameters, self.intent.cancel_parameters)
                new_ledger = replace(
                    new_ledger,
                    cancel_requests=self.ledger.cancel_requests + 1,
                    stage=RequestStage.CANCEL_ATTEMPTED,
                )
                self._owned_order_proof = None
                self._post_create_read_reservations.pop("/fapi/v1/order", None)
                self._pending_read_reservations.pop("/fapi/v1/order", None)
            elif purpose is RequestPurpose.EMERGENCY_CLOSE:
                if self.ledger.emergency_close_requests >= 1:
                    raise MutationProtocolError("EMERGENCY_CLOSE_BUDGET_EXCEEDED")
                if (
                    self.stage is not RequestStage.OWNED_POSITION_PROVEN
                    or self._owned_position_proof is None
                ):
                    raise MutationProtocolError("OWNED_POSITION_PROOF_REQUIRED")
                if normalized_method != "POST":
                    raise MutationProtocolError("REQUEST_NOT_ALLOWLISTED")
                expected = self.intent.emergency_close_payload(
                    self._owned_position_proof.residual_quantity
                )
                validate_market_close_proof(
                    self._owned_position_proof.market_close_proof,
                    max_owned_quantity=self.intent.probe_order.quantity,
                    expected_filter_snapshot_sha256=self.intent.filter_snapshot_sha256,
                    reservation_elapsed_seconds=elapsed_seconds,
                )
                self._match_parameters(parameters, expected)
                new_ledger = replace(
                    new_ledger,
                    emergency_close_requests=self.ledger.emergency_close_requests + 1,
                    stage=RequestStage.EMERGENCY_CLOSE_ATTEMPTED,
                )
                self._owned_position_proof = None
                self._post_create_read_reservations.clear()
                self._pending_read_reservations.clear()
            else:  # pragma: no cover - StrEnum exhaustiveness guard.
                raise MutationProtocolError("REQUEST_NOT_ALLOWLISTED")

        self.ledger = new_ledger
        reservation = ReservedRequest(
            ledger=new_ledger,
            intent_sha256=self.intent.intent_sha256,
            origin=origin,
            method=normalized_method,
            path=path,
            purpose=purpose,
            parameters=normalized_parameters,
            elapsed_seconds=elapsed_seconds,
            retry_index=retry_index,
        )
        if purpose is RequestPurpose.CREATE:
            self._post_create_read_reservations.clear()
            self._pending_read_reservations.clear()
        elif purpose is RequestPurpose.READ and new_ledger.create_requests == 1:
            self._post_create_read_reservations.pop(path, None)
            self._pending_read_reservations[path] = reservation
        elif purpose is RequestPurpose.READ:
            self._pending_read_reservations[path] = reservation
        return reservation

    def _validate_read(
        self,
        method: str,
        path: str,
        parameters: Mapping[str, object],
    ) -> None:
        if method != "GET" or path not in _ALLOWED_READ_PATHS:
            raise MutationProtocolError("REQUEST_NOT_ALLOWLISTED")
        if path == "/fapi/v1/order":
            allowed = [self.intent.query_parameters]
            if self.ledger.emergency_close_requests == 1:
                allowed.append(self.intent.emergency_query_parameters)
            if dict(parameters) not in allowed:
                raise MutationProtocolError("ORDER_PARAMETER_MISMATCH")
            return
        recv_window = {"recvWindow": str(RECEIVE_WINDOW_MS)}
        expected_by_path: dict[str, dict[str, str]] = {
            "/fapi/v1/time": {},
            "/fapi/v1/exchangeInfo": {},
            "/fapi/v1/ticker/bookTicker": {"symbol": SYMBOL},
            "/fapi/v1/premiumIndex": {"symbol": SYMBOL},
            "/fapi/v1/positionSide/dual": recv_window,
            "/fapi/v1/symbolConfig": {"symbol": SYMBOL, **recv_window},
            "/fapi/v1/openOrders": recv_window,
            "/fapi/v1/openAlgoOrders": recv_window,
            "/fapi/v1/userTrades": {"symbol": SYMBOL, **recv_window},
            "/fapi/v2/account": recv_window,
        }
        self._match_parameters(parameters, expected_by_path[path])

    @staticmethod
    def _match_parameters(
        actual: Mapping[str, object],
        expected: Mapping[str, object],
    ) -> None:
        if dict(actual) != dict(expected):
            raise MutationProtocolError("ORDER_PARAMETER_MISMATCH")


class DuplicateDisposition(StrEnum):
    """Safe action after querying the deterministic client order ID."""

    CREATE_ONCE = "CREATE_ONCE"
    RECOVER_AND_CANCEL_NO_CREATE = "RECOVER_AND_CANCEL_NO_CREATE"
    RECONCILE_FILL_NO_CREATE = "RECONCILE_FILL_NO_CREATE"
    SESSION_CONSUMED_NO_CREATE = "SESSION_CONSUMED_NO_CREATE"


class LookupOutcome(StrEnum):
    """Distinguish a venue-confirmed absence from an ambiguous lookup."""

    CONFIRMED_NOT_FOUND = "CONFIRMED_NOT_FOUND"
    FOUND = "FOUND"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class DuplicateLookup:
    """Sanitized duplicate lookup plus the same-query global clean assertion."""

    outcome: LookupOutcome
    status: str | None
    executed_quantity: Decimal
    global_state_clean: bool


@dataclass(frozen=True)
class LifecycleEvidence:
    """Sanitized summary fields; not a final artifact/hash evidence verifier."""

    create_requests: int
    cancel_requests: int
    emergency_close_requests: int
    modify_requests: int
    account_setting_mutations: int
    accepted_orders: int
    observed_statuses: tuple[str, ...]
    executed_quantity: Decimal
    fee_delta: Decimal
    funding_delta: Decimal
    wallet_balance_delta: Decimal
    total_http_requests: int
    total_runtime_seconds: Decimal
    create_elapsed_seconds: Decimal
    accepted_to_cancel_seconds: Decimal
    final_nonzero_positions: tuple[tuple[str, Decimal], ...]
    final_open_regular_orders: int
    final_open_algo_orders: int
    unexpected_mutations: int
    read_retries: int
    production_contacted: bool
    preflight_passed: bool
    final_account_config_matches: bool
    runtime_binding_passed: bool
    credential_cleanup_passed: bool
    filters_passed: bool
    order_parameters_match: bool
    cleanup_confirmed: bool

    @property
    def mutation_requests(self) -> int:
        return (
            self.create_requests
            + self.cancel_requests
            + self.emergency_close_requests
            + self.modify_requests
            + self.account_setting_mutations
        )


def _ceil_from_origin(value: Decimal, *, origin: Decimal, increment: Decimal) -> Decimal:
    if value <= origin:
        return origin
    steps = ((value - origin) / increment).to_integral_value(rounding=ROUND_CEILING)
    return origin + steps * increment


def _floor_from_origin(value: Decimal, *, origin: Decimal, increment: Decimal) -> Decimal:
    if value < origin:
        raise MutationProtocolError("PRICE_BELOW_FILTER_MINIMUM")
    steps = ((value - origin) / increment).to_integral_value(rounding=ROUND_FLOOR)
    return origin + steps * increment


def derive_limit_order(
    *,
    best_bid: Decimal,
    best_ask: Decimal,
    mark_price: Decimal,
    filters: LimitOrderFilters,
) -> FrozenLimitOrder:
    """Derive the smallest valid post-only BUY one percent below a fresh best bid."""

    with localcontext() as context:
        context.prec = 50
        return _derive_limit_order(
            best_bid=best_bid,
            best_ask=best_ask,
            mark_price=mark_price,
            filters=filters,
        )


def _derive_limit_order(
    *,
    best_bid: Decimal,
    best_ask: Decimal,
    mark_price: Decimal,
    filters: LimitOrderFilters,
) -> FrozenLimitOrder:
    if type(best_bid) is not Decimal or not best_bid.is_finite() or best_bid <= 0:
        raise MutationProtocolError("INVALID_BEST_BID")
    if type(best_ask) is not Decimal or not best_ask.is_finite() or best_ask <= best_bid:
        raise MutationProtocolError("INVALID_BOOK_SPREAD")
    if type(mark_price) is not Decimal or not mark_price.is_finite() or mark_price <= 0:
        raise MutationProtocolError("INVALID_MARK_PRICE")
    if (best_bid - filters.min_price) % filters.tick_size != 0:
        raise MutationProtocolError("BEST_BID_NOT_TICK_ALIGNED")
    if (best_ask - filters.min_price) % filters.tick_size != 0:
        raise MutationProtocolError("BEST_ASK_NOT_TICK_ALIGNED")

    raw_price = best_bid * (Decimal("1") - Decimal(PRICE_DISCOUNT_BPS) / Decimal("10000"))
    price = _floor_from_origin(
        raw_price,
        origin=filters.min_price,
        increment=filters.tick_size,
    )
    if price < filters.min_price or price > filters.max_price:
        raise MutationProtocolError("PRICE_FILTER_VIOLATION")
    if price >= best_ask:
        raise MutationProtocolError("ORDER_NOT_PASSIVE")
    if price > mark_price * filters.percent_price_multiplier_up:
        raise MutationProtocolError("PERCENT_PRICE_VIOLATION")

    required_quantity = max(filters.min_quantity, filters.min_notional / price)
    quantity = _ceil_from_origin(
        required_quantity,
        origin=filters.min_quantity,
        increment=filters.step_size,
    )
    if quantity < filters.min_quantity or quantity > filters.max_quantity:
        raise MutationProtocolError("LOT_SIZE_VIOLATION")
    if (quantity - filters.min_quantity) % filters.step_size != 0:
        raise MutationProtocolError("STEP_SIZE_VIOLATION")

    notional = price * quantity
    if notional < filters.min_notional:
        raise MutationProtocolError("MIN_NOTIONAL_VIOLATION")
    if notional > MAX_NOTIONAL_USDT:
        raise MutationProtocolError("NOTIONAL_CAP_EXCEEDED")

    return FrozenLimitOrder(price=price, quantity=quantity)


def build_client_order_id(runtime_commit: str, session_nonce: str) -> str:
    """Build one deterministic, non-secret identifier for duplicate reconciliation."""

    if type(runtime_commit) is not str or _GIT_COMMIT.fullmatch(runtime_commit) is None:
        raise MutationProtocolError("INVALID_RUNTIME_COMMIT")
    if type(session_nonce) is not str or _SESSION_NONCE.fullmatch(session_nonce) is None:
        raise MutationProtocolError("INVALID_SESSION_NONCE")
    return f"g1b16-{runtime_commit[:10]}-{session_nonce}-01"


def build_emergency_client_order_id(runtime_commit: str, session_nonce: str) -> str:
    """Build the separate deterministic ID for the sole owned-position close."""

    if type(runtime_commit) is not str or _GIT_COMMIT.fullmatch(runtime_commit) is None:
        raise MutationProtocolError("INVALID_RUNTIME_COMMIT")
    if type(session_nonce) is not str or _SESSION_NONCE.fullmatch(session_nonce) is None:
        raise MutationProtocolError("INVALID_SESSION_NONCE")
    return f"g1b16c-{runtime_commit[:8]}-{session_nonce}-1"


def validate_order_payload(
    payload: Mapping[str, object],
    *,
    expected: FrozenLimitOrder,
    client_order_id: str,
) -> None:
    """Reject optional or changed order fields before any signing or network call."""

    frozen = expected.as_payload(client_order_id=client_order_id)
    unknown = set(payload) - set(frozen)
    if unknown:
        raise MutationProtocolError("UNFROZEN_ORDER_PARAMETER")
    if dict(payload) != frozen:
        raise MutationProtocolError("ORDER_PARAMETER_MISMATCH")


def classify_duplicate(
    lookup: DuplicateLookup,
    *,
    attempt_record_exists: bool,
) -> DuplicateDisposition:
    """Permit create only after an explicit not-found result and unused authorization."""

    if type(lookup) is not DuplicateLookup or type(attempt_record_exists) is not bool:
        raise MutationProtocolError("INVALID_DUPLICATE_EVIDENCE")
    if type(lookup.global_state_clean) is not bool:
        raise MutationProtocolError("INVALID_DUPLICATE_EVIDENCE")
    if type(lookup.outcome) is not LookupOutcome or (
        lookup.status is not None and type(lookup.status) is not str
    ):
        raise MutationProtocolError("INVALID_DUPLICATE_EVIDENCE")
    if (
        type(lookup.executed_quantity) is not Decimal
        or not lookup.executed_quantity.is_finite()
        or lookup.executed_quantity < 0
    ):
        raise MutationProtocolError("INVALID_DUPLICATE_EVIDENCE")
    if lookup.outcome is LookupOutcome.UNKNOWN:
        raise MutationProtocolError("DUPLICATE_LOOKUP_UNKNOWN")
    if lookup.outcome is LookupOutcome.CONFIRMED_NOT_FOUND:
        if lookup.status is not None or lookup.executed_quantity != 0:
            raise MutationProtocolError("INVALID_DUPLICATE_EVIDENCE")
        if attempt_record_exists:
            return DuplicateDisposition.SESSION_CONSUMED_NO_CREATE
        if not lookup.global_state_clean:
            raise MutationProtocolError("ACCOUNT_NOT_CLEAN_FOR_CREATE")
        return DuplicateDisposition.CREATE_ONCE
    if lookup.outcome is not LookupOutcome.FOUND or lookup.status is None:
        raise MutationProtocolError("INVALID_DUPLICATE_EVIDENCE")

    normalized = lookup.status.upper()
    if lookup.executed_quantity > 0 or normalized in {"PARTIALLY_FILLED", "FILLED"}:
        return DuplicateDisposition.RECONCILE_FILL_NO_CREATE
    if normalized == "NEW":
        return DuplicateDisposition.RECOVER_AND_CANCEL_NO_CREATE
    if normalized in {"CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}:
        return DuplicateDisposition.SESSION_CONSUMED_NO_CREATE
    raise MutationProtocolError("UNEXPLAINED_PRIOR_ORDER_STATUS")


def validate_lifecycle_pass(evidence: LifecycleEvidence) -> None:
    """Validate only the zero-fill create-query-cancel happy path as PASS."""

    integer_counters = (
        evidence.create_requests,
        evidence.cancel_requests,
        evidence.emergency_close_requests,
        evidence.modify_requests,
        evidence.account_setting_mutations,
        evidence.accepted_orders,
        evidence.total_http_requests,
        evidence.final_open_regular_orders,
        evidence.final_open_algo_orders,
        evidence.unexpected_mutations,
        evidence.read_retries,
    )
    if any(type(value) is not int or value < 0 for value in integer_counters):
        raise MutationProtocolError("INVALID_EVIDENCE_COUNTER")
    if (
        type(evidence.executed_quantity) is not Decimal
        or not evidence.executed_quantity.is_finite()
        or evidence.executed_quantity < 0
    ):
        raise MutationProtocolError("INVALID_EXECUTED_QUANTITY")
    economic_deltas = (
        evidence.fee_delta,
        evidence.funding_delta,
        evidence.wallet_balance_delta,
    )
    if any(type(value) is not Decimal or not value.is_finite() for value in economic_deltas):
        raise MutationProtocolError("INVALID_ECONOMIC_DELTA")
    if any(value != 0 for value in economic_deltas):
        raise MutationProtocolError("UNEXPECTED_ECONOMIC_DELTA")
    timings = (
        evidence.total_runtime_seconds,
        evidence.create_elapsed_seconds,
        evidence.accepted_to_cancel_seconds,
    )
    if any(type(value) is not Decimal or not value.is_finite() or value < 0 for value in timings):
        raise MutationProtocolError("INVALID_EVIDENCE_TIMING")
    boolean_fields = (
        evidence.production_contacted,
        evidence.preflight_passed,
        evidence.final_account_config_matches,
        evidence.runtime_binding_passed,
        evidence.credential_cleanup_passed,
        evidence.filters_passed,
        evidence.order_parameters_match,
        evidence.cleanup_confirmed,
    )
    if any(type(value) is not bool for value in boolean_fields):
        raise MutationProtocolError("INVALID_EVIDENCE_BOOLEAN")
    if type(evidence.observed_statuses) is not tuple or any(
        type(status) is not str for status in evidence.observed_statuses
    ):
        raise MutationProtocolError("INVALID_LIFECYCLE_STATUS")
    if type(evidence.final_nonzero_positions) is not tuple or any(
        type(position) is not tuple
        or len(position) != 2
        or type(position[0]) is not str
        or not position[0]
        or type(position[1]) is not Decimal
        or not position[1].is_finite()
        or position[1] == 0
        for position in evidence.final_nonzero_positions
    ):
        raise MutationProtocolError("INVALID_FINAL_POSITION_EVIDENCE")

    if evidence.modify_requests:
        raise MutationProtocolError("MODIFY_FORBIDDEN")
    if evidence.account_setting_mutations:
        raise MutationProtocolError("ACCOUNT_SETTING_MUTATION_FORBIDDEN")
    if (
        evidence.create_requests > 1
        or evidence.cancel_requests > 2
        or evidence.emergency_close_requests > 1
        or evidence.mutation_requests > MAX_HARD_MUTATION_REQUESTS
    ):
        raise MutationProtocolError("MUTATION_BUDGET_EXCEEDED")
    if evidence.unexpected_mutations:
        raise MutationProtocolError("UNEXPECTED_MUTATION")
    if evidence.production_contacted:
        raise MutationProtocolError("PRODUCTION_CONTACTED")
    if not evidence.preflight_passed:
        raise MutationProtocolError("PREFLIGHT_NOT_PROVEN")
    if not evidence.final_account_config_matches:
        raise MutationProtocolError("FINAL_ACCOUNT_CONFIG_MISMATCH")
    if evidence.executed_quantity != 0:
        raise MutationProtocolError("UNEXPECTED_FILL")
    if evidence.total_http_requests > MAX_HTTP_REQUESTS:
        raise MutationProtocolError("HTTP_REQUEST_BUDGET_EXCEEDED")
    if evidence.total_runtime_seconds > TOTAL_RUNTIME_SECONDS:
        raise MutationProtocolError("TOTAL_RUNTIME_EXCEEDED")
    if evidence.create_elapsed_seconds > CREATE_DEADLINE_SECONDS:
        raise MutationProtocolError("CREATE_DEADLINE_EXCEEDED")
    if evidence.accepted_to_cancel_seconds > MAX_ACCEPTED_TO_CANCEL_SECONDS:
        raise MutationProtocolError("CANCEL_DEADLINE_EXCEEDED")
    if evidence.read_retries > MAX_READ_RETRIES:
        raise MutationProtocolError("READ_RETRY_BUDGET_EXCEEDED")
    if evidence.total_http_requests != NORMAL_TOTAL_HTTP_REQUESTS + evidence.read_retries:
        raise MutationProtocolError("NORMAL_PATH_HTTP_REQUEST_MISMATCH")
    with localcontext() as context:
        context.prec = 50
        if (
            evidence.total_runtime_seconds <= 0
            or evidence.create_elapsed_seconds > evidence.total_runtime_seconds
            or evidence.create_elapsed_seconds + evidence.accepted_to_cancel_seconds
            > evidence.total_runtime_seconds
        ):
            raise MutationProtocolError("INVALID_EVIDENCE_TIMING")
    if not evidence.runtime_binding_passed:
        raise MutationProtocolError("RUNTIME_BINDING_FAILED")
    if not evidence.credential_cleanup_passed:
        raise MutationProtocolError("CREDENTIAL_CLEANUP_FAILED")
    if not evidence.filters_passed:
        raise MutationProtocolError("FILTER_VALIDATION_FAILED")
    if not evidence.order_parameters_match:
        raise MutationProtocolError("ORDER_PARAMETERS_NOT_PROVEN")
    if not evidence.cleanup_confirmed:
        raise MutationProtocolError("CLEANUP_NOT_CONFIRMED")
    if (
        evidence.final_nonzero_positions
        or evidence.final_open_regular_orders != 0
        or evidence.final_open_algo_orders != 0
    ):
        raise MutationProtocolError("FINAL_ACCOUNT_NOT_CLEAN")
    if (
        evidence.create_requests != 1
        or evidence.cancel_requests != 1
        or evidence.emergency_close_requests != 0
        or evidence.mutation_requests != NORMAL_MUTATION_REQUESTS
        or evidence.accepted_orders != 1
    ):
        raise MutationProtocolError("NORMAL_PATH_MUTATION_MISMATCH")
    if evidence.observed_statuses != ("NEW", "CANCELED"):
        raise MutationProtocolError("LIFECYCLE_STATUS_MISMATCH")
