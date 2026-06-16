# AUDIT REPORT 2026-06-15 - UI numeric fail-closed re-audit

## Scope

Repository: Bybit Linear USDT futures recommendation and operator-preflight service.

Requested scope: regression audit against the canonical directional model, Bybit Linear USDT semantics, frontend/backend parity, risk-management guardrails, static scan of TP/SL/side/risk logic, red-to-green test, and corrected archive delivery.

System boundary confirmed from `docs/KNOWN_RISKS.md`: this repository is a recommender and fail-closed preflight/operator UI. It is not a live OMS/EMS. Real order lifecycle, fills, partial fills, exact exchange reconciliation, balance truth and exact liquidation truth remain requirements for an external execution/reconciliation layer.

## Documents reviewed first

- `docs/KNOWN_RISKS.md`
- `docs/TRADING_LOGIC.md`
- `docs/ARCHITECTURE.md`
- `docs/MODULES.md`
- `app/trading_semantics.py`
- Latest audit reports present in the supplied archive:
  - `docs/AUDIT_REPORT_2026-06-15_full_system_regression.md`
  - `docs/AUDIT_REPORT_2026-06-15_execution_liq_boundary_reaudit.md`

## Baseline before changes

Commands executed from a fresh extraction of the supplied archive:

```bash
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q
```

Baseline results:

| Check | Result |
|---|---:|
| `python -m compileall -q app tests main.py` | pass, exit 0 |
| `node --check app/ui/static/app.js` | pass, exit 0 |
| `pytest -q` | 708 passed / 0 failed |

The supplied repository was already green at baseline. The new red case was reproduced independently against the original `app/ui/static/app.js` by extracting `toFiniteNumber()` and running it with Node:

```json
{"emptyString":0,"whitespaceString":0,"nullValue":0,"zeroString":0}
```

That proves the new test would fail on the original code because missing/blank frontend values were normalized to numeric zero instead of unknown/null.

## Canonical directional model and Bybit semantics

The canonical long/short/neutral source remains `app/trading_semantics.py`:

- `directional_exit_levels()` maps long TP=upper/SL=lower, short TP=lower/SL=upper, and neutral grids to no single directional TP.
- `validate_directional_exit_geometry()` rejects invalid long/short TP/SL geometry.
- `directional_trade_math()` computes gross directional PnL, reward, risk and risk/reward only after geometry passes.
- `bybit_linear_order_semantics()` and `bybit_linear_protective_order_plan()` centralize one-way linear side/reduce-only/protective trigger semantics.

No new backend bypass of canonical directional math was found. The UI continues to require backend `directional_exit_levels` for linear long/short recommendations and blocks local TP/SL rendering when backend payload is missing, mismatched or geometrically invalid.

## Static scan summary

A targeted scan was run over `app`, `tests`, `README.md` and `.env.example` for TP/SL, side, long/short, kill-switch, leverage, PnL, risk/reward, notional and Bybit-related fields. It was not dumped raw; high-risk hits were mapped to canonical modules:

- `app/trading_semantics.py`: directional TP/SL, PnL and Bybit side/trigger helpers.
- `app/main.py`: execution preflight, Bybit metadata validation, risk checks, liquidation buffer and sizing/notional gates.
- `app/ui/static/app.js`: frontend formatting/parity/fail-closed rendering.
- `app/grid_math.py`: linear PnL, funding cashflow, fees, margin and liquidation approximation.
- `app/outcomes.py` and `app/calibration.py`: proxy outcome/calibration path, already documented as residual risk.

No new unsafe TP/SL inversion or Bybit side mapping was found.

## Finding fixed

### Finding 1 - Blank/null frontend numeric fields were silently converted to zero

