# Lessons from EXPL-017

Status: `ARCHIVED_AFTER_EXPL-017-FORMAL-003`
Scope: interpretation of the completed formal result, not a new experiment or
a modification of any historical artifact.

## Evidence boundary

EXPL-017-FORMAL-003 was the only formal run (`FORMAL_RUN_COUNT=1`) and ended
`HYPOTHESIS_FAIL`. Its frozen result is the sole source for numerical results.
This note distinguishes observations from causal claims: the sample is a
curated, survivor-biased, exploration-only Price V1 universe, so it cannot
establish a universal crypto-market mechanism or live readiness.

## What the result says

1. **Train/OOS strength and holdout failure.** The premise needs qualification:
   train was positive but modest (return `7.42%`, Sharpe `0.37`), OOS was much
   stronger (return `45.66%`, Sharpe `1.44`), and final holdout fell to return
   `0.37%` and Sharpe `0.14`, below the frozen `0.50` gate. The discontinuity
   is evidence that the observed relation was not stable in the untouched
   final period. It does not identify a cause: market-state change, sampling
   variability, and earlier-sample selection remain possible explanations,
   not established causal findings.

2. **IC sign flip.** OOS mean signed IC was positive while final-holdout mean
   signed IC was negative. The combined IC t-statistic was `0.79`, below the
   frozen `1.50` requirement. Therefore the sign-changing rank relationship
   is not accepted as stable prediction; neither OOS IC nor portfolio PnL may
   be quoted alone as validation.

3. **Cost pressure.** OOS return fell from `59.66%` with no trading cost to
   `45.66%` at the 15 bps baseline and `32.85%` at 30 bps. Holdout return fell
   from `10.58%` to `0.37%` and then `-8.93%`; the baseline cost impact alone
   was about `14.00` percentage points in OOS and `10.21` in holdout. Combined
   30 bps Sharpe was `0.48`, below its gate. The apparent edge was therefore
   friction-sensitive. This is not a license to lower costs, change turnover,
   or choose a favorable execution assumption after observing the result.

4. **Regime mechanism.** The high-volatility regime had only `12` admitted
   observations against the frozen minimum of `20`. Calm and high-volatility
   slices also had similar baseline returns (`21.09%` and `20.74%`); those
   slices are not a direct causal estimator of incremental regime value. The
   evidence is therefore insufficient to claim that the sign switch added a
   reliable marginal edge. A new hypothesis may study a different,
   independently measured regime mechanism, but cannot reuse this result as
   proof of one.

5. **Concentration.** Maximum absolute incumbent/target weight was `16.30%`,
   above the frozen `15%` limit. Equal target legs did not prevent drifted
   incumbent exposure from breaching the pathwise cap. The largest symbol and
   top-five absolute contribution shares were `7.63%` and `25.07%`, so the
   formal failure is specifically the weight constraint, not a claim that one
   symbol alone created all PnL. This is a failed risk gate, not a tuning
   invitation.

## Non-repeat rules

The following are permanently prohibited for EXPL-017: rerunning
`EXPL-017-FORMAL-003`; creating `EXPL-017-FORMAL-004`; changing its formula,
parameters, splits, cost model, lifecycle treatment, universe, or PASS gates;
and presenting a parameter rescue as independent confirmation. The next study,
if any, must begin with a materially distinct economic mechanism, an explicit
data contract, and independent Hypothesis Review before it receives an EXPL
identifier. Adding another filter to the same frozen rank/state mapping solely
to remove failed observations is also parameter rescue, not a new mechanism.
