# Audit report — publication-chain TTL hardening / Bybit futures recommender

Date: 2026-06-11  
Scope: Bybit linear USDT futures recommender, operator UI/API, recommendation lifecycle, risk/execution gate, publication-chain semantics.

## Executive summary

The operator observation was valid: recommendations could remain visually and operationally misleading after their original market idea had expired. The previous patch exposed `publication_chain_age_sec`, but the engine still allowed a fresh child row to keep an old `publication_root_rec_id` alive as `active`. This produced UI states such as `1.4 ч · обновлений: 79 · цепочка истекла 1.1 ч назад`.

This is a high-severity trading-semantics bug. A recommendation chain is a market idea, not a perpetual mutable object. After TTL, the system must not keep improving it or show it as actionable; a new qualifying signal must start a new root-chain.

## Findings and fixes

### 1. Expiry used row age, not root-chain age

- Severity: **high**
- Files:
  - `app/db.py`
- Problem:
  - `expire_stale_recommendations()` expired transient recommendations only by `ts + ttl_sec` of each row.
  - A stale root idea could receive a fresh child row and remain `active`, even when the original publication chain was long expired.
- Trading risk:
  - UI/API could display stale confidence/probability as if it were current.
  - Operator could treat a repeatedly updated historical idea as a fresh trade setup.
  - Backtest/paper/live semantics diverge from real decision time because the recommendation age becomes hidden.
- Fix:
  - Added `recommendation_chain_expiry_context()` and `_recommendation_chain_first_ts()`.
  - `expire_stale_recommendations()` now expires transient rows when either:
    - the row itself exceeds TTL; or
    - the root publication chain exceeds TTL.
  - Expiry log now records `row_expired_count`, `chain_expired_count`, and mode `row_or_publication_chain`.
- Tests:
  - `test_expire_stale_recommendations_expires_fresh_child_when_root_chain_ttl_elapsed`.

### 2. Recommender could keep reusing an expired root-chain

- Severity: **high**
- Files:
  - `app/recommender.py`
- Problem:
  - `_find_recent_publication()` and `_find_open_publication_position()` treated old roots as reusable if they were inside cooldown or outcome-horizon.
  - This meant outcome-horizon could outlive TTL and keep a trading recommendation chain active.
- Trading risk:
  - Same-direction signals could be merged into a stale root instead of becoming a new market idea.
  - `publication_chain_update_count` could grow indefinitely while the root idea was already invalid.
- Fix:
  - Both recent-publication and open-position dedupe now consult chain-expiry context.
  - Expired chains are skipped and cannot be reused for new child publications.
  - Existing tests that intentionally validate outcome-horizon reuse were adjusted so the prior root remains within TTL; TTL now has precedence over horizon.
- Tests:
  - `test_recent_publication_dedupe_does_not_extend_expired_root_chain`.
  - Updated outcome-horizon tests in `tests/test_iteration73.py` and `tests/test_iteration107_execution_and_validation_hardening.py` so they validate live-chain reuse, not expired-chain reuse.

### 3. Execution could attach a fresh child recommendation to a running bot from an expired chain

- Severity: **high**
- Files:
  - `app/main.py`
- Problem:
  - `_materialize_bot_from_rec()` checked row TTL early, but chain TTL was only checked later in preflight.
  - Before that later preflight, the function could reuse an existing running bot from the same `publication_root_rec_id` and mark the fresh child row `executed`.
- Trading risk:
  - An expired child update could be silently treated as executed/idempotent because a running bot existed for the stale root.
- Fix:
  - Added chain freshness blocking before publication-root running-bot reuse.
  - If the row or chain is expired, execution is rejected and the row is marked `expired`.
  - Added explicit `EXECUTION_STALE_RECOMMENDATION_BLOCKED` audit log.
- Tests:
  - `test_materialize_rejects_fresh_child_when_publication_chain_expired_even_with_running_root_bot`.

