# Changelog

## 2026-07-11 - v1.0.12 - strict temporal/funding integer semantics

- Bybit ticker, OHLCV and open-interest timestamps are no longer silently truncated from fractional values into valid integer keys.
- `fundingIntervalHour` and `fundingInterval` now require exact whole-hour/integer-minute semantics; malformed metadata remains unavailable and therefore fail-closed.
- Fractional funding/OI rows can no longer overwrite a valid SQLite/PostgreSQL logical key after coercion.
- Purged calibration rejects fractional recommendation and label-availability timestamps instead of manufacturing chronology through `int()`.
- Fractional label horizons fall back to the canonical 12-hour futures-grid horizon; unknown funding schedules use the conservative possible-event count.
- Funding cashflow accepts only exact integer event counts.
- Baseline: 800 passed. Post-check: 810 passed; 10 new regression items. Ruff remains at the same 9 pre-existing findings, with no new findings.
- No schema, migration, public API, environment variable or operator lifecycle change. Live PostgreSQL integration remained untested because no verified disposable DSN was supplied.

## 2026-06-18 - History/order-label regression audit

- The «История и динамика» table now shows newest publications first while the timeline remains chronological.
- Canonical direction normalization now reaches proxy-outcome return and TP calculations.
- Boolean label horizons no longer mature futures-grid outcomes at six hours.
- Valid zero coherence is preserved in expected R:R instead of being replaced by a neutral default.
- Full regression suite: 767 passed.

## 2026-06-15 - Audit delivery consistency

- Restored release artifact manifest consistency: operator guide DOCX/PDF, operator infographic source, PNG quick-reference, and changelog are shipped with the repository.
- Kept the execution boundary unchanged: this project remains a recommendation/audit service, not OMS/EMS.
- No fail-open changes were introduced.

## Current safety profile

- Bybit Linear USDT futures grid only.
- One running bot per account/symbol by default.
- Shipped actionable leverage interval: min_leverage=3, max_leverage=5.
- Execution preflight remains fail-closed on missing trade plan, stale market data, invalid Bybit metadata, invalid directional TP/SL geometry, and insufficient economic edge.
