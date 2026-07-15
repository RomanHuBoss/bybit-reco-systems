# Audit iteration 249 - operator Plan RR, empirical expectancy and decision-focused UI

## 1. Result

The operator-facing metric contract was materially misleading. The primary screen labelled the bounded legacy `expected_rr` heuristic as a capture/risk or reward/risk measure even though it did not use the concrete generated plan's kill-switch loss and was not estimated from current-policy matured outcomes. At the same time, the project already calculated most of the underlying plan economics and maintained exact-policy outcome diagnostics, but those data were scattered across technical JSON blocks rather than presented as decision metrics.

Version `1.0.61` separates three different concepts:

1. **Heuristic capture score** - the legacy bounded capture-to-volatility ranking diagnostic. It remains in stored/API data for backward compatibility and internal analysis, but is not rendered in the frontend.
2. **Plan RR** - a scenario ratio for the concrete generated grid plan: projected net grid reward over the recommendation horizon divided by the monotonic worst-side price and terminal execution loss at the kill-switch.
3. **Empirical expectancy / empirical tail ratio** - current-policy matured proxy-outcome mean with a two-sided Student-t confidence interval, expected shortfall and an explicitly labelled mean-to-tail ratio.

The primary recommendation table is reduced to direction, Plan RR, empirical expectancy, cross-margin risk buffer, status and the details action. Raw score/rank, raw/model confidence, direction confidence and the visible minimum-confidence filter were removed from the primary operator surface.

This iteration does **not** claim live profitability. Empirical evidence still derives from the project's conservative OHLCV proxy-outcome contract unless externally reconciled execution evidence is separately available.

## 2. Input and release identity

- Input ZIP: `bybit-reco-systems-1.0.60-postgres-ohlcv-deadlock.zip`
- Input SHA-256: `95d5d211653b4d7b7c4685020006bfd1888b839ac20a17f937a70d66e65e6f5b`
- Project root: `bybit-reco-systems-main`
- Original FastAPI version: `1.0.60`
- New FastAPI version: `1.0.61`
- Highest previous regression iteration: 248
- Current regression iteration: 249

The supplied archive had already passed archive-safety and project-fingerprint checks in iteration 248. It was unpacked into separate pristine, red-test and working copies; the input ZIP was not modified.

## 3. Project fingerprint

Matched the expected Bybit Recommender boundaries:

- recommendation/audit service, not OMS/EMS;
- Bybit Linear USDT perpetual and `futures_grid` scope;
- FastAPI application in `app/main.py`;
- canonical directional semantics in `app/trading_semantics.py`;
- SQLite and PostgreSQL persistence;
- frontend in `app/ui/static/`;
- no private Bybit order create/amend/cancel endpoint in production code.

## 4. Goal and acceptance criteria

After this iteration, the operator must see the economics and evidence needed to decide whether to consider a recommendation, without a bounded heuristic masquerading as trading RR.

Acceptance criteria:

1. The frontend contains no rendering of `expected_rr` or the label `Прокси capture/risk`.
2. Every newly published recommendation stores a fail-closed Plan RR contract derived from the generated plan, its cost model and worst-side kill-switch stress.
3. Plan RR must not double-count recurring grid-pair fees already included in `net_profit_usdt`.
4. Every newly published recommendation stores exact-current-policy empirical mean return, two-sided confidence interval, expected shortfall and mean-to-tail ratio when estimable.
5. A positive one-sided calibration gate must not render as positive operator evidence when the two-sided interval crosses zero.
6. Missing, boolean or non-finite mandatory inputs render a metric as unavailable rather than zero or a favourable fallback.
7. Old recommendations remain readable and show the new metrics as unavailable until a new publication supplies them.
8. Policy fingerprint, outcome target, model identity, DB schema, environment variables and execution boundary remain unchanged.
9. Operator DOCX/PDF/PNG and project documentation explain the new metric semantics.

## 5. Sources reviewed

