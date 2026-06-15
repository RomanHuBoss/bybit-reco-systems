# Audit report: execution liquidation-boundary re-audit

Date: 2026-06-15  
Scope: Bybit futures / linear USDT recommender, fail-closed execution preflight, canonical directional semantics, UI/backend parity, liquidation-buffer guard, release-test regression.

## Baseline before changes

Required baseline was captured before any source-code change:

| Check | Baseline result |
|---|---:|
| `python -m compileall -q app tests main.py` | pass, exit 0 |
| `node --check app/ui/static/app.js` | pass, exit 0 |
| `pytest -q` | 705 passed / 0 failed |

The repository was already release-test clean at baseline. No baseline failure was used as a reason to weaken a guard.

## Canonical model and audited sources

Read first, before patching:

- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `app/trading_semantics.py`
- latest report: `docs/AUDIT_REPORT_2026-06-15_full_system_regression.md`

System boundary confirmed: this repository is still a recommender/operator layer with fail-closed preflight. It is not a live OMS/EMS and does not own exchange order lifecycle, fills, partial fills, or reconciliation truth.

Canonical directional semantics remain centralized in `app/trading_semantics.py`:

- long: TP above entry/reference, SL below entry/reference;
- short: TP below entry/reference, SL above entry/reference;
- neutral/grid: no single directional TP/SL;
- Bybit one-way linear: open long=`Buy`, close/protect long=`Sell`; open short=`Sell`, close/protect short=`Buy`; protective orders set `reduceOnly=true`, `closeOnTrigger=true`; long TP and short SL trigger on rise; long SL and short TP trigger on fall.

Current Bybit V5 documentation was cross-checked for the relevant API contract: place-order supports linear/perpetual orders, uses `side=Buy/Sell`, `triggerDirection=1` for rising and `2` for falling triggers, and marks `orderFilter` as spot-only; instruments-info exposes `tickSize`, `qtyStep`, `minNotionalValue`, and leverage filters used by the preflight validator.

## Single source-of-truth map

| Area | Files / functions | Status |
|---|---|---|
| Canonical TP/SL / PnL / Bybit side semantics | `app/trading_semantics.py` | OK |
| UI/API payload construction | `app/main.py::_directional_exit_payload_for_reco` | OK |
| Execution preflight directional geometry | `app/main.py::_validate_trade_plan_against_bybit_meta` | OK after patch |
| Bybit public metadata fetch/normalization | `app/bybit_client.py`, `app/main.py::_fetch_bybit_instrument_meta` | OK, still subject to public REST limitations |
| UI operator rendering | `app/ui/static/app.js` | OK; linear directional UI requires backend exit payload and blocks mismatch/missing/invalid payload |
| Grid economics / funding / liquidation estimates | `app/grid_math.py`, `app/recommender.py` | OK after execution-preflight patch; exact liquidation remains external truth |
| Outcome labels / calibration | `app/outcomes.py`, `app/calibration.py` | Proxy-only, residual risk retained |
| Runtime size caps | `app/main.py::_execution_runtime_size_risk_blocks` | OK |
| Documentation / operator artifacts | `docs/`, `README.md`, `how_to_trade.png` | OK |

## Static scan summary

Targeted scan terms: `tp`, `sl`, `take_profit`, `stop_loss`, `kill_switch`, `short`, `long`, `side`, `Buy`, `Sell`, `reduceOnly`, `closeOnTrigger`, `triggerDirection`, `pnl`, `roi`, `risk_reward`, `leverage`, `notional`, `qty_step`, `min_notional`, `upper`, `lower`.

No previous `docs/STATIC_SCAN_*` file was present for diffing. High-risk hits were reviewed manually and mapped to the canonical module, execution preflight, UI parity/fail-closed rendering, grid economics, outcome labels, and tests. No new unsafe TP/SL or Bybit side implementation outside the canonical module was found.

## Finding 1 — HIGH: execution-preflight leverage fallback used reference-price liquidation buffer instead of adverse boundary

- **Severity**: HIGH.
- **Files**:
  - Fixed: `app/main.py` lines 3111-3139.
  - Tests: `tests/test_iteration185_execution_liq_boundary_preflight.py` lines 90-112.
  - Risk note: `docs/KNOWN_RISKS.md` lines 90-92.
