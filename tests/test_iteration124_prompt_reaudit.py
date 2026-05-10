from __future__ import annotations

import json
import time

from pathlib import Path

from app import db
from app.recommender import _sync_recommendation_metadata, run_recommender_once
from app.settings import Settings
from tests.test_logic import _seed_ohlcv_wave


def _settings_for_prompt_reaudit(symbol: str = "BTCUSDT") -> Settings:
    return Settings(
        outcome_horizon_fallback_sec=6 * 3600,
        calib_min_samples=80,
        db_path=":memory:",
        bybit_base_url="https://api.bybit.com",
        collect_interval_sec=20,
        stale_data_max_sec=3600,
        reco_interval_sec=20,
        top_n=20,
        venues=["linear"],
        symbols_linear=[symbol],
        risk_limits={
            "max_concurrent_bots": 4,
            "max_daily_dd_usdt": 200.0,
            "cooldown_after_loss_min": 30,
            "max_symbol_bots": 1,
            "max_leverage": 3,
            "max_position_notional_usdt": 5000.0,
            "max_margin_per_bot_usdt": 1000.0,
        },
        min_score_to_recommend=-1.0,
        min_conf_to_recommend=0.0,
        taker_fee_bps_linear=6.0,
        master_key=None,
        admin_api_key=None,
        sentiment_interval_sec=60,
        futures_collect_interval_sec=900,
        telegram_token=None,
        telegram_chat_id=None,
        require_conf_gate=False,
    )


def test_sync_recommendation_metadata_keeps_risk_report_decision_in_sync_after_pending_gate() -> None:
    rec = {
        "status": "pending",
        "reasons": {"decision_layers": {"final_status": "recommended"}},
        "params": {"risk_report": {"decision": "recommended", "rejection_reasons": []}},
    }

    _sync_recommendation_metadata(rec)

    assert rec["reasons"]["decision_layers"]["final_status"] == "pending"
    assert rec["params"]["risk_report"]["decision"] == "not_recommended"


def test_recommender_blocks_futures_grid_when_mtf_history_is_insufficient(tmp_path, monkeypatch) -> None:
    conn = db.connect(str(tmp_path / "insufficient_mtf_history.db"))
    db.init_db(conn)
    now = int(time.time())
    monkeypatch.setattr(db, "now_ts", lambda: now)
    symbol = "BTCUSDT"
    base_price = 50_000.0

    # 1m history is enough to compute basic features, but not enough to validate
    # the multi-timeframe direction/regime thesis required for a futures grid.
    _seed_ohlcv_wave(conn, venue="linear", symbol=symbol, now_ts=now, tf_sec=60, n=220, base_price=base_price)
    db.insert_tickers(conn, [{
        "venue": "linear",
        "symbol": symbol,
        "ts": now,
        "last": base_price,
        "bid": base_price - 5.0,
        "ask": base_price + 5.0,
        "vol24h": 12_345.0,
        "turnover24h": 5_000_000.0,
    }])
    db.upsert_funding_rate(conn, [{
        "symbol": symbol,
        "ts": now,
        "funding_rate": 0.00001,
        "next_funding_ts": now + 4 * 3600,
        "funding_interval_min": 480,
    }])

    result = run_recommender_once(conn, _settings_for_prompt_reaudit(symbol))

    assert result["count"] >= 1
    row = conn.execute("SELECT status, reasons_json, params_json FROM recommendations ORDER BY ts DESC LIMIT 1").fetchone()
    assert row is not None
    reasons = json.loads(row["reasons_json"])
    params = json.loads(row["params_json"])
    codes = {block["code"] for block in reasons["risk_checks"]["blocks"]}
    assert row["status"] == "blocked"
    assert "INSUFFICIENT_MTF_HISTORY_FOR_GRID" in codes
    assert params["risk_report"]["decision"] == "not_recommended"
    conn.close()


def test_ui_exposes_conservative_funding_edge_labels() -> None:
    app_js = Path("app/ui/static/app.js").read_text(encoding="utf-8")

    assert "Net/сетка conservative" in app_js
    assert "Funding cost для допуска" in app_js
    assert "Funding benefit исключён" in app_js
    assert "net_profit_with_signed_funding_bps" in app_js


def test_ui_symbol_links_has_single_chart_and_single_grid_bot_link() -> None:
    app_js = Path("app/ui/static/app.js").read_text(encoding="utf-8")

    symbol_links_body = app_js.split("function symbolLinksHtml", 1)[1].split("function statusBadgeHtml", 1)[0]
    assert symbol_links_body.count('title="Открыть график Bybit"') == 1
    assert symbol_links_body.count('title="Открыть страницу создания grid-бота Bybit"') == 1
