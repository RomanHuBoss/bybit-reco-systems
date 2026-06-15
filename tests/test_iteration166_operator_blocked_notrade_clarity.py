from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _app_js() -> str:
    return (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")


def test_blocked_details_show_actual_blocker_before_rank_diagnostics() -> None:
    app_js = _app_js()

    decision_idx = app_js.index('<div class="operator-card operator-decision-card ${decisionClass}">')
    blockers_idx = app_js.index('${blockersHtml}', decision_idx)
    diagnostics_idx = app_js.index('${launchDecisionDiagnosticsHtml(it, scoreMeta)}', decision_idx)

    assert blockers_idx < diagnostics_idx
    assert 'Фактическая причина блокировки / предупреждения' in app_js
    assert 'Фактическая причина показана сразу под этим решением' in app_js


def test_no_launchable_banner_distinguishes_blocked_from_no_trade() -> None:
    app_js = _app_js()

    assert 'НЕТ ЗАПУСКАЕМЫХ: нет эффективных <b>recommended</b>/<b>active</b>' in app_js
    assert '<b>blocked</b> = жёсткий риск/Bybit/preflight-блокер' in app_js
    assert '<b>no_trade</b> = запуск не прошёл launch-score/confidence/economics gates' in app_js
    assert 'NO-TRADE: нет актуальных рекомендаций со статусом' not in app_js


def test_static_asset_cache_key_bumped_after_blocked_notrade_clarity_patch() -> None:
    index = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")

    assert "styles.css?v=manual-ui-v38" in index
    assert "app.js?v=manual-ui-v38" in index
