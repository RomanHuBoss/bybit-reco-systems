# Forensic audit report — Bybit Recommender project

## Scope and method

Audit target: original archive unpacked to `reco_proj/`.
Patched result: `reco_proj_fixed/`.

Method:
- full unpack + inventory;
- per-file source review;
- cross-check of README/SPEC/CHANGELOG against code paths;
- contract tracing from collector → DB → features/direction/regime/sentiment → recommender → recommendations → outcomes → calibrators/UI;
- focused review of economic meaning, outcome labels, confidence, fees/funding/slippage, state/risk logic, and train/inference consistency;
- targeted post-fix verification with `python -m compileall` and small synthetic runtime checks.

Important constraint: this is a best-effort static/dynamic code audit, not an exchange-execution replay. Where the code still uses proxies instead of real execution simulation, that is called out explicitly as residual risk.

## 1) Project map by module

### Entry points / orchestration
- `main.py` — thin launcher that delegates to app startup.
- `app/main.py` — FastAPI app, background loops, API wiring, lifecycle.

### Configuration / security / ops
- `.env` — local runtime config; original archive also contained leaked credentials.
- `.env.example` — sample environment.
- `app/settings.py` — env parsing and runtime settings defaults.
- `app/security.py` — admin key verification / key encryption helpers.
- `app/alerts.py` — Telegram alert delivery.

### Persistence layer
- `migrations/init.sql` — SQLite schema.
- `app/db.py` — all DB writes/reads, health queries, stats, pruning, operator actions.

### Market data ingestion
- `app/bybit_client.py` — REST client for tickers / klines / funding / open interest.
- `app/collector.py` — periodic collection into SQLite.

### Feature engineering / market interpretation
- `app/features.py` — ATR, slope, spread, liquidity tier, funding signal, OI trend, BTC beta.
- `app/direction.py` — multi-timeframe direction vote and regime-confidence-like direction metadata.
- `app/regime.py` — aggregate market regime.
- `app/sentiment.py` — external sentiment collection.
- `app/sentiment_features.py` — sentiment aggregation and per-symbol sentiment map.

### Recommendation / scoring / calibration
- `app/recommender.py` — core inference, bot taxonomy routing, cost model, score/confidence, gating, persistence confirmation, publication.
- `app/calibration.py` — feature extraction from stored reasons, LogReg/Platt calibration, persistence of calibrators.
- `app/outcomes.py` — post-fact outcome labeling for recommendation back-checking.
- `app/risk.py` — live gating: concurrent bots, daily DD, cooldown, per-symbol limits.

### UI / docs
- `app/ui/static/index.html` — operator dashboard shell.
- `app/ui/static/app.js` — dashboard logic.
- `app/ui/static/styles.css` — dashboard styling.
- `README.md`, `SPEC.md`, `CHANGELOG.md` — docs; treated as untrusted until validated by code.

### Build artifacts inventoried but not treated as source of truth
- all `__pycache__/*.pyc` files.

## 2) End-to-end data and decision trace

1. `collector.py` fetches tickers, klines, funding, and open interest through `bybit_client.py` and writes them via `db.py`.
2. `recommender.py` loads recent OHLCV/ticker state from SQLite.
3. `features.py` computes local symbol features; `direction.py` computes multi-TF direction; `regime.py` computes market regime; `sentiment_features.py` provides global/per-symbol sentiment.
4. `recommender.py` builds a cost model, score, raw confidence, trade params, trade plan, and status (`recommended` / `blocked` / `no_trade` / `suppressed`).
5. Accepted rows are stored in `recommendations`.
6. `outcomes.py` later labels past recommendations and stores rows in `reco_outcomes`.
7. `calibration.py` reads joined recommendation/outcome rows through `db.get_outcomes_with_recs()` and fits per-bot probability models.
8. UI/API reads the current recommendation snapshot, health, risk state, and outcome stats.

## 3) Findings — from critical to low

---

