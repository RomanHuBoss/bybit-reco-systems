# Audit report — Score UI near-tie segmentation

## Summary

The operator-facing `Скор UI` previously converted raw score rank directly into a percentile among the currently visible candidates. With a small candidate set this created false precision: three practically indistinguishable raw scores such as `0.245 / 0.242 / 0.232` could be rendered as `100 / 50 / 0`.

This iteration changes `Скор UI` from a pure rank percentile into a grouped percentile with near-tie bands. Raw-score differences within `0.025` are treated as not materially distinguishable in the UI. Members of a near-tie group receive the same averaged percentile and grade.

## Changed files

- `app/ui/static/app.js`
  - Added `SCORE_UI_NEAR_TIE_DELTA = 0.025`.
  - Reworked `computeUiScoreMetaMap()` to create near-tie score groups.
  - UI tooltip now exposes group size, raw spread and material threshold.
  - Sorting by `Скор UI` uses grouped UI percentile instead of raw score.
- `app/ui/static/index.html`
  - Updated column tooltip to explain grouped score percentile.
- `tests/test_iteration128_score_ui_segmentation.py`
  - Added regression for `0.245 / 0.242 / 0.232` -> one shared UI segment.
  - Added regression that materially different groups still separate.
  - Added static copy regression.
- `README.md`, `docs/TRADING_LOGIC.md`, `CHANGELOG.md`
  - Documented that `Скор UI` is a visual grouped percentile, not an exact quality/probability ranking.

## Behavioral example

Before:

| raw score | UI score |
|---:|---:|
| 0.245 | 100 |
| 0.242 | 50 |
| 0.232 | 0 |

After:

| raw score | UI score |
|---:|---:|
| 0.245 | 50 |
| 0.242 | 50 |
| 0.232 | 50 |

The candidates are still sortable deterministically, but the visual segment no longer implies that the top candidate is materially superior when the raw scores are inside the configured near-tie delta.

## Tests run

```bash
node --check app/ui/static/app.js
python -m pytest -q tests/test_iteration128_score_ui_segmentation.py tests/test_iteration124_prompt_reaudit.py
python -m py_compile app/*.py tests/*.py main.py
python -m pytest -q tests/test_logic.py tests/test_grid_linear_economics.py tests/test_iteration128_score_ui_segmentation.py
```

Results:

- `7 passed` for the UI near-tie + existing prompt re-audit tests.
- `99 passed` for trading logic, grid linear economics and UI score segmentation tests.
- `node --check` passed.
- `py_compile` passed.

## Residual risk

The material delta `0.025` is an operator-facing UI threshold, not a trading gate. It should be revisited after enough outcome-labelled recommendations exist. Backend approval gates continue to use raw score, confidence, expected RR and risk checks.
