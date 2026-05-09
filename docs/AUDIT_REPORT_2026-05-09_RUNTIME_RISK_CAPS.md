# Audit report — runtime Futures Grid Bot risk caps

Date: 2026-05-09

## Scope

Follow-up audit of runtime risk controls for a grid-only Bybit Linear USDT Futures recommender.

## Finding

`normalize_risk_limits()` accepted very large operator values for `max_concurrent_bots` and `max_symbol_bots` and clamped them only at `100000`. This was a dangerous operator-config escape hatch: even though the default project risk profile is strict, a malformed or over-permissive active risk-limit record could make the runtime gate inconsistent with the Futures Grid Bot product cap.

Bybit Futures Grid Bot currently documents a maximum of 50 simultaneously running Futures Grid Bots, so project-level runtime limits must never exceed that cap.

## Fix

- Added explicit constants in `app/risk.py`:
  - `BYBIT_FUTURES_GRID_MAX_CONCURRENT_BOTS = 50`
  - `BYBIT_FUTURES_GRID_MAX_SYMBOL_BOTS = 50`
- `normalize_risk_limits()` now clamps effective `max_concurrent_bots` and `max_symbol_bots` to 50.
- `gate_candidate()` reuses the same constants when applying the final runtime gate.
- README and `.env.example` now state that operator JSON can make limits stricter, but cannot exceed the Futures Grid Bot product cap.
- Added `tests/test_iteration118_grid_bot_risk_caps.py`.

## Verification

```bash
python -m pytest -q tests/test_iteration118_grid_bot_risk_caps.py
# 2 passed

python -m pytest -q
# full suite expected to pass after this patch
```