- current user requirement and prior discussion of the legacy capture/risk scale;
- `README.md`, `CHANGELOG.md`, `.env.example`;
- `docs/KNOWN_RISKS.md`, `TRADING_LOGIC.md`, `ARCHITECTURE.md`, `MODULES.md`, `SCENARIOS.md`, `HOW_TO_TRADE_INFOGRAPHIC.md`;
- recent audit reports covering grid economics, cross-margin stress, expectancy uncertainty, current-policy evidence scope, calibration lineage and bounded censoring;
- `app/recommender.py`, `app/calibration.py`, `app/main.py`, `app/db.py`;
- `app/grid_math.py`, `app/risk.py`, `app/outcomes.py`, `app/policy.py`, `app/trading_semantics.py` for contract compatibility;
- frontend `index.html`, `app.js`, `styles.css` and relevant UI regressions;
- operator DOCX/PDF and root infographic.

## 6. Affected data flow

### Plan metric

`generated grid geometry` -> `params.economics` -> `cross_margin_stress` + `cost_model` -> projected completed pairs -> projected net plan reward -> worst-side kill-switch loss -> `operator_metrics.plan_rr` -> API/detail/history -> primary UI.

### Empirical metric

`matured exact-current-policy proxy outcomes` -> recency weights and non-overlapping temporal cohorts -> calibrator diagnostics -> two-sided Student-t interval + expected shortfall -> `operator_metrics.empirical_expectancy` -> API/detail/history -> primary UI.

### Legacy heuristic

`stable_range_score / coherence / trend / ATR / costs` -> legacy `expected_rr` -> stored/API compatibility field and `heuristic_capture_score.operator_visible=false`; there is no frontend rendering path.

## 7. Baseline environment and inventory

- Python: `3.13.5`
- Node: `v22.16.0`
- Input version tests: 1119 collected
- Input test files: 193
- Frontend files: 3
- Migration SQL files: 2
- Database engines: SQLite and PostgreSQL compatibility layer

Baseline inherited from the verified v1.0.60 release:

| Check | Result |
|---|---|
| `python -m pytest --collect-only -q` | 1119 tests collected |
| exhaustive non-overlapping batches | 1119 passed |
| monolithic `python -m pytest -q` | TIMED OUT near 70%, no failure summary; not counted as pass |
| `python -m compileall -q app tests main.py` | PASSED |
| `node --check app/ui/static/app.js` | PASSED |
| `python -m pip check` | FAILED only on external MoviePy/Pillow environment conflict |
| Ruff | UNAVAILABLE: module not installed |

## 8. Confirmed defects and gaps

### OPMET-249-01 - HIGH - CONFIRMED DEFECT

- Files: `app/recommender.py`, `app/ui/static/app.js`, `app/ui/static/index.html`
- Actual behavior: the primary table displayed `expected_rr` as `Прокси capture/risk`, encouraging interpretation as trading reward/risk.
- Mathematical reality: the metric was a bounded capture-to-volatility heuristic. It did not use the concrete generated plan's kill-switch loss and did not use empirical outcome distribution tails.
- Expected behavior: keep it, if required, only as an internal ranking diagnostic and display independently defined plan and empirical metrics.
- Operator impact: the screen could make structurally tiny values appear to be evidence of a weak plan, or larger historical values appear to be evidence of favourable reward/risk, although neither interpretation was warranted.
- Why tests missed it: tests asserted formatting and presence of the old field rather than its economic semantics.

### OPMET-249-02 - HIGH - CONFIRMED GAP

- File: `app/recommender.py`
- Actual behavior: no canonical operator metric compared the generated plan's projected net reward with its concrete worst-side kill-switch loss.
- Existing data: `net_profit_usdt` per completed pair, active-order estimate, fill efficiency, full cross-margin stress and cost components existed but were not assembled into a plan-level RR contract.
- Expected behavior: calculate a fail-closed Plan RR and expose the monetary numerator and denominator, not only a scalar.
- Risk impact: the operator lacked a direct view of whether the plan's projected grid economics were proportionate to the configured protective boundary.

### OPMET-249-03 - HIGH - CONFIRMED GAP

- Files: `app/calibration.py`, `app/recommender.py`, `app/main.py`, `app/db.py`, frontend
- Actual behavior: current-policy monetary diagnostics and lower-bound gates existed, but the recommendation surface did not show a two-sided uncertainty interval, expected shortfall or a clearly labelled empirical mean-to-tail ratio.
- Expected behavior: display empirical mean return only with its cohort basis, sample/cluster counts, two-sided interval and tail statistic.
- Model impact: a point estimate could be mistaken for established evidence, especially during model warm-up.

### OPMET-249-04 - HIGH - CONFIRMED DEFECT FOUND DURING IMPLEMENTATION

