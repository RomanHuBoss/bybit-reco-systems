# Audit iteration: bounded censoring and project viability

## Result
The previously documented HIGH risk was confirmed: a single terminally censored policy root changed a fitted positive model to `censored`, erased coefficients and prevented actionability indefinitely. This was a liveness defect, not a valid statistical necessity.

## Fix
The gate now distinguishes three cases:

1. unresolved, invalid-contract or invalid-labeled evidence: hard fail-closed;
2. terminal censoring up to 5%: adverse sensitivity analysis;
3. censoring above 5% or a non-positive pessimistic result: fail-closed.

For each censored root the sensitivity return is the most adverse of -1%, observed expected shortfall, or mean minus three standard deviations, capped at -100%. A model remains usable only if the pessimistically adjusted mean and the existing row and temporal one-sided lower bounds all remain positive.

## Viability conclusion
The source release alone cannot establish profitability or structural unprofitability because it contains no runtime database, current-policy outcome cohort, real fills or externally reconciled fee/funding/slippage observations. The code is intentionally fail-closed and can remain in shadow mode for long periods. Earlier releases also repeatedly changed model/policy identity, restarting exact-policy evidence cohorts.

The project should therefore be classified as an unvalidated research/recommendation system, not as a proven profitable or proven loss-making strategy. A month without actionable recommendations is compatible with the current gates and repeated lineage resets; it is not evidence by itself that expected return is negative.

## Remaining critical validation requirement
A decision on economic viability requires an exported runtime database and a frozen-policy shadow run long enough to produce the minimum temporal clusters. The decisive report must include net returns after fees, spread, slippage and funding; censoring reasons and rate; temporal-cluster lower bound; purged OOF and terminal-holdout log loss; and comparison with simple baselines.
