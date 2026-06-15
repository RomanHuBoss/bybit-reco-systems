# Audit report — 2026-06-15 — 3-5x leverage sync

## Scope

Operator request: make the shipped leverage policy an interval everywhere, including `.env` and documentation.

This patch changes the current shipped profile to:

- `min_leverage=3`
- `max_leverage=5`
- operator-facing wording: `3-5x leverage` / adaptive `3-5x` interval

The change is configuration/documentation synchronization plus test expectation updates. It does not introduce live OMS/EMS behavior and does not weaken execution fail-closed guards: recommendations below the active runtime minimum remain blocked, and `no_trade` rows remain non-executable.

## Changed behavior

| Severity | Area | Problem | Fix | Risk impact |
|---|---|---|---|---|
| medium | Runtime defaults | The previous package still shipped a single-value upper-bound leverage default in `app/risk.py` / `app/settings.py`. | Changed default runtime risk limits to `min_leverage=3`, `max_leverage=5`. | Makes the shipped profile match the requested operator interval while preserving a bounded maximum of 5x. |
| medium | `.env.example` | Example deployment copied the single-value upper-bound leverage profile. | Updated `RISK_LIMITS_JSON` to `"min_leverage":3,"max_leverage":5` and updated adjacent comments. | Fresh deployments now start from the requested 3-5x interval. |
| low | Documentation and operator artifacts | README, TRADING_LOGIC, KNOWN_RISKS, HOW_TO_TRADE source, DOCX/PDF, PNG and historical text references still mentioned the old single-value upper-bound policy. | Reworded active docs and regenerated `how_to_trade.png`, `docs/instrukciya_operatora_bybit_recommender.docx`, and `docs/instrukciya_operatora_bybit_recommender.pdf` around 3-5x leverage. | Operator instructions now align with runtime settings. |
| low | Tests | Tests encoded the old single-value default. | Updated regression tests to assert the 3-5x default interval and adaptive interval note where appropriate. | Prevents future drift back to single-value leverage defaults. |

## Key files changed

- `.env.example`
- `app/risk.py`
- `app/settings.py`
- `app/recommender.py`
- `app/main.py`
- `README.md`
- `CHANGELOG.md`
- `docs/TRADING_LOGIC.md`
- `docs/KNOWN_RISKS.md`
- `docs/HOW_TO_TRADE_INFOGRAPHIC.md`
- `how_to_trade.png`
- `docs/instrukciya_operatora_bybit_recommender.docx`
- `docs/instrukciya_operatora_bybit_recommender.pdf`
- leverage-policy regression tests in `tests/`

## Verification

Commands executed after the patch:

```text
python -m compileall -q app tests main.py
node --check app/ui/static/app.js
pytest -q
```

Result:

```text
684 passed in 20.46s
```

DOCX render QA:

- Rendered `docs/instrukciya_operatora_bybit_recommender.docx` to PNG pages and PDF.
- Inspected rendered pages for visible clipping/overlap after replacing single-value upper-bound wording with 3-5x leverage wording.
- Replaced `docs/instrukciya_operatora_bybit_recommender.pdf` from the verified DOCX render.

## Static leverage wording scan

A post-patch scan was saved to:

- `docs/STATIC_SCAN_2026-06-15_3X_5X_LEVERAGE_SYNC.txt`

The scan intentionally still contains valid mentions of the upper bound `5x`, for example adaptive promotion to `5x` inside the approved `3-5x` interval. The shipped policy and active operator instructions now express the profile as `min_leverage=3`, `max_leverage=5` / `3-5x leverage`.