- File: `app/recommender.py`
- Defective draft path: subtracting `market_round_trip_cost_bps` at the plan-horizon layer would deduct recurring pair fees a second time because `net_profit_usdt` already contains the completed-pair fee model.
- Correct behavior: deduct only distinct one-time market friction (spread/slippage) and adverse funding at the plan-horizon layer.
- Financial impact if left unfixed: systematic understatement of projected reward and Plan RR.
- Regression protection: the independent Plan RR oracle includes a larger `market_round_trip_cost_bps` value and verifies that only `one_time_market_friction_bps` is applied again.

### OPMET-249-05 - MEDIUM - CONFIRMED DEFECT

- File: `app/recommender.py`
- Actual risk: the calibrator's one-sided gate status could be `positive` while an operator-facing two-sided confidence interval crossed zero.
- Correct behavior: preserve `gate_status` separately, but derive display status from the two-sided interval: positive only if lower > 0, negative only if upper < 0, otherwise uncertain.
- Operator impact: prevents green-looking evidence when uncertainty still includes zero.

### OPMET-249-06 - MEDIUM - CONFIRMED UX GAP

- Frontend files: `index.html`, `app.js`, `styles.css`
- Actual behavior: raw rank, raw/model confidence, direction confidence and a visible minimum-confidence filter occupied the primary decision surface despite being heuristic/model diagnostics, not direct plan economics or evidence.
- Correct behavior: primary table prioritizes direction, Plan RR, empirical expectancy, risk buffer and lifecycle status. Technical diagnostics may remain in detailed audit data where properly labelled.

## 9. Metric contracts

### 9.1 Plan RR

For a newly generated recommendation:

```text
projected_completed_pairs = estimated_active_orders * clamp(fill_efficiency, 0, 1)
recurring_grid_reward = max(net_profit_usdt_per_pair, 0) * projected_completed_pairs
one_time_market_cost = worst_case_position_notional * max(one_time_market_friction_bps, 0) / 10_000
adverse_funding_cost = worst_case_position_notional * max(expected_funding_bps, 0) / 10_000
projected_net_reward = recurring_grid_reward - one_time_market_cost - adverse_funding_cost
kill_switch_loss = qty_per_order * (worst_side_gross_loss_per_qty + worst_side_terminal_execution_cost_per_qty)
Plan RR = max(projected_net_reward, 0) / kill_switch_loss
```

Properties:

- recurring pair fees are already included in `net_profit_usdt_per_pair` and are not deducted again;
- positive funding receipt is not credited;
- maintenance reserve is not mislabelled as realised loss;
- the numerator and denominator are exposed in USDT;
- missing/bool/non-finite mandatory inputs produce `status=unavailable`;
- this is a scenario metric for the generated plan, not a probability forecast and not live execution truth.

### 9.2 Empirical expectancy and empirical tail ratio

The preferred mean/std/effective sample inputs come from non-overlapping temporal cohorts for the exact current policy. If unavailable, the contract discloses fallback to recency-weighted current-policy outcomes.

```text
CI = two-sided Student-t interval for weighted mean return
expected_shortfall = weighted lower-tail mean retained by current calibrator diagnostics
empirical mean-to-tail ratio = positive mean_return / abs(negative expected_shortfall)
```

Properties:

- policy fingerprint must be a valid SHA-256 identity;
- unresolved and invalid-labeled current-policy roots prevent `decision_ready=true`;
- display status follows the two-sided interval;
- the one-sided gate status remains separate and is not weakened;
- the ratio is explicitly mean-to-tail, not geometric Plan RR;
- no statistic is shown as live profitability proof.

### 9.3 Heuristic capture score

The old `expected_rr` remains in backend persistence/API for compatibility and internal diagnostics. It is stored as:

```json
{
  "operator_visible": false,
  "basis": "legacy_expected_rr_heuristic_capture_to_volatility_proxy"
}
```

The frontend contains no `expected_rr` reference and no old proxy label.

## 10. RED -> GREEN evidence

New test: `tests/test_iteration249_operator_rr_metrics.py`.

RED command on the pristine production code with only the new regression file added:

```bash
python -m pytest -q tests/test_iteration249_operator_rr_metrics.py
```

RED result: collection failed, exit code 2.

Essential RED line:

