# Operator documentation and how_to_trade synchronization — 2026-06-14

## Scope

This pass updated operator-facing project documentation and the root infographic `how_to_trade.png` so that the shipped docs no longer contradict the current risk/leverage and fail-closed semantics of the Bybit Linear USDT futures-grid build.

The synchronization covered:

- `how_to_trade.png`;
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`;
- `README.md`;
- `docs/TRADING_LOGIC.md`;
- `docs/KNOWN_RISKS.md`;
- `.env.example`;
- `docs/instrukciya_operatora_bybit_recommender.docx`;
- `docs/instrukciya_operatora_bybit_recommender.pdf`;
- `CHANGELOG.md`;
- historical infographic report supersession note.

## Findings and fixes

| Severity | Area | Problem | Risk | Fix |
|---|---|---|---|---|
| high | `how_to_trade.png`, historical docs | The old infographic still presented 1-3x as the small-account baseline, while the current shipped policy uses `min_leverage=5` and `max_leverage=5`. | Operator could interpret a low-leverage 1-3x idea as current actionable guidance even though the engine now treats sub-minimum leverage ideas as non-actionable or blocked. | Rebuilt `how_to_trade.png` around the current 5x-gated operator profile and added a supersession note to the old 2026-05-09 infographic report. |
| high | `.env.example` | Example risk profile still had `max_leverage=10` while `app/settings.py` defaults to `max_leverage=5`. | Fresh deployments copied from `.env.example` could silently permit a materially more aggressive profile than the shipped runtime default and current documentation. | Changed `.env.example` to `min_leverage=5`, `max_leverage=5` and added comments about stricter caps below 5x. |
| medium | Operator documentation | README and trading logic did not explicitly describe the current relationship between `min_leverage`, stricter `max_leverage < 5` caps and `MIN_LEVERAGE_PER_BOT`. | Ambiguous operator interpretation of low-leverage safety caps. | Added an operator profile section to README and a dedicated leverage/small-account section to `docs/TRADING_LOGIC.md`. |
| medium | Operator instruction DOCX/PDF | The short operator instruction was broadly conservative but did not reflect the current 5x-gated profile, invalid-price fail-closed rule, publication-chain TTL and LLM gate wording. | Manual operator checklist could lag backend semantics. | Rebuilt DOCX and regenerated PDF from the same updated content. |
| low | Infographic maintainability | `how_to_trade.png` had no text source-of-truth, making future sync hard to test or review. | Future visual edits could drift from docs. | Added `docs/HOW_TO_TRADE_INFOGRAPHIC.md` as the editable source text for the PNG. |

## Current operator model captured in docs

- The project is a recommendation/audit service, not a live OMS/EMS.
- Supported product: Bybit Linear USDT Perpetual, `bot_type=futures_grid`, `account_mode=unified`, `margin_mode=isolated`, `grid_type=arithmetic`.
- Current shipped risk profile: one running bot, `min_leverage=5`, `max_leverage=5`.
- `max_leverage < 5` is a stricter safety cap and may leave more ideas `no_trade`/`blocked`; it is not a guarantee that low-leverage ideas become actionable.
- Any blocking/critical preflight, invalid reference price, stale publication-chain, live price outside range/kill-switch, unconfirmed material funding, minNotional/qtyStep/minQty failure, or missing LLM OK verdict when enabled means NO TRADE.
- Long TP/SL and short TP/SL are documented in the same canonical geometry used by the backend and UI.

## Checks performed

| Check | Result |
|---|---:|
| DOCX render to PNGs | PASS — 2 rendered pages inspected |
| PDF render to PNGs | PASS — 2 rendered pages inspected |
| `python3 -m compileall -q app tests` | PASS |
| `node --check app/ui/static/app.js` | PASS |
| Targeted pytest for docs/env synchronization | PASS |
| Full `pytest -q --disable-warnings --maxfail=1` | PASS |
| `npm test` | SKIPPED — no `package.json` in project root |

## Files changed

- `.env.example`
- `CHANGELOG.md`
- `README.md`
- `docs/TRADING_LOGIC.md`
- `docs/KNOWN_RISKS.md`
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`
- `docs/AUDIT_REPORT_2026-05-09_OPERATOR_INFOGRAPHIC_100_500.md`
- `docs/AUDIT_REPORT_2026-06-14_OPERATOR_DOCS_AND_HOW_TO_TRADE_SYNC.md`
- `docs/instrukciya_operatora_bybit_recommender.docx`
- `docs/instrukciya_operatora_bybit_recommender.pdf`
- `how_to_trade.png`
- `tests/test_iteration162_docs_and_infographic_sync.py`

## Residual risks

1. The infographic is still a human-facing quick reference. Runtime truth remains backend preflight, Bybit metadata, current ticker/funding state and any external execution/reconciliation layer.
2. 5x small-account operation has materially higher liquidation sensitivity than 1-3x. The documentation now calls this out, but actual safety depends on live margin, risk tier, mark price and exchange-side state.
3. Historical audit reports remain in the repository for traceability; the old infographic report now has a supersession banner, but raw grep over historical docs will still find old 1-3x text as historical context.
