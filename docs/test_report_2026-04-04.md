# Test report — 2026-04-04

## Commands executed

```bash
# full pytest baseline executed in grouped invocations that together cover all test files
pytest -q tests/test_api.py tests/test_iteration63.py tests/test_iteration65.py tests/test_iteration66.py tests/test_iteration67.py tests/test_iteration68.py
pytest -q tests/test_iteration69.py tests/test_iteration70.py tests/test_iteration71.py tests/test_iteration72.py tests/test_iteration73.py tests/test_iteration74_runtime_locks.py tests/test_iteration75_runtime_heartbeat.py
pytest -q tests/test_iteration76.py tests/test_iteration77.py tests/test_iteration78.py tests/test_iteration79.py tests/test_iteration80.py tests/test_iteration81_regression.py tests/test_iteration82_publish_atomicity.py
pytest -q tests/test_iteration83_db_write_retries.py tests/test_iteration84_alerts_and_security.py tests/test_iteration85_integrity_and_sanitization.py tests/test_iteration86_atomicity_and_backfill.py tests/test_iteration87_publication_lineage.py tests/test_iteration88_calibration_guardrails.py tests/test_iteration89_env_and_docs_integrity.py
pytest -q tests/test_iteration90_audit_hardening.py tests/test_iteration91_sentiment_release_hardening.py tests/test_iteration92_json_shape_hardening.py tests/test_iteration93_outcomes_and_feature_hardening.py tests/test_iteration94_risk_limits_and_outcome_bounds.py tests/test_sentiment_pipeline.py
pytest -q tests/test_logic.py
python -m py_compile app/*.py tests/*.py main.py
```

## Results
- `247 passed`
- Python bytecode smoke compile: passed without errors

## Focus of this revision
- hardening against malformed/non-dict sentiment source payloads;
- regression coverage for poisoned Reddit posts and safe degraded `collect_sentiment_once()`;
- актуализация README / audit under the verified 247-test baseline.
- shape-hardening для malformed legacy JSON payloads в recommendation/bot/trade/sentiment/status API слоях.
- fail-open hardening outcome-labeling для malformed `grid_spacing_pct/grid_levels` в legacy/manual recommendation params.
- защита feature-layer от логически невозможных OHLCV-баров и poisoned `turnover24h`.
- строгая нормализация blank audit keys для `risk limits version` и явного `trade_id`.
- сохранение и возврат канонических effective risk limits в bootstrap и mutating API.
- fail-open fallback outcome-labeling от poisoned top-level range bounds к валидным `trade_plan.levels.range/kill_switch`.
