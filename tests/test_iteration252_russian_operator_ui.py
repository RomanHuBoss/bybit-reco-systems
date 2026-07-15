from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import importlib
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app/ui/static/index.html").read_text(encoding="utf-8")
JS = (ROOT / "app/ui/static/app.js").read_text(encoding="utf-8")


def _extract_js_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    quote: str | None = None
    escape = False
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'", "`"}:
            quote = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


def _run_js(function_names: list[str], expression: str, prelude: str = "") -> object:
    functions = "\n".join(_extract_js_function(JS, name) for name in function_names)
    script = f"""
function escapeHtml(value) {{
  return String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\"/g, '&quot;').replace(/'/g, '&#039;');
}}
{prelude}
{functions}
console.log(JSON.stringify({expression}));
"""
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _visible_html() -> str:
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", HTML, flags=re.S | re.I)
    cleaned = re.sub(r"\s(?:src|href|id|class|data-[\w-]+)=\"[^\"]*\"", "", cleaned)
    return cleaned


def test_static_shell_uses_plain_russian_operator_language() -> None:
    visible = _visible_html()
    for required in [
        "Фьючерсная сетка — Панель оператора", "Ручной режим", "Показывать строк",
        "Можно торговать", "Ожидает проверки", "Заблокировано", "Не торговать",
        "Скрыто системой", "RR плана", "Доходность по наблюдениям",
    ]:
        assert required in visible
    for forbidden in [
        "Futures Grid", ">manual<", "Top N", "recommended+active", ">pending<",
        ">blocked<", "no_trade", ">suppressed<", "Plan RR", "Emp. expectancy",
    ]:
        assert forbidden not in visible, forbidden


def test_direction_and_status_badges_are_understandable_without_trading_english() -> None:
    directions = _run_js(["directionRu"], "[directionRu('long'), directionRu('short'), directionRu('neutral')]")
    assert directions == ["Покупка (рост)", "Продажа (снижение)", "Нейтральная сетка"]
    statuses = _run_js(
        ["operatorStatusRu"],
        "['recommended','active','pending','blocked','no_trade','suppressed','expired','executed','ignored'].map(operatorStatusRu)",
    )
    assert statuses == [
        "Можно торговать", "Можно торговать", "Ожидает проверки", "Заблокировано",
        "Не торговать", "Скрыто системой", "Устарело", "Запущено", "Отклонено оператором",
    ]


def test_exit_geometry_and_cost_labels_are_russian() -> None:
    results = _run_js(
        ["operatorExitLevels"],
        "[operatorExitLevels('long','90','110'), operatorExitLevels('short','90','110'), operatorExitLevels('neutral','90','110')]",
    )
    serialized = json.dumps(results, ensure_ascii=False)
    for required in ["Цель прибыли", "Ограничение убытка", "аварийная граница", "Нейтральная сетка"]:
        assert required.lower() in serialized.lower()
    for forbidden in ["Take Profit", "Stop Loss", "Kill-switch", "Directional", "long:", "short:", "neutral:"]:
        assert forbidden not in serialized


def test_localizer_translates_dynamic_api_values_and_messages() -> None:
    values = _run_js(
        [
            "operatorStatusRu", "healthStatusRu", "llmStatusRu", "gateDecisionRu", "sampleRoleRu",
            "marketStateRu", "timeframeRu", "calibrationModeRu", "sentimentRegimeRu", "humanizeOperatorText",
        ],
        "[operatorStatusRu('no_trade'), healthStatusRu('stale'), llmStatusRu('pending'), gateDecisionRu('pass'), sampleRoleRu('shadow_no_trade'), marketStateRu('risk_on'), timeframeRu('3600'), calibrationModeRu('legacy'), humanizeOperatorText('mean_reversion_score=0.17; funding too high; preflight blocked; ticker payload empty') ]",
    )
    assert values[:8] == [
        "Не торговать", "Устарело", "Ожидает проверки", "Условие выполнено",
        "Учебное наблюдение", "риск допустим", "1 ч", "устаревшая калибровка",
    ]
    translated = values[8].lower()
    for required in ["возврат", "плат", "предзапуск", "биржа не вернула текущую цену"]:
        assert required in translated
    for forbidden in ["mean_reversion", "funding", "preflight", "blocked", "ticker payload empty"]:
        assert forbidden not in translated