```text
ImportError: cannot import name 'return_confidence_interval' from 'app.calibration'
```

The pristine version had neither the independent Plan RR contract nor the two-sided empirical interval helper.

Final GREEN command:

```bash
python -m pytest -q tests/test_iteration249_operator_rr_metrics.py
```

Final GREEN result: `7 passed`.

The seven tests cover:

1. independent Plan RR monetary oracle;
2. boolean/missing input fail-closed behavior;
3. exact-policy temporal-cohort mean, expected shortfall and empirical ratio;
4. two-sided interval display status independent of one-sided gate status;
5. interval symmetry and strict numeric semantics;
6. operator decision API context without legacy proxy;
7. frontend replacement of proxy/rank/confidence fields with Plan RR, empirical expectancy and risk buffer.

## 11. Implementation diff

### Production

- `app/calibration.py`
  - persists weighted temporal mean return;
  - adds strict two-sided Student-t return interval helper;
  - keeps legacy calibrator loading additive and compatible.
- `app/recommender.py`
  - adds `_plan_rr_metrics()` and `_empirical_expectancy_metrics()`;
  - persists full cross-margin stress needed for the plan denominator;
  - stores additive `reasons.operator_metrics` and `params.operator_metrics`;
  - retains legacy `expected_rr` only as backend/internal heuristic data.
- `app/main.py`
  - exposes compact Plan RR and empirical decision context;
  - advances FastAPI version to `1.0.61`.
- `app/db.py`
  - parses additive stored operator metrics for recommendation history;
  - legacy rows remain readable with unavailable values.

### Frontend

- `app/ui/static/index.html`
  - main columns now: symbol, direction, Plan RR, empirical expectancy, risk buffer, status, details;
  - visible confidence threshold removed; a hidden zero-valued compatibility input preserves the existing request contract.
- `app/ui/static/app.js`
  - renders the new metric contracts and fail-closed states;
  - removes all frontend `expected_rr` rendering;
  - removes raw score/confidence fields from primary/history decision surfaces;
  - removes misleading directional TP/SL `Risk/Reward` field from the operator card.
- `app/ui/static/styles.css`
  - adds semantic positive/negative/uncertain/unavailable metric classes with text, not color-only semantics.

### Tests

- Added `tests/test_iteration249_operator_rr_metrics.py`.
- Updated only affected historical static UI/version/cache-key assertions where the intentionally changed operator contract superseded prior expectations.

### Documentation and operator artifacts

Updated:

- `README.md`;
- `CHANGELOG.md`;
- `docs/TRADING_LOGIC.md`;
- `docs/KNOWN_RISKS.md`;
- `docs/ARCHITECTURE.md`;
- `docs/MODULES.md`;
- `docs/SCENARIOS.md`;
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- `docs/instrukciya_operatora_bybit_recommender.docx`;
- `docs/instrukciya_operatora_bybit_recommender.pdf`;
- `how_to_trade.png`;
- this audit report.

The DOCX was rendered and visually checked page by page; the final PDF was independently rendered to 11 PNG pages and visually checked. The root infographic was recreated and inspected at 1600 x 1200.

## 12. Post-check results

| Check | Result |
|---|---|
| `python -m pytest --collect-only -q` | 1126 tests collected in 194 files |
| final exhaustive non-overlapping file batches | 99 + 99 + 97 + 47 + 99 + 96 + 98 + 100 + 95 + 97 + 99 + 100 = 1126; all passed |
| final metric/UI targeted suite | 64 passed in 3.71s |
| iteration 249 targeted | 7 passed |
| PostgreSQL offline translation/locking plus new contract subset | 23 passed |
| fresh SQLite initialization | PASSED: 19 tables |
| repeated SQLite initialization | PASSED: 19 tables, idempotent |
| pristine v1.0.60 SQLite opened by v1.0.61 | PASSED: 19 tables |
| `python -m compileall -q app tests main.py` | PASSED |
| `node --check app/ui/static/app.js` | PASSED |
| frontend `expected_rr` scan | PASSED: no matches |
| private order endpoint scan | PASSED: no matches |
| operator DOCX render | PASSED: 11 pages inspected |
| operator PDF independent render | PASSED: 11 pages inspected |
| `python -m pip check` | FAILED only on pre-existing environment conflict: MoviePy requires Pillow `<12`, environment has 12.2.0 |
| Ruff | UNAVAILABLE: `No module named ruff` |
| monolithic full pytest | TIMED OUT in the harness after substantial progress; not counted as a pass; exhaustive batches are the release evidence |