- **Why this is an error**: when `leverage > 1` and `params.economics.liquidation_buffer_pct` was missing, execution preflight recomputed the approximate liquidation buffer from `reference_price`. A recommendation can look safe at reference price while already being below the required 12% floor at the adverse grid/kill-switch boundary. Example from the regression: reference=100, leverage=5, range/kill-switch 90/110. The reference buffer is about 19.4%, but the adverse-boundary buffer is below 12% for both long and short cases.
- **Financial/trading risk**: operator could approve a leveraged linear futures grid whose edge boundary is too close to approximate liquidation, especially after a manual/legacy payload omitted generated adverse-boundary economics. This is not a live order placement bug because the repository is not an OMS, but it is a dangerous preflight false negative.
- **Fix**: execution preflight now recomputes liquidation-buffer candidates against the adverse boundary: long uses `kill_switch.lower` or range lower; short uses `kill_switch.upper` or range upper; neutral checks both sides. It then takes the minimum of the recomputed candidate and any supplied `params.economics.liquidation_buffer_pct`. This is strictly more conservative and does not weaken fail-closed behavior.
- **Red → green evidence**:
  - `test_execution_preflight_uses_adverse_boundary_for_liquidation_buffer_fallback` fails on the original code because both long and short payloads pass preflight when only reference-price buffer is considered.
  - `test_execution_preflight_does_not_trust_manual_high_liquidation_buffer_when_boundary_is_tight` fails on the original code because a manually supplied high `liquidation_buffer_pct` could mask a tight boundary.
  - After the patch, all three new assertions pass and preflight emits `LIQUIDATION_BUFFER_TOO_LOW`.

## Other audited areas and results

### Directional TP/SL and PnL

No new defect found. Existing canonical tests cover long/short TP/SL mapping, swapped short geometry rejection, `directional_trade_math()` gross PnL/risk/reward symmetry, protective trigger direction, and neutral-grid non-directional semantics.

### UI/backend parity

No new defect found. UI only renders executable directional TP/SL for linear long/short when backend `directional_exit_levels` payload exists, matches the item direction, and passes geometry validation. Otherwise it renders kill-switch-only blocked state.

### Bybit V5 semantics

No new defect found. The code’s Bybit assumptions match the checked official contract for the audited fields: `side`, positive qty, `triggerDirection`, `triggerPrice`, `reduceOnly`, `closeOnTrigger`, `positionIdx`, instrument `tickSize`, `qtyStep`, `minNotionalValue`, and leverage filters.

### Econometrics / leakage

No new concrete failing leakage case was found in this pass. The residual risk remains: outcome labels are proxy labels, not live fills/funding/liquidation truth; calibration is advisory and remains guarded by risk/shock/funding/metadata/preflight checks.

### Live lifecycle / OMS

No fictitious OMS code was added. Partial fills, real order reconciliation, exchange websocket truth, idempotent live order retries, and exact account liquidation remain requirements for an external execution/reconciliation layer.

## Tests and verification after changes

| Check | Post-change result |
|---|---:|
| `python -m compileall -q app tests main.py` | pass, exit 0 |
| `node --check app/ui/static/app.js` | pass, exit 0 |
| `pytest -q` | 708 passed / 0 failed |
| `python -m ruff check app tests main.py` | not available: `No module named ruff` |
| npm/yarn tests | not available: no `package.json`, `package-lock.json`, or `yarn.lock` |

No previously green test regressed.

## Baseline vs post counts

| Suite | Baseline | Post |
|---|---:|---:|
| pytest | 705 passed / 0 failed | 708 passed / 0 failed |
| compileall | pass | pass |
| node syntax check | pass | pass |

## Files changed

- `app/main.py` — hardened execution-preflight liquidation-buffer fallback to adverse boundary and minimum-of-supplied/recomputed value.
- `tests/test_iteration185_execution_liq_boundary_preflight.py` — added red→green regression tests for long, short, and manually inflated buffer cases.
- `docs/KNOWN_RISKS.md` — documented the hardening and retained exact-liquidation residual risk.
- `docs/AUDIT_REPORT_2026-06-15_execution_liq_boundary_reaudit.md` — this report.

## Residual risks after this pass

Still open and outside this repository’s boundary:

1. No real OMS/EMS and no live exchange truth for orders/fills/reconciliation.
2. Public Bybit REST metadata can be stale or delayed; preflight remains fail-closed where possible but cannot replace exchange/account truth.
3. Exact liquidation price depends on account state, margin mode, risk tier, mark price, fees, and wallet balance; current liquidation helper remains an approximation.
4. Outcome labels remain proxy labels.
5. LLM review remains secondary and must not override deterministic risk/scoring/preflight gates.
6. Telegram alerts are best-effort only.
7. SQLite is practical for single-node operation but not a multi-node production source of truth.
8. `ruff` could not be run in this container because the dependency is not installed.

## Final conclusion

The supplied repository was already green at baseline. The re-audit found one high-severity preflight false-negative in leveraged liquidation-buffer fallback. The patch moves the system in a strictly safer fail-closed direction by measuring the execution-time liquidation buffer against the adverse grid/kill-switch boundary and by not trusting a manually supplied high buffer when recomputation is possible. Full available offline verification is green: `708 passed`, `compileall` passed, and `node --check` passed.
