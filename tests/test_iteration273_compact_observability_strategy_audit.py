from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import pytest

from app.direction import TF_WEIGHTS, aggregate_direction, vote_for_tf
from app.outcomes import _signed_return, _signed_settled_funding_pnl
from app.trading_semantics import directional_trade_math


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "ui" / "static" / "app.js"
PROMPT = ROOT / "docs" / "Bybit_Recommender_Iteration_Prompt.md"


def _extract_function(source: str, name: str) -> str:
    start = source.index(f"function {name}")
    next_function = source.index("\n\nfunction ", start + 1)
    return source[start:next_function]


def _ohlc(closes: list[float]) -> tuple[list[float], list[float], list[float]]:
    highs = [value * 1.002 for value in closes]
    lows = [value * 0.998 for value in closes]
    return closes, highs, lows


def test_results_removes_duplicate_direction_aggregations_and_marks_shadow_evidence() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("async function loadOutcomes()")
    end = source.index("async function loadDecisions()", start)
    fn = source[start:end]

    for removed_title in (
        "Результаты по торговым кандидатам",
        "Что хотел алгоритм и во что это превратилось",
        "Нейтральные сигналы нужно рассматривать раздельно",
        "Сырой тезис алгоритма",
    ):
        assert removed_title not in fn
    assert "shadow/no-trade наблюдения" in fn
    assert "Успех по контракту" in fn
    assert "renderModalDisclosure" in fn
    assert fn.count('<div class="modal-section-title">Стратегии</div>') == 1


def test_health_has_one_operator_table_and_collapsed_advanced_diagnostics() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("async function loadHealth()")
    end = source.index("async function loadOutcomes()", start)
    fn = source[start:end]

    assert "Операторский статус" in fn
    assert "Готовность данных и доказательность" in fn
    assert "Расширенная диагностика БД, outcome, runtime и LLM" in fn
    for removed_title in (
        "Сводное заключение",
        "Почему сейчас нет торговых кандидатов",
        "Жёсткие блокировки последней публикации",
        "Накопление данных и готовность",
        "Настройки проверки LLM",
    ):
        assert removed_title not in fn


def test_escape_closes_all_dialogs_in_javascript_runtime() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    fn = _extract_function(source, "closeAllDialogs")
    script = f"""
const closed = [];
const dialogs = [1, 2, 3].map(id => ({{ classList: {{ add: name => closed.push([id, name]) }} }}));
global.document = {{ querySelectorAll: selector => selector === '.modal' ? dialogs : [] }};
{fn}
closeAllDialogs();
process.stdout.write(JSON.stringify(closed));
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert json.loads(completed.stdout) == [[1, "hidden"], [2, "hidden"], [3, "hidden"]]
    assert 'if (e.key === "Escape")' in source
    assert "closeAllDialogs();" in source


def test_directional_price_and_funding_signs_are_mirror_symmetric() -> None:
    assert _signed_return(100.0, 110.0, "long") == pytest.approx(0.10)
    assert _signed_return(100.0, 90.0, "short") == pytest.approx(0.10)
    assert _signed_return(100.0, 90.0, "long") == pytest.approx(-0.10)
    assert _signed_return(100.0, 110.0, "short") == pytest.approx(-0.10)

    long_plan = directional_trade_math("long", 100.0, 118.0, 90.0, qty=2.0)
    short_plan = directional_trade_math("short", 100.0, 82.0, 110.0, qty=2.0)
    assert long_plan is not None and short_plan is not None
    assert long_plan.gross_profit_usdt == pytest.approx(short_plan.gross_profit_usdt)
    assert long_plan.gross_loss_usdt == pytest.approx(short_plan.gross_loss_usdt)
    assert long_plan.risk_reward == pytest.approx(short_plan.risk_reward)

    assert _signed_settled_funding_pnl(1, 100.0, 0.001) == pytest.approx(-0.1)
    assert _signed_settled_funding_pnl(-1, 100.0, 0.001) == pytest.approx(0.1)
    assert _signed_settled_funding_pnl(1, 100.0, -0.001) == pytest.approx(0.1)
    assert _signed_settled_funding_pnl(-1, 100.0, -0.001) == pytest.approx(-0.1)


def test_direction_votes_follow_price_direction_without_long_short_inversion() -> None:
    up = [100.0 * math.exp(0.0015 * index) for index in range(120)]
    down = [100.0 * math.exp(-0.0015 * index) for index in range(120)]
    up_vote = vote_for_tf(*_ohlc(up))
    down_vote = vote_for_tf(*_ohlc(down))

    assert up_vote["score"] > 0.0
    assert down_vote["score"] < 0.0
    assert up_vote["contrib"]["ma_slope"] > 0.0
    assert down_vote["contrib"]["ma_slope"] < 0.0

    up_map = {tf: dict(up_vote) for tf in TF_WEIGHTS}
    down_map = {tf: dict(down_vote) for tf in TF_WEIGHTS}
    assert aggregate_direction(up_map)["direction"] == "long"
    assert aggregate_direction(down_map)["direction"] == "short"


def test_iteration_prompt_requires_compact_ui_and_strategy_sign_audit() -> None:
    text = PROMPT.read_text(encoding="utf-8")
    assert "1600 px" in text
    assert "Escape" in text
    assert "shadow/no_trade" in text
    assert "зеркальная симметрия LONG/SHORT" in text
    assert "kill-switch" in text