### F-01 — CRITICAL — funding event horizon used mixed time units
- **File / block:** `app/bybit_client.py:get_funding_rate`, `app/recommender.py:_estimate_cost_model`
- **Problem:** `nextFundingTime` from Bybit arrived in milliseconds, while horizon comparison in `_estimate_cost_model()` was against `ts_now` in seconds.
- **Why this is wrong:** a single funding event should be charged only if it falls inside the projected bot horizon. Milliseconds vs seconds makes that comparison false almost all the time.
- **Practical danger:** linear/futures recommendations underpriced carry cost, inflated score/confidence, and made expensive long funding regimes look artificially cheap.
- **Evidence:** runtime snapshot showed `next_funding_ts=1772899200000` together with `expected_funding_events=0`, which is internally inconsistent for near-term futures recommendations.
- **Fix applied:** normalize Bybit timestamp to seconds in `bybit_client.py`; add defensive normalization for legacy ms payloads inside `_estimate_cost_model()`.
- **Status in patched archive:** **fixed**.

### F-02 — CRITICAL — cross-bot calibration fallback created pseudo-statistical confidence
- **File / block:** `app/recommender.py` confidence path around per-bot/global calibrators; `app/calibration.py`
- **Problem:** original inference fell back from missing bot-specific calibrator to a global calibrator pooled across heterogeneous bot mechanics.
- **Why this is wrong:** grid, DCA, martingale, and combo success labels do not measure the same economic event. Pooling them produces a number that looks like a probability but is not a coherent conditional probability for the active bot.
- **Practical danger:** operator sees a mathematically “calibrated” confidence even when the underlying label distribution belongs to a different strategy family.
- **Evidence:** runtime status snapshot showed global calibrator fitted while per-bot calibrators were absent; that is exactly the regime where the original fallback became misleading.
- **Fix applied:** removed cross-bot/global fallback from live inference; heuristic-only confidence is now capped conservatively; class-balance guards in calibration were tightened.
- **Status in patched archive:** **fixed**.

### F-03 — CRITICAL — outcome success metric and stored return were economically inconsistent
- **File / block:** `app/outcomes.py` (`_grid_success`/`_simulate_dca_long_success`/`_simulate_martingale_success` in original code)
- **Problem:** original code could mark a recommendation as `success=1` using a proxy rule while storing `ret` as simple entry→exit endpoint return, even for strategies whose economics come from path, averaging, or grid turnover rather than endpoint move.
- **Why this is wrong:** calibration and stats later consume both `success` and `ret`. If they describe different economic objects, the system trains on self-contradictory evidence.
- **Practical danger:** impossible summaries such as `win_rate=1.0` with strongly negative `avg_ret`, distorted bot comparisons, and confidence that learns from mislabeled payoff structure.
- **Evidence:** provided outcome stats snapshot contained rows like `futures_grid ... wins=23/23, win_rate=1.0, avg_ret=-0.947` in the original runtime data.
- **Fix applied:** rewrote `app/outcomes.py` so that grid, DCA, and martingale now store a net-of-cost return proxy aligned to the respective simulated mechanics; TP/SL and averaging paths now return `(success, ret_proxy, exit_price)` consistently.
- **Why the fix is still not perfect:** it is still a simulator/proxy, not a full execution replay with order queueing, inventory accounting, partial fills, or liquidation path.
- **Status in patched archive:** **fixed as a consistency bug; residual model risk remains**.

### F-04 — CRITICAL — secret material shipped inside the archive
- **File / block:** original `.env`
- **Problem:** archive contained real-looking Telegram credentials.
- **Why this is wrong:** `.gitignore` does not help once the secret is already inside the shipped bundle.
- **Practical danger:** credential compromise, alert hijack, environment impersonation, and irreversible leakage once the archive leaves the machine.
- **Fix applied:** `.env` in patched archive was scrubbed; archive creation excludes caches and keeps only sanitized config.
- **Status in patched archive:** **fixed**.

### F-05 — CRITICAL — daily drawdown logic was not drawdown
- **File / block:** `app/risk.py:compute_risk_status`
- **Problem:** original daily drawdown behaved like `max(0, -daily_pnl)` instead of realised peak-to-trough drop of cumulative net PnL.
- **Why this is wrong:** drawdown is path-dependent. Ending the day positive does not mean no intraday loss happened.
- **Practical danger:** sequence `+300, -250` would pass DD gate even though the strategy just lost 250 from its intraday equity peak.
- **Fix applied:** compute cumulative net PnL over today’s trades and take peak-to-trough maximum decline; cooldown now also checks actual losing trades instead of relying only on a `LOSS` log action that was not guaranteed to exist.
- **Status in patched archive:** **fixed**.

