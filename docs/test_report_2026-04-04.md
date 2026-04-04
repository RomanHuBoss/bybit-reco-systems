# Test report — 2026-04-04

## Commands executed

```bash
pytest -q
python -m py_compile app/*.py tests/*.py main.py
```

## Results
- `244 passed`
- Python bytecode smoke compile: passed without errors

## Focus of this revision
- hardening against malformed/non-dict sentiment source payloads;
- regression coverage for poisoned Reddit posts and safe degraded `collect_sentiment_once()`;
- актуализация README / audit under the verified 244-test baseline.
- shape-hardening для malformed legacy JSON payloads в recommendation/bot/trade/sentiment/status API слоях.
- fail-open hardening outcome-labeling для malformed `grid_spacing_pct/grid_levels` в legacy/manual recommendation params.
- защита feature-layer от логически невозможных OHLCV-баров и poisoned `turnover24h`.
- строгая нормализация blank audit keys для `risk limits version` и явного `trade_id`.
