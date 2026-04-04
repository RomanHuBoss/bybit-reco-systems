# Test report — 2026-04-04

## Commands executed

```bash
pytest -q
pytest --cov=app --cov-report=term-missing -q
python -m py_compile app/*.py tests/*.py main.py
```

## Results
- `232 passed`
- coverage for `app/*`: `80%`
- Python bytecode smoke compile: passed without errors

## Focus of this revision
- manual `/api/v1/sentiment` integrity hardening;
- failure-path tests for sentiment source adapters;
- release artifact smoke checks for README / docs / `.env.example`.
