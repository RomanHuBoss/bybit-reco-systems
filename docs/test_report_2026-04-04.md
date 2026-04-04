# Test report — 2026-04-04

## Commands executed

```bash
pytest -q
python -m py_compile app/*.py tests/*.py main.py
```

## Results
- `237 passed`
- Python bytecode smoke compile: passed without errors

## Focus of this revision
- hardening against malformed/non-dict sentiment source payloads;
- regression coverage for poisoned Reddit posts and safe degraded `collect_sentiment_once()`;
- актуализация README / audit under the verified 237-test baseline.
