from datetime import timedelta

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy
from pandas import DataFrame


class LiveExecutionCanaryStrategy(IStrategy):
    """Runtime canary only. NOT_PROVEN_ALPHA = TRUE."""

    NOT_PROVEN_ALPHA = True

    INTERFACE_VERSION = 3
    can_short = False
    timeframe = "15m"
    startup_candle_count = 1
    process_only_new_candles = True

    minimal_roi = {"0": 100.0}
    stoploss = -0.01
    trailing_stop = False
    use_exit_signal = True

    @property
    def protections(self):
        return [
            {
                "method": "CooldownPeriod",
                "stop_duration_candles": 24,
            }
        ]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Enter the single allowlisted pair on the first eligible candle. The
        # six-hour strategy CooldownPeriod keeps this deliberately low-rate.
        dataframe.loc[
            (dataframe["volume"] > 0) & (dataframe["close"] > 0),
            ["enter_long", "enter_tag"],
        ] = (1, "live_execution_canary")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        return dataframe

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ):
        # Dry-run exits quickly enough to exercise persistence and controls.
        # A separately reviewed live configuration would hold for 30 minutes.
        timeout = timedelta(seconds=120 if self.config.get("dry_run", True) else 1800)
        if current_time >= trade.open_date_utc + timeout:
            return "canary_timeout"
        return None

    def leverage(
        self,
        pair: str,
        current_time,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        return 1.0
