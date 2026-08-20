# Data Availability Checklist: BTC/ETH weekly TSMOM

Status before acquisition: `PREREGISTERED_NOT_FETCHED`.

Required before scoring:

- [ ] BTCUSDT daily USD-M klines for the complete declared window
- [ ] ETHUSDT daily USD-M klines for the complete declared window
- [ ] unique, strictly increasing daily timestamps and valid OHLC values
- [ ] BTCUSDT funding pagination complete for the declared window
- [ ] ETHUSDT funding pagination complete for the declared window
- [ ] source URL, fetch timestamp, record counts, first/last timestamps, and
      SHA-256 for every data file
- [ ] preregistration and execution-cost-model SHA-256 pinned in run manifest
- [ ] no private endpoint, API key, account value, or real order used

The formal runner must replace this status with a factual acquisition verdict.
Incomplete data cannot be imputed or promoted to PASS.