def test_visible_outcomes_health_and_details_phrases_are_russian() -> None:
    # Exact user-visible literals that existed before the localization pass.
    for forbidden in [
        'label: "LONG"', 'label: "NEUTRAL"', 'label: "SHORT"',
        '"Proxy-исходы', '"Algo raw', '"Algo exec', '"Neutral class',
        '"Raw direction"', '"Execution direction"', '"Gate decision"',
        '<h3>LLM reviewer</h3>', '>Risk flags<', '"Warm-up / readiness"',
        '"Take Profit', '"Stop Loss', '"Kill-switch', '"TP/SL дистанция"',
        '"Волатильность ATR"', '"Emp. expectancy"', '"Plan RR"',
        '>raw</span>', '>cal</span>', '>legacy</span>',
        'current model lineage=', 'feature-eligible=', 'accepted purged OOF',
        'текущая model lineage=', 'label horizon temporal floor',
        'смена policy fingerprint', 'Это OHLCV proxy-оценка',
    ]:
        assert forbidden not in JS, forbidden

    for required in [
        "Доля успешных", "Средний результат", "Исходное направление алгоритма",
        "Направление после проверок", "Тип нейтрального сигнала", "Проверка LLM",
        "Запас капитала", "Расходы на исполнение", "Платёж финансирования",
        "Предзапусковая проверка", "Текущий набор правил", "учебные наблюдения",
        "Средний диапазон колебаний цены", "Число интервалов сетки",
    ]:
        assert required in JS, required


def test_complex_fields_have_discoverable_plain_russian_help() -> None:
    assert 'aria-label="Подсказка по RR плана"' in HTML
    assert 'aria-label="Подсказка по доходности по наблюдениям"' in HTML
    assert 'tabindex="0"' in HTML
    for explanation in [
        "не является вероятностью прибыли", "не является вероятностью прибыли и не заменяет RR",
        "периодический платёж между участниками", "1 б.п. = 0,01%",
        "Количество независимых завершённых наблюдений",
    ]:
        assert explanation in JS or explanation in HTML


@pytest.fixture()
def app_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "iteration252.db"))
    monkeypatch.setenv("RUNTIME_LOCK_DB_PATH", str(tmp_path / "iteration252_runtime_lock.db"))
    monkeypatch.setenv("SYMBOLS_LINEAR", "BTCUSDT")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    try:
        yield module
    finally:
        sys.modules.pop("app.main", None)


def test_operator_next_action_messages_do_not_leak_trading_english(app_main) -> None:
    rec = {
        "status": "blocked",
        "effective_status": "blocked",
        "direction": "long",
        "params": {
            "leverage": 5,
            "economics": {"liquidation_buffer_pct": 5.5},
            "trade_plan": {"economics": {}},
        },
        "reasons": {},
    }
    actions = app_main._operator_next_actions_for_reco(
        rec,
        ctx={"liquidation_buffer_pct": 5.5},
        guard_errors=[{"code": "LIQUIDATION_BUFFER_TOO_LOW", "msg": "blocked"}],
        guard_warnings=[],
    )
    visible = " ".join(f"{item['title']} {item['detail']}" for item in actions)
    for required in ["общей марж", "плеч", "аварийн", "изолированной позиции"]:
        assert required in visible.lower()
    for forbidden in [
        "cross-margin", "stress", "leverage", "kill-switch", "isolated",
        "liquidation price", "oracle", "fail-closed", "grid",
    ]:
        assert forbidden not in visible.lower(), forbidden