### F-06 — HIGH — symbol health mixed venues under the same symbol key
- **File / block:** `app/db.py:get_symbol_health`
- **Problem:** original health aggregation keyed disabled/error/stale counters by `symbol` only, so `linear:BTCUSDT` could contaminate `spot:BTCUSDT` and vice versa.
- **Why this is wrong:** throughout the project the entity identity is `(venue, symbol)`, not `symbol` alone.
- **Practical danger:** wrong health page, false stale/error counts, and incorrect operator interpretation of which market is actually broken.
- **Fix applied:** disabled, error, and stale counters are now tracked venue-aware as `(venue, symbol)` tuples.
- **Status in patched archive:** **fixed**.

### F-07 — HIGH — disabled futures symbols were not latched consistently in ancillary collectors
- **File / block:** `app/collector.py:collect_futures_once`
- **Problem:** original ancillary futures collection (funding/OI) logged errors but did not always turn repeated “unsupported symbol” responses into a durable disabled state.
- **Why this is wrong:** the main collector already had a disabled-symbol concept. Leaving side-channel endpoints out of that contract keeps retrying known-bad symbols.
- **Practical danger:** noisy logs, useless API pressure, and operator confusion because some collectors keep hammering symbols that should have been quarantined.
- **Fix applied:** `collect_futures_once()` now recognizes unsupported-symbol errors, disables the symbol, logs `SYMBOL_DISABLED`, and skips further processing.
- **Status in patched archive:** **fixed**.

### F-08 — HIGH — calibration could activate on thin or nearly one-class samples
- **File / block:** `app/calibration.py`, `app/settings.py`, `.env`, `.env.example`
- **Problem:** original minimum sample threshold and class-balance guard were too permissive for noisy, proxy-labeled crypto outcomes.
- **Why this is wrong:** very small or nearly homogeneous samples produce extreme intercepts and fake certainty.
- **Practical danger:** overconfident probabilities on unstable data slices; apparent statistical precision without real support.
- **Fix applied:** stricter 15/85 class-balance guard retained; default `CALIB_MIN_SAMPLES` raised to 80 in code and env samples; heuristic-only confidence remains capped.
- **Status in patched archive:** **fixed/mitigated**.

### F-09 — HIGH — documentation and shipped code diverged on confidence/funding honesty
- **File / block:** `README.md`, `SPEC.md`, `CHANGELOG.md` versus actual runtime code
- **Problem:** original docs already claimed event-aware funding and “honest” confidence while the shipped inference path still had the funding-unit bug and cross-bot calibration fallback.
- **Why this is wrong:** docs were materially ahead of code.
- **Practical danger:** reviewers trust guarantees that were not actually true in the delivered artifact.
- **Fix applied:** code was brought closer to the documented behavior; defaults in docs were updated where changed.
- **Why partially fixed only:** the archive still has narrative/history docs, and changelog prose should never be treated as executable truth.
- **Status in patched archive:** **partially fixed; process risk remains**.

### F-10 — HIGH — `futures_combo` still has no true two-leg execution/PnL model
- **File / block:** `app/recommender.py`, `app/outcomes.py`
- **Problem:** combo/hedge bot is scored with a proxy and lacks full two-leg realised economics.
- **Why this is wrong:** without real pair-leg accounting, carry, basis, and hedge leg fills, a published confidence would be unjustified.
- **Practical danger:** false sense that combo is calibrated like the other bots.
- **Fix applied:** patched code keeps combo confidence heuristic and suppresses publication to research-only mode.
- **Status in patched archive:** **mitigated, not solved**.

