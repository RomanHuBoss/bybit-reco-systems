from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _details_logic_fragment() -> str:
    app_js = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")
    return app_js.split("function buildDetailsHtml", 1)[1].split("function pillStatus", 1)[0]


def test_pending_status_is_not_rendered_as_no_trade_when_risk_report_is_conservative() -> None:
    fragment = _details_logic_fragment()

    assert 'const status = String(it.status || "").trim().toLowerCase()' in fragment
    assert 'const noTradeDecision = status === "no_trade"' in fragment
    assert 'const pendingDecision = status === "pending"' in fragment
    assert 'riskReport.decision === "not_recommended"' not in fragment
    assert 'Рекомендация удержана до завершения LLM-проверки. Это не no_trade' in fragment


def test_pending_details_copy_has_its_own_title_and_does_not_emit_no_trade_reason() -> None:
    fragment = _details_logic_fragment()

    assert 'pendingDecision\n          ? "Ждать LLM-проверку"' in fragment
    no_trade_items_expr = fragment.split('const noTradeReasonItems = ', 1)[1].split('];', 1)[0]
    assert 'noTradeDecision && !explicitHardBlocked' in no_trade_items_expr
    assert 'pendingDecision' not in no_trade_items_expr
