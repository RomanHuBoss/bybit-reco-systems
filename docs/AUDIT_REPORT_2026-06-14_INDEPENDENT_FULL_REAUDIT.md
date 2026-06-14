# Independent full trading-system re-audit — 2026-06-14

## Scope

Two archives were unpacked and compared:

- `bybit-reco-systems-audited.zip` — archive proposed for expert use.
- `bybit-reco-systems-main(3).zip` — current GitHub/project archive and the base for the patched output.

The review followed the full requested prompt: Bybit Linear USDT futures semantics, long/short TP/SL, PnL, risk/reward, kill-switch, grid bounds, Bybit V5 fields, risk gates, operator execution lifecycle, UI/API consistency, quant/time-series leakage risks, rounding and edge-case tests.

## Verdict on the expert `audited` archive

**Accept only partially.** The expert archive is useful as a regression-test/documentation addition, but it is not a production patch by itself.

The archive diff showed only these effective changes relative to `main(3)`:

1. Added `docs/AUDIT_REPORT_2026-06-14_FULL_SYSTEM_REAUDIT.md`.
2. Added `tests/test_iteration167_full_trading_system_audit.py`.
3. Appended two low-severity observations to `docs/KNOWN_RISKS.md`.

No production Python/JS trading code differed between `audited` and `main(3)`. Therefore the expert archive should not be accepted as a complete code-fix archive. Its regression test was retained because it provides useful coverage of directional TP/SL, PnL, Bybit order semantics, UI helpers and edge cases. Its broad conclusion that no critical code changes were needed was re-checked independently and narrowed: the existing directional math is generally strong, but one execution-materialization guard was missing.

## Files changed in this patched archive

| File | Change |
|---|---|
| `app/main.py` | Added `_execution_symbol_direction_conflict_blocks()` and invoked it during `_materialize_bot_from_rec()` after current risk gates and before execution preflight/bot insertion. |
| `tests/test_iteration167_full_trading_system_audit.py` | Retained from the expert archive as regression coverage. |
| `tests/test_iteration168_execution_direction_conflict_guard.py` | Added independent tests for same-symbol one-way direction conflict guard. |
| `docs/KNOWN_RISKS.md` | Added corrected residual-risk notes and marked the discovered execution-direction issue as resolved. |
| `docs/STATIC_SCAN_2026-06-14_INDEPENDENT_FULL_REAUDIT.txt` | Stored the static keyword scan result for audit traceability. |
| `docs/AUDIT_REPORT_2026-06-14_INDEPENDENT_FULL_REAUDIT.md` | This report. |

## Findings and fixes

### HIGH — same-symbol incompatible direction could be materialized when symbol bot cap was raised

- **Files**: `app/main.py`, `tests/test_iteration168_execution_direction_conflict_guard.py`
- **Area**: execution lifecycle, risk-management, one-way futures semantics.
- **Problem**: `gate_candidate()` correctly enforced numeric caps such as `max_concurrent_bots` and `max_symbol_bots`, but if an operator deliberately set `max_symbol_bots > 1`, the execution path did not explicitly prove that an already-running bot on the same `(venue, symbol)` had the same direction as the candidate.
- **Why this matters**: this system models Bybit Linear USDT futures grids as one-way/isolated execution unless hedge-mode is added explicitly. Running local long and short/neutral bots on the same symbol would make TP/SL, exposure, reconciliation and operator UI semantics ambiguous.
- **Financial/trading risk**: a later external executor could misinterpret the active directional source of truth, fail to reduce-only close the intended position, or display/protect the wrong side after a flip.
- **Fix**: added `_execution_symbol_direction_conflict_blocks()` and called it inside the serialized `BEGIN IMMEDIATE` execution transaction. The guard:
  - checks only supported `futures_grid + linear` candidates;
  - scans running supported bots on the same venue and symbol;
  - allows idempotent re-attachment to the same publication root;
  - allows same-direction co-existence only when numeric risk caps allow it;
  - blocks different directions, including neutral-vs-directional overlap;
  - blocks unknown existing direction fail-closed.
- **Tests added**:
  - blocks long → short and short → long incompatible materialization;
  - allows same-direction materialization;
  - treats neutral as incompatible with directional one-way execution;
  - skips same publication root for idempotent re-attach;
  - blocks unknown existing direction;
  - ignores other symbols/venues.

### LOW — expert archive is not a production-code patch

- **Files**: archive-level diff.
- **Problem**: `audited` did not modify production trading code. It only added a report, known-risk notes and a regression-test module.
- **Risk**: accepting it as a “fixed system” would give a false sense that expert recommendations introduced runtime hardening.
- **Fix**: retained the useful regression test, independently audited the current code, and added a real execution guard plus report.

### LOW/RESIDUAL — calibration remains advisory/proxy-based

- **File**: `app/calibration.py`
- **Clarification**: the full LogReg + Platt path already uses chronological out-of-fold logits for the Platt-on-top stage. The expert note “no holdout split” is too broad. However, the score-only fallback still fits Platt on available historical proxy outcomes below `logreg_min_samples`.
- **Risk**: confidence may remain over-optimistic in small/non-stationary samples because labels are proxy outcomes, not real execution/fill/funding/liquidation truth.
- **Mitigation**: no code change in this iteration; existing effective-sample/class-balance gates remain. The issue is documented as residual LOW because execution still passes separate risk, shock, freshness, funding, Bybit metadata and preflight gates.

## Directional and TP/SL audit summary

The current code already contains a strong canonical directional layer in `app/trading_semantics.py`:

- `directional_exit_levels()` maps:
  - long: `take_profit = upper`, `stop_loss = lower`;
  - short: `take_profit = lower`, `stop_loss = upper`;
  - neutral: no directional TP/SL.
- `validate_directional_exit_geometry()` fails closed on wrong long/short TP/SL geometry.
- `directional_trade_math()` returns positive gross profit for the correct long/short favorable move and computes risk/reward only when geometry is valid.
- `bybit_linear_order_semantics()` maps opening/closing sides with `reduceOnly` and one-way `positionIdx=0` semantics.
- `bybit_linear_protective_order_plan()` keeps protective orders `reduceOnly`/`closeOnTrigger` and sets trigger direction by TP/SL side.

The frontend also uses direction-aware exit derivation in `app/ui/static/app.js` and validates backend exit payloads before displaying them. No current short TP/SL inversion was found in the audited files.

## Bybit-specific checks

The execution preflight validates Linear USDT metadata fields and filters including `symbol`, `category`, `contractType`, `quoteCoin`, `settleCoin`, `priceFilter.tickSize`, `lotSizeFilter.qtyStep`, `minOrderQty`, `maxOrderQty`, `minNotionalValue`, and `leverageFilter`. It also rejects missing/mismatched metadata fail-closed for strict execution.

Order-side semantics use the intended one-way mapping:

| Position intent | Open side | Close side | Close flags |
|---|---:|---:|---|
| long | `Buy` | `Sell` | `reduceOnly=True`, `closeOnTrigger=True` for protective reduce orders |
| short | `Sell` | `Buy` | `reduceOnly=True`, `closeOnTrigger=True` for protective reduce orders |

## Quant/econometric and time-series checks

The review checked the feature/calibration/recommender path for common leakage and chronological issues:

- feature extraction uses stored `feature_snapshot` where available, reducing train/inference skew;
- LogReg + Platt sorts by timestamp and uses chronological out-of-fold logits for Platt-on-top;
- effective sample and class-balance gates prevent degenerate calibrators;
- rolling/feature code contains explicit dirty-row and finite-number handling;
- no production patch was made here because no immediate look-ahead/data-leakage defect was confirmed in this pass.

Residual risk remains that labels are proxy outcomes rather than true live fills and funding/liquidation truth. This remains documented in `docs/KNOWN_RISKS.md`.

## Static/code-quality scan

A static keyword scan was run over the project for the requested trading-risk tokens:

`tp`, `sl`, `stop`, `take`, `upper`, `lower`, `short`, `long`, `side`, `Buy`, `Sell`, `reduceOnly`, `kill`, `leverage`, `pnl`, `roi`, `risk`, `positionIdx`, `triggerDirection`, `minNotional`, `qtyStep`, `tickSize`, `lookahead`, `future`, `rolling`, `shift`, `sort`, `timestamp`, `partial`, `fill`, `retry`, `order`.

The scan produced **7058** matching lines and is stored at:

`docs/STATIC_SCAN_2026-06-14_INDEPENDENT_FULL_REAUDIT.txt`

The high-risk findings from that scan were followed into the semantic modules, execution preflight, Bybit metadata validation, UI exit-level display, risk gates and bot materialization path. The actionable code defect found from this pass is the same-symbol one-way direction conflict fixed above.

## Tests added

### `tests/test_iteration167_full_trading_system_audit.py`

Retained from the expert archive; covers 63 regression checks, including:

- long/short directional TP/SL geometry;
- PnL signs and risk/reward;
- neutral grid no directional TP;
- Bybit open/close/protective side and flags;
- frontend short TP/SL helper behavior;
- min-notional/tick/qty step edge cases;
- invalid-side fail-closed behavior;
- time-series/calibration helper invariants.

### `tests/test_iteration168_execution_direction_conflict_guard.py`

Added in this independent audit; covers 6 execution guard checks:

- block opposite same-symbol direction;
- allow same direction;
- block neutral-vs-directional overlap;
- skip same publication root for idempotent re-attachment;
- block unknown existing direction fail-closed;
- ignore other symbols/venues.

## Verification results

All available checks passed in the patched archive:

| Check | Result |
|---|---:|
| `python3 -m compileall app tests` | PASS |
| `node --check` for all `.js` files | PASS |
| `pytest -q` | `652 passed in 18.08s` |
| `npm/yarn tests` | Not applicable: no `package.json` present |
| configured lint/type-check | Not found in project configuration |

## Residual risks

1. The project still does not appear to be a complete OMS/EMS with real Bybit private order lifecycle, fills, partial fills, funding and reconciliation as the single exchange truth. It is a strong recommendation/operator-control layer, but an external live executor still must bind to the canonical semantics in `app/trading_semantics.py`.
2. Proxy outcomes/calibration do not replace real execution outcomes.
3. Cross-margin and hedge-mode are not implemented. The new guard intentionally blocks incompatible one-way same-symbol direction states rather than trying to infer hedge semantics.
4. Public Bybit metadata/ticker snapshots are not the same as private account/execution truth. The current execution preflight correctly fails closed when it cannot prove metadata/filters.
5. Manual/legacy payloads can remain stricter after these guards: old rows lacking full trade plans or mode metadata may be blocked rather than silently executed.

## Conclusion

Do **not** accept the expert `audited` archive as-is as a full production fix, because it contains no production-code changes. Do accept its regression-test direction coverage as useful. The patched archive in this output keeps that useful coverage, adds an actual execution guard for one-way same-symbol direction conflicts, documents the independent findings, and passes all available checks.