### F-11 — MEDIUM — grid/DCA/martingale outcomes are still path approximations, not true execution replay
- **File / block:** `app/outcomes.py`
- **Problem:** even after the rewrite, the system estimates economic outcome from candle path proxies rather than simulating real order placement, inventory turnover, maker/taker mix, partial fills, liquidation, and queue priority.
- **Why this matters:** the label is now internally more consistent, but still not exchange-faithful.
- **Practical danger:** calibration can still learn from proxy error, especially in tail conditions and on volatile small caps.
- **Fix applied:** none beyond consistency rewrite.
- **Status in patched archive:** **residual risk**.

### F-12 — MEDIUM — cost model is still heuristic, not size-aware execution modeling
- **File / block:** `app/recommender.py:_estimate_cost_model`
- **Problem:** spread/slippage/funding are approximated heuristically and do not incorporate order size, maker-vs-taker execution path, depth, liquidation distance, or multi-fill inventory turnover.
- **Why this matters:** economic filtering is directionally better than before, but still approximate.
- **Practical danger:** expensive trades can still look better than they truly are, especially for illiquid symbols or martingale ladders.
- **Fix applied:** funding horizon bug fixed; no full execution model added.
- **Status in patched archive:** **residual risk**.

### F-13 — MEDIUM — no formal invariant tests for contracts between modules
- **File / block:** project-wide
- **Problem:** there is no automated test suite locking down schema contracts, unit consistency, calibration feature order, or outcome invariants.
- **Why this matters:** this codebase depends on many implicit contracts (`reasons_json`, `params_json`, `feature_snapshot`, `(venue,symbol)` identity, cost model keys, bot-specific label semantics).
- **Practical danger:** future edits can silently reintroduce train/inference skew or schema mismatch without obvious runtime errors.
- **Fix applied:** none in this patch set.
- **Status in patched archive:** **technical debt**.

### F-14 — LOW — shipped archive contained compiled caches
- **File / block:** `__pycache__/*.pyc`
- **Problem:** compiled artifacts were present in the bundle.
- **Why this matters:** not a logic bug, but it increases noise during forensic review and can mislead about what exact source version was executed.
- **Fix applied:** patched release archive excludes `__pycache__` and `*.pyc`.
- **Status in patched archive:** **fixed**.

## 4) What was changed in code

Patched files:
- `app/bybit_client.py`
- `app/calibration.py`
- `app/collector.py`
- `app/db.py`
- `app/outcomes.py`
- `app/recommender.py`
- `app/risk.py`
- `app/settings.py`
- `.env`
- `.env.example`
- `README.md`
- `CHANGELOG.md`

Summary of edits:
- normalized funding timestamps to seconds;
- added legacy-ms defensive handling in cost model;
- removed cross-bot/global confidence fallback for live inference;
- capped heuristic-only confidence and hardened calibrator activation;
- rewrote outcome labeling to align `success` and `ret` with bot mechanics;
- replaced fake daily DD with realised peak-to-trough DD;
- made cooldown depend on actual losing trades;
- made symbol health venue-aware for disabled/error/stale counts;
- latched unsupported futures side-channel symbols as disabled;
- sanitized `.env` and raised default calibration minimum samples to 80;
- removed caches from release packaging.

## 5) Re-check after patching

Post-patch checks performed:
- `python -m compileall -q .` on `reco_proj_fixed/` — passed.
- synthetic check: funding ms timestamp now yields non-zero `expected_funding_events` when horizon crosses funding.
- synthetic check: daily DD now returns `250` for `+300, -250` sequence while day PnL remains `+50`.
- synthetic check: `get_symbol_health()` now separates `spot:BTCUSDT` and `linear:BTCUSDT` error/stale counters.
- synthetic check: rewritten grid outcome returns positive proxy for bounded oscillation and negative proxy for range breach/trend drift.

## 6) Verdict

### Verdict on the original archive

**Not acceptable as a trustworthy trading recommendation system.**

Reasons:
- cost model undercounted funding on linear products;
- confidence could look statistically calibrated while actually borrowing labels from the wrong bot family;
- outcome labels and stored returns were self-contradictory for key bot types;
- drawdown gating could miss real intraday loss;
- secrets were shipped inside the archive.

### Verdict on the patched archive

**Usable as a research/operator support tool with guardrails, but still not strong enough for blind automated trading execution.**

What improved materially:
- major unit/contract bugs were removed;
- confidence is less misleading;
- outcome storage is internally coherent enough for much saner monitoring/calibration;
- risk and health paths are closer to operational reality.

