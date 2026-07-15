from __future__ import annotations

from pathlib import Path
import json
import re
import subprocess

from app import main as app_main


def _no_trade_rec(code: str, message: str) -> dict:
    return {
        "rec_id": "R-hint",
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "neutral",
        "status": "no_trade",
        "reasons": {
            "decision_layers": {
                "no_trade_reasons": [{"code": code, "msg": message}],
            }
        },
        "blocks": [],
    }


def test_operator_summary_translates_verbose_internal_reason_to_short_operator_hint() -> None:
    verbose = (
        "mean_reversion_score=0.17 < configured candidate floor=0.25; "
        "повторяемая anti-persistence недостаточно выражена, а положительное monetary expectancy "
        "не доказано до отдельной проверки"
    )
    summary = app_main._operator_summary_for_reco(
        _no_trade_rec("MEAN_REVERSION_EDGE_UNCONFIRMED", verbose),
        conn=None,
        guard=None,
    )

    assert summary["primary_reason"] == "Возвратность цены не подтверждена"
    assert summary["primary_reason_detail"] == verbose
    assert len(summary["primary_reason"]) <= 48


def test_unknown_internal_reason_never_leaks_as_long_table_hint() -> None:
    verbose = "x=" + ("technical diagnostic payload; " * 20)
    summary = app_main._operator_summary_for_reco(
        _no_trade_rec("UNMAPPED_INTERNAL_DIAGNOSTIC", verbose),
        conn=None,
        guard=None,
    )

    assert summary["primary_reason"] == "Не пройдены условия запуска"
    assert summary["primary_reason_detail"] == verbose.strip()
    assert "technical diagnostic" not in summary["primary_reason"]


def test_primary_table_uses_decision_tooltip_and_has_no_reason_column() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "app/ui/static/index.html").read_text(encoding="utf-8")
    js = (root / "app/ui/static/app.js").read_text(encoding="utf-8")

    table_match = re.search(r'<table class="table" id="recoTable">(.*?)</table>', html, re.S)
    assert table_match is not None
    headers = re.findall(r"<th(?:\s[^>]*)?>(.*?)</th>", table_match.group(1), re.S)
    labels = [re.sub(r"<[^>]+>", "", item).strip() for item in headers]

    assert labels == ["Символ", "Направление", "RR плана ?", "Доходность по наблюдениям ?", "Решение"]
    assert "primaryDecisionReasonCell" not in js
    assert 'title="${escapeHtml(reason)}"' in js
    assert 'aria-label="${escapeHtml(ariaLabel)}"' in js
    assert "НЕ ТОРГОВАТЬ" in js
    assert '<td data-cell="status">${operatorDecisionCell(it)}</td>' in js


def _extract_js_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


def test_decision_cell_renders_short_hover_hint_from_production_function() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "app/ui/static/app.js").read_text(encoding="utf-8")
    function_source = _extract_js_function(source, "operatorDecisionCell")
    script = f"""
function escapeHtml(value) {{
  return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\"/g, '&quot;').replace(/'/g, '&#039;');
}}
function operatorEffectiveStatus(it) {{ return it.effective_status || it.status || ''; }}
{function_source}
const html = operatorDecisionCell({{
  status: 'no_trade',
  operator_summary: {{
    decision: 'do_not_enter',
    effective_status: 'no_trade',
    primary_reason: 'Возвратность цены не подтверждена'
  }}
}});
console.log(JSON.stringify(html));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    html = json.loads(result.stdout)
    assert ">НЕ ТОРГОВАТЬ<" in html
    assert 'title="Возвратность цены не подтверждена"' in html
    assert 'aria-label="НЕ ТОРГОВАТЬ: Возвратность цены не подтверждена"' in html
    assert "mean_reversion_score" not in html