### 4. UI/API could still treat an expired chain as active before the background sweep

- Severity: **medium/high**
- Files:
  - `app/main.py`
- Problem:
  - Even after exposing `is_publication_chain_expired`, operator list filtering used stored `status` only.
  - A row could remain `active` in UI until a background expiration sweep ran.
- Trading risk:
  - Race condition between recommender/runtime loops and operator UI.
- Fix:
  - Added `_apply_publication_chain_effective_expiry_guard()`.
  - API/UI now expose chain-expired actionable rows as effective `expired` even without waiting for the background sweeper.
  - `/api/v1/recommendations` and `/api/v1/recommendations/{rec_id}` run `expire_stale_recommendations()` before returning operator-facing data.
  - Operator filtering uses `effective_status` when present.
- Tests:
  - `test_operator_filter_excludes_effectively_expired_chain_from_active_list`.

## Directional semantics / TP-SL / risk-management re-check

No new long/short TP/SL inversion was introduced by this patch. The changes are constrained to publication-chain lifecycle and execution gating. Existing regression coverage from the prior audit remains active for:

- long/short TP/SL mapping;
- directional exit payloads;
- UI short TP/SL fallback;
- risk/reward and leverage fail-closed policy;
- Bybit execution preflight;
- Bybit plan validation;
- stale recommendation execution blocking.

The important semantic change in this patch is:

```text
outcome_horizon may keep an outcome label open,
but it may not keep a trading recommendation active after TTL.
```

## Added / modified tests

Added to `tests/test_iteration152_deep_trading_reaudit.py`:

1. `test_expire_stale_recommendations_expires_fresh_child_when_root_chain_ttl_elapsed`
2. `test_recent_publication_dedupe_does_not_extend_expired_root_chain`
3. `test_operator_filter_excludes_effectively_expired_chain_from_active_list`
4. `test_materialize_rejects_fresh_child_when_publication_chain_expired_even_with_running_root_bot`

Updated:

- `tests/test_iteration73.py`
- `tests/test_iteration107_execution_and_validation_hardening.py`

The updated tests preserve coverage for open-position/outcome-horizon dedupe, but only while the root-chain remains within TTL.

## Static/code-quality checks

Executed checks:

```text
python -m compileall -q app tests
PASS

node --check app/ui/static/app.js
PASS

python -m pytest -q
511 passed in 20.32s
```

Static scan performed:

```text
grep -RIn --exclude-dir=.git -E "tp|sl|stop|take|upper|lower|short|long|side|Buy|Sell|reduceOnly|kill|leverage|pnl|roi|risk" app tests
2545 matching lines reviewed/sampled around changed lifecycle paths
```

Checks not executed:

- `npm test` / `yarn test`: no `package.json`, lockfile, or configured JS test runner found.
- lint/type-check tools: no `pyproject.toml`, `ruff.toml`, `mypy.ini`, `setup.cfg`, or ESLint config found.
- Live/testnet Bybit order submission: not executed because no safe API credentials and no explicit exchange sandbox execution scenario are present in the archive.

## Residual risks

- This patch prevents expired recommendation chains from remaining actionable, but it does not independently validate whether the underlying signal model is profitable.
- The existing confidence/probability fields still depend on model calibration quality and available outcome labels.
- Real exchange behavior for partial fills, rejected conditional orders, and network retries still requires testnet/live scenario validation with controlled credentials.
- UI/API now fail closed on stale chain state, but historical rows remain available in the database for audit; operators should not interpret expired rows as trade ideas.

## Operator interpretation after fix

Expected behavior now:

```text
8 мин · обновлений: 6 · цепочка: осталось 7 мин
```

Can remain in active/recommended lists.

```text
1.4 ч · обновлений: 79 · цепочка истекла 1.1 ч назад
```

Must not remain actionable. It will be expired/filtered from active lists, execution is blocked, and a new qualifying signal must start a new `publication_root_rec_id`.