What still prevents “production-grade autonomous trading” classification:
- no full execution replay for grid/DCA/martingale;
- combo remains research-only;
- cost/slippage are still heuristics;
- no test harness enforcing invariants.

## 7) Technical debt that remains

- true exchange-faithful outcome replay per bot;
- full two-leg combo economics;
- order-size/depth-aware slippage and maker/taker path modeling;
- formal tests for feature contracts, schema keys, and calibration invariants;
- regression fixtures using captured market snapshots;
- better documentation discipline so README/CHANGELOG cannot outrun shipped code.

## 8) Residual risks

1. Proxy outcomes can still bias calibrators in rare path-dependent scenarios.
2. Heuristic cost model can still under/overestimate real execution costs on thin books.
3. Martingale remains structurally tail-risky even if labeling is more coherent.
4. DCA remains long-only; this is economically asymmetric by design.
5. Sentiment and BTC-beta features can still inject correlated market-wide information rather than idiosyncratic edge.
6. SQLite with multiple writers is acceptable at this scale but still operationally fragile compared with a proper task queue / DB separation.

## 9) Raw inventory of inventoried files

- `.env`
- `.env.example`
- `.gitignore`
- `CHANGELOG.md`
- `README.md`
- `SPEC.md`
- `__pycache__/main.cpython-313.pyc`
- `app/__init__.py`
- `app/__pycache__/__init__.cpython-312.pyc`
- `app/__pycache__/__init__.cpython-313.pyc`
- `app/__pycache__/alerts.cpython-312.pyc`
- `app/__pycache__/alerts.cpython-313.pyc`
- `app/__pycache__/bybit_client.cpython-312.pyc`
- `app/__pycache__/bybit_client.cpython-313.pyc`
- `app/__pycache__/calibration.cpython-312.pyc`
- `app/__pycache__/calibration.cpython-313.pyc`
- `app/__pycache__/collector.cpython-312.pyc`
- `app/__pycache__/collector.cpython-313.pyc`
- `app/__pycache__/db.cpython-312.pyc`
- `app/__pycache__/db.cpython-313.pyc`
- `app/__pycache__/direction.cpython-312.pyc`
- `app/__pycache__/direction.cpython-313.pyc`
- `app/__pycache__/features.cpython-312.pyc`
- `app/__pycache__/features.cpython-313.pyc`
- `app/__pycache__/main.cpython-312.pyc`
- `app/__pycache__/main.cpython-313.pyc`
- `app/__pycache__/outcomes.cpython-312.pyc`
- `app/__pycache__/outcomes.cpython-313.pyc`
- `app/__pycache__/recommender.cpython-312.pyc`
- `app/__pycache__/recommender.cpython-313.pyc`
- `app/__pycache__/regime.cpython-312.pyc`
- `app/__pycache__/regime.cpython-313.pyc`
- `app/__pycache__/risk.cpython-312.pyc`
- `app/__pycache__/risk.cpython-313.pyc`
- `app/__pycache__/security.cpython-312.pyc`
- `app/__pycache__/security.cpython-313.pyc`
- `app/__pycache__/sentiment.cpython-312.pyc`
- `app/__pycache__/sentiment.cpython-313.pyc`
- `app/__pycache__/sentiment_features.cpython-312.pyc`
- `app/__pycache__/sentiment_features.cpython-313.pyc`
- `app/__pycache__/settings.cpython-312.pyc`
- `app/__pycache__/settings.cpython-313.pyc`
- `app/alerts.py`
- `app/bybit_client.py`
- `app/calibration.py`
- `app/collector.py`
- `app/db.py`
- `app/direction.py`
- `app/features.py`
- `app/main.py`
- `app/outcomes.py`
- `app/recommender.py`
- `app/regime.py`
- `app/risk.py`
- `app/security.py`
- `app/sentiment.py`
- `app/sentiment_features.py`
- `app/settings.py`
- `app/ui/static/app.js`
- `app/ui/static/index.html`
- `app/ui/static/styles.css`
- `main.py`
- `migrations/init.sql`
- `requirements.txt`
