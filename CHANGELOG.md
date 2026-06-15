# Changelog

## 2026-06-15 - Audit delivery consistency

- Restored release artifact manifest consistency: operator guide DOCX/PDF, operator infographic source, PNG quick-reference, and changelog are shipped with the repository.
- Kept the execution boundary unchanged: this project remains a recommendation/audit service, not OMS/EMS.
- No fail-open changes were introduced.

## Current safety profile

- Bybit Linear USDT futures grid only.
- One running bot per account/symbol by default.
- Shipped actionable leverage interval: min_leverage=3, max_leverage=5.
- Execution preflight remains fail-closed on missing trade plan, stale market data, invalid Bybit metadata, invalid directional TP/SL geometry, and insufficient economic edge.
