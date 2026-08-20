# Run manifest: study-2026-08-17-pit-funding-shock-neutral

## Identity

- run id: `run-2026-08-17-pit-funding-shock-neutral-1`
- preregistration lock commit: `53bcf63dd3584e6360d70bfd671a65152e729336`
- branch: `codex/gmaq-p0-remediation`
- command: `./scripts/gmaq-research-pit-funding-shock-neutral --data-dir /Users/ASUS/Desktop/global-quant-continuation/user_data/data/pit`
- network and credential use: none; offline read of existing public-data files

## Pinned inputs

- PIT manifest file SHA-256: `2dc62f0897e4c43bcc62ebc80b8285370fa1c7c919956a53325fed155c581c8e`
- dataset SHA-256 recorded inside that manifest: `4e2a8a7a3eebfa0011657bce0fe3e77b138db1369126a990b135bde31f8ee48f`
- PIT universes SHA-256: `47bdcd6e646ce3c92a6ca870378b4266c88abe42bac600c11af04aa0fb59f785`
- execution cost model SHA-256: `effa8ab33aa35f94788e77049adf13a4a0a3f226b68771797eded866d2689dc6`
- candidate pool: 100 current symbols; daily PIT universe spans 77 symbols
- split: 2026-06-16 00:00 UTC

## Result summary

- train: 618 legs, return -10.17%, Sharpe -0.964, max drawdown 24.57%
- OOS: 366 legs across 61 rebalance days, return -17.20%, Sharpe -1.236,
  max drawdown 33.92%
- OOS stress: return -33.29%, Sharpe -2.451, max drawdown 47.26%
- every frozen parameter-neighbor stress return was negative
- 15-minute delayed stress return: -38.16%
- largest single short-leg loss: 40.81 USDT
- verdict: `REJECT`

The study failed statistical and robustness thresholds independently of its
known data limitations. Missing promotion evidence does not soften this result.
It does not authorize Demo entries or live trading.
