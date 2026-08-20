# Verdict: REJECT

- OOS net return: `-11.05%`
- OOS annualized Sharpe: `-0.152`
- OOS maximum drawdown: `36.51%`
- OOS adverse-funding stress return: `-40.19%`
- delayed stressed return: `-34.73%`
- bootstrap positive probability: `33.0%`
- failed gates: `['oos_sharpe_gte_0_70', 'oos_return_positive', 'oos_adverse_funding_stress_positive', 'oos_max_drawdown_lte_20pct', 'both_oos_halves_positive', 'all_neighbor_stress_returns_positive', 'delayed_stress_positive', 'bootstrap_positive_probability_gte_80pct', 'short_risk_below_50pct']`

This one-shot verdict does not authorize Demo or live trading. A PASS could
only enter the preregistered prospective shadow gate; a REJECT closes the
hypothesis without parameter rescue.