Some alternative ad-hoc regroupings also exhibited harness stalls despite completed batch logs; they are not counted as release evidence. The deterministic exhaustive batch set above is the canonical final result.

## 13. Database and migration compatibility

- No schema change.
- No SQL migration required.
- `migrations/init.sql` and `migrations/init_postgres.sql` unchanged.
- Existing SQLite/PostgreSQL databases remain compatible.
- New operator metrics are additive JSON fields on newly published recommendations.
- Historical recommendations are not rewritten; they show Plan RR and empirical metrics as unavailable until a new publication contains the new contract.
- Calibrator JSON loading remains backward compatible; missing temporal mean data triggers the disclosed recency-weighted fallback or an unavailable interval.

## 14. API and configuration compatibility

- No route removed or renamed.
- Existing recommendation fields, including `expected_rr`, remain available for compatibility.
- New operator fields are additive.
- Environment variables unchanged.
- Policy fingerprint, model identity, calibrator identity, outcome label and current-policy cohort are unchanged; no evidence reset is triggered.
- Status and recommendation lifecycle semantics unchanged.

## 15. Security and execution boundary

- No private Bybit order create/amend/cancel method added.
- No live keys or production DSN used.
- The application remains recommendation/audit-only and does not submit orders.
- New metrics do not bypass blocked/no-trade/pending/expired gates.
- Empirical evidence does not override deterministic risk gates.

## 16. What the operator now sees

Primary screen:

- symbol;
- canonical direction;
- Plan RR for the current generated plan;
- empirical mean return and status, with uncertainty in details;
- cross-margin risk buffer;
- recommendation lifecycle status;
- details/action access.

Details additionally show:

- Plan projected net reward in USDT;
- kill-switch loss denominator in USDT;
- projected completed pairs and worst side;
- empirical confidence interval;
- expected shortfall and empirical mean-to-tail ratio;
- current-policy samples and non-overlapping temporal clusters;
- relevant per-pair economics and risk diagnostics.

Raw heuristic score/confidence may still exist in backend audit data and gates, but it is not presented as a primary operator decision metric.

## 17. Unverified items and residual risks

- Live two-session PostgreSQL integration: SKIPPED because no explicitly disposable test DSN was supplied.
- Real Bybit fill, queue, spread, slippage and funding reconciliation: NOT VERIFIED by this source-only iteration.
- Live profitability/alpha: NOT ESTABLISHED.
- Plan RR depends on projected fill efficiency and the generated horizon economics; it is a scenario estimate, not a guaranteed payoff ratio.
- Empirical expectancy currently describes the exact current policy under the project's matured proxy-outcome contract. Proxy-to-live execution gap remains material.
- Expected shortfall quality is limited by current-policy sample size, temporal dependence and regime drift.
- Old recommendations do not acquire new metrics retroactively.
- A future product iteration should pre-register operator thresholds only after sufficient frozen-policy evidence; arbitrary thresholds would recreate false precision.

## 18. User actions

### Database

None. Do not delete or rebuild the database.

### Environment

None. No `.env` variable changed.

### Deployment

1. Stop v1.0.60.
2. Deploy v1.0.61 application files.
3. Start with the existing database and environment.
4. Wait for the next recommendation publication; historical rows will legitimately show the new metrics as unavailable.
5. Verify that the primary table shows Plan RR, empirical expectancy and risk buffer, and that no legacy capture/risk proxy is visible.

## 19. Rollback

1. Stop v1.0.61.
2. Restore the v1.0.60 application ZIP/files.
3. Restart with the same database and environment.
4. No DB rollback is required.
5. The old proxy-focused operator UI will return after rollback.

## 20. Recommended next work package

Freeze the current policy and validate the operator metrics against an externally reconciled shadow/live execution sample. Compare:

- projected Plan RR versus realised net reward / realised protective loss;
- proxy empirical expectancy versus exact execution expectancy;
- predicted and realised fill efficiency;
- tail loss and kill-switch slippage;
- stability by symbol, direction and regime.

Only after that evidence exists should operator thresholds or traffic-light rules be calibrated. Until then, the UI should present values and uncertainty without claiming a universal acceptable RR cutoff.