- **Severity**: HIGH for operator UI correctness; MEDIUM direct trading risk because backend execution preflight still rejects non-positive required prices and the project is not an OMS.
- **File/range**: `app/ui/static/app.js` lines 66-70.
- **Defect**: JavaScript `Number("")`, `Number("   ")` and `Number(null)` evaluate to `0`. The shared UI helper `toFiniteNumber()` used this conversion directly. Any UI path that used `toFiniteNumber()` before an explicit blank/null guard could treat a missing API/UI field as a real zero.
- **Risk**: Missing prices, sizing values or risk distances could be displayed or used in frontend diagnostics as `0` instead of unknown. In a trading UI this is unsafe because zero is a concrete numeric state, while missing data must remain fail-closed.
- **Fix**: `toFiniteNumber()` now returns `null` for `null`, `undefined`, empty strings and whitespace-only strings before numeric conversion. Literal numeric zero remains accepted when explicitly provided.
- **Safety direction**: stricter/fail-closed only. It does not weaken backend preflight or any trading guard.

## Tests added / changed

- Added `tests/test_iteration186_ui_blank_numeric_failclosed.py`:
  - `test_frontend_to_finite_number_rejects_blank_and_null_inputs()` validates that `""`, whitespace, `null` and `undefined` become `null`, while explicit `0`, `"0"`, and positive numeric strings still parse.
  - `test_static_asset_cache_key_bumped_after_blank_numeric_failclosed_patch()` verifies the static asset cache key bump.
- Updated existing UI cache-key assertions from `manual-ui-v40` to `manual-ui-v41` because `app.js` changed.

Red-to-green proof:

- Original code output: `{"emptyString":0,"whitespaceString":0,"nullValue":0,"zeroString":0}`.
- Patched code output: `{"emptyString":null,"whitespaceString":null,"nullValue":null,"zeroString":0}`.
- New test would fail before the patch and passes after it.

## Verification after changes

| Check | Post-change result |
|---|---:|
| `python -m compileall -q app tests main.py` | pass, exit 0 |
| `node --check app/ui/static/app.js` | pass, exit 0 |
| `pytest -q` | 710 passed / 0 failed |
| `pytest -q tests/test_iteration186_ui_blank_numeric_failclosed.py tests/test_iteration160_frontend_tick_directional_rounding.py tests/test_iteration184_ui_backend_direction_mismatch.py tests/test_iteration157_ui_invalid_exit_failclosed.py` | 8 passed / 0 failed |
| `python -m ruff check app tests main.py` | not available: `No module named ruff` |
| npm/yarn tests | not available: no `package.json` |

No previously green test regressed.

## Baseline vs post counts

| Suite | Baseline | Post |
|---|---:|---:|
| pytest | 708 passed / 0 failed | 710 passed / 0 failed |
| compileall | pass | pass |
| node syntax check | pass | pass |

## Files changed

- `app/ui/static/app.js` - fail-closed blank/null numeric parsing.
- `app/ui/static/index.html` - static cache key bumped to `manual-ui-v41`.
- `tests/test_iteration186_ui_blank_numeric_failclosed.py` - new red-to-green UI numeric parsing regression test.
- Existing UI cache-key tests - expected version updated from `manual-ui-v40` to `manual-ui-v41`.
- `docs/KNOWN_RISKS.md` - documented the resolved UI numeric parsing hardening.
- `docs/AUDIT_REPORT_2026-06-15_ui_numeric_failclosed_reaudit.md` - this report.

## Residual risks

Still outside this repository's boundary or intentionally residual:

1. No live OMS/EMS, no real fill stream and no exchange reconciliation truth.
2. Exact Bybit liquidation depends on live account state, margin mode, risk tier, mark price and wallet balance; local liquidation math remains conservative approximation.
3. Proxy outcome labeling and calibration remain advisory, not live PnL/fill truth.
4. Public market-data/metadata can be stale or unavailable; execution preflight stays fail-closed where possible.
5. LLM review remains a secondary filter and must not override deterministic risk/scoring/preflight gates.
6. Alerts remain best-effort and do not replace external monitoring.

## Final conclusion

The re-audit found and fixed one UI-level fail-closed defect: blank/null numeric values were treated as zero by the shared frontend parser. This could corrupt operator diagnostics even though backend execution preflight still rejects required non-positive prices. The patch is minimal and safer: unknown stays unknown; explicit zero remains explicit zero. Full available offline verification is green: `710 passed`, `compileall` passed, and `node --check` passed.
