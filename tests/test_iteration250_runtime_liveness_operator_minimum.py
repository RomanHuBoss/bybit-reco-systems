from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from app import db
from app import main as app_main
from app import outcomes
from app.bybit_client import BybitPublicClient
from app.collector import _fetch_ticker_payloads


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> dict:
        return dict(self._payload)


class _SequenceClient:
    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)

    def get(self, _url: str, params: dict | None = None):
        del params
        if not self._responses:
            raise AssertionError("unexpected extra call")
        return self._responses.pop(0)

    def close(self) -> None:
        return None


class _MissingTickerClient:
    def get_tickers(self, category: str, symbol: str | None = None):
        assert category == "linear"
        return []

    def get_instrument_info(self, category: str, symbol: str):
        assert category == "linear"
        assert symbol == "TONUSDT"
        return None


def _seed_candles(conn, *, symbol: str, base_ts: int, count: int = 370) -> None:
    rows = []
    for idx in range(count):
        ts = base_ts + idx * 60
        px = 100.0 + ((idx % 8) - 4) * 0.18
        close = px + (0.08 if idx % 2 == 0 else -0.08)
        rows.append({
            "venue": "linear",
            "symbol": symbol,
            "tf_sec": 60,
            "ts": ts,
            "open": px,
            "high": max(px, close),
            "low": min(px, close),
            "close": close,
            "volume": 1_000.0,
        })
    db.upsert_ohlcv(conn, rows)


def _recommendation(*, rec_id: str, symbol: str, ts: int, status: str, shadow: bool) -> dict:
    reasons: dict = {
        "feature_snapshot": {
            "mean_reversion_score": 0.40,
            "mean_reversion_evidence_valid": 1,
        },
        "risk_checks": {"passed": True, "blocks": []},
        "decision_layers": {
            "final_status": status,
            "no_trade_reasons": ([{"code": "CALIBRATED_CONFIDENCE_UNAVAILABLE", "msg": "direction probability is not calibrated"}] if status == "no_trade" else []),
        },
        "outcome_policy": {
            "eligible": shadow or status in {"recommended", "active"},
            "policy_evaluation_eligible": True,
            "sample_role": "shadow_no_trade" if shadow else "actionable_root",
        },
    }
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": symbol,
        "bot_type": "futures_grid",
        "direction": "neutral",
        "account_mode": "unified",
        "margin_mode": "cross",
        "score": 0.40,
        "confidence": 0.50,
        "expected_rr": 0.10,
        "risk_score": 0.20,
        "params": {
            "label_horizon_hours": 6,
            "grid_count": 8,
            "grid_levels": 8,
            "grid_spacing_pct": 0.5,
            "price_range_lower": 98.0,
            "price_range_upper": 102.0,
            "cost_model": {"execution_cost_bps": 10.0, "expected_funding_bps": 0.0},
            "trade_plan": {
                "grid_count": 8,
                "levels": {
                    "range": {"lower": 98.0, "upper": 102.0},
                    "kill_switch": {"lower": 97.0, "upper": 103.0},
                    "tp_per_leg": {"abs": 0.5},
                },
            },
        },
        "reasons": reasons,
        "blocks": [],
        "status": status,
        "ttl_sec": 900,
        "model_version": "bybit-taxonomy-v8-policy-conditioned-censor-aware+llm-review-v1",
        "features_ref_ts": ts,
        "publication_root_rec_id": rec_id,
        "is_outcome_label_root": True,
    }


def test_llm_mode_labels_explicit_shadow_without_llm_review_but_keeps_actionable_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = db.connect(str(tmp_path / "shadow-llm-bootstrap.db"))
    try:
        db.init_db(conn)
        base_ts = 1_700_500_000
        _seed_candles(conn, symbol="BTCUSDT", base_ts=base_ts)
        _seed_candles(conn, symbol="ETHUSDT", base_ts=base_ts)
        db.insert_recommendations(conn, [
            _recommendation(rec_id="R-shadow", symbol="BTCUSDT", ts=base_ts, status="no_trade", shadow=True),
            _recommendation(rec_id="R-actionable", symbol="ETHUSDT", ts=base_ts, status="recommended", shadow=False),
        ])
        monkeypatch.setattr(outcomes, "settings", replace(outcomes.settings, llm_reviewer_enabled=True))
        monkeypatch.setattr(db, "now_ts", lambda: base_ts + 24 * 3600)

        processed = outcomes.compute_outcomes_once(conn, max_to_process=10)

        assert processed == 1
        assert db.outcome_exists(conn, "R-shadow") is True
        assert db.outcome_exists(conn, "R-actionable") is False
    finally:
        conn.close()


def test_outcome_liveness_flags_matured_unattempted_shadow_root(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "outcome-liveness.db"))
    try:
        db.init_db(conn)
        base_ts = 1_700_500_000
        db.insert_recommendations(conn, [
            _recommendation(rec_id="R-shadow-stalled", symbol="BTCUSDT", ts=base_ts, status="no_trade", shadow=True),
        ])
        status = db.get_outcome_worker_liveness(
            conn,
            now_ts_value=base_ts + 24 * 3600,
            require_llm_verdict=True,
        )
        assert status["state"] == "stalled"
        assert status["matured_pending_total"] == 1
        assert status["unattempted_total"] == 1
        assert status["code"] == "OUTCOME_WORKER_STALLED"
    finally:
        conn.close()


def test_operator_summary_is_stable_and_contains_one_primary_reason() -> None:
    rec = {
        "rec_id": "R-summary",
        "ts": 1_700_000_000,
        "ttl_sec": 900,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "direction": "long",
        "status": "no_trade",
        "params": {"economics": {"cross_margin_stress_buffer_pct": 92.0}},
        "reasons": {
            "operator_metrics": {
                "plan_rr": {"status": "available", "rr": 0.75},
                "empirical_expectancy": {"status": "insufficient", "return_samples": 0},
            },
            "decision_layers": {
                "no_trade_reasons": [
                    {"code": "PROXY_MONETARY_EXPECTANCY_UNPROVEN", "msg": "empirical expectancy is not proven"},
                    {"code": "CALIBRATED_CONFIDENCE_UNAVAILABLE", "msg": "confidence model is unavailable"},
                ]
            },
        },
        "blocks": [],
    }

    summary = app_main._operator_summary_for_reco(rec, conn=None, guard=None)

    assert summary["decision"] == "do_not_enter"
    assert summary["plan_rr"] == pytest.approx(0.75)
    assert summary["empirical_expectancy_status"] == "insufficient"
    assert summary["primary_reason_code"] == "PROXY_MONETARY_EXPECTANCY_UNPROVEN"
    assert summary["primary_reason"] == "Недостаточно данных об эффективности"
    assert summary["primary_reason_detail"] == "empirical expectancy is not proven"


def test_primary_table_has_only_five_visible_fields_and_keeps_diagnostics_in_details() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "app/ui/static/index.html").read_text(encoding="utf-8")
    js = (root / "app/ui/static/app.js").read_text(encoding="utf-8")
    table_match = re.search(r'<table class="table" id="recoTable">(.*?)</table>', html, re.S)
    assert table_match is not None
    headers = re.findall(r"<th(?:\s[^>]*)?>(.*?)</th>", table_match.group(1), re.S)
    labels = [re.sub(r"<[^>]+>", "", item).strip() for item in headers]
    assert labels == ["Символ", "Направление", "RR плана ?", "Доходность по наблюдениям ?", "Решение"]
    assert "Запас капитала" not in table_match.group(1)
    assert ">Карточка<" not in table_match.group(1)
    assert "function operatorDecisionCell" in js
    assert "primaryDecisionReasonCell" not in js
    assert "НЕ ТОРГОВАТЬ" in js
    assert 'data-act="details"' in js


def test_bybit_10006_waits_until_exchange_reset_window(monkeypatch: pytest.MonkeyPatch) -> None:
    client = BybitPublicClient("https://api.bybit.com", max_retries=1, backoff_base_sec=0.25)
    client._client = _SequenceClient([
        _FakeResponse(
            200,
            {"retCode": 10006, "retMsg": "Too many visits", "result": {"list": []}},
            headers={"X-Bapi-Limit-Reset-Timestamp": "1001500"},
        ),
        _FakeResponse(200, {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT"}]}}),
    ])  # type: ignore[attr-defined]
    sleeps: list[float] = []
    monkeypatch.setattr("app.bybit_client.time.time", lambda: 1000.0)
    monkeypatch.setattr("app.bybit_client.time.sleep", lambda delay: sleeps.append(float(delay)))

    rows = client.get_tickers("linear", "BTCUSDT")

    assert rows == [{"symbol": "BTCUSDT"}]
    assert sleeps
    assert sleeps[0] >= 1.5


def test_confirmed_absent_instrument_is_temporarily_disabled_instead_of_logged_forever(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "missing-symbol.db"))
    try:
        db.init_db(conn)
        disabled: dict[str, int] = {}
        _ticker_rows, _funding_rows, missing = _fetch_ticker_payloads(
            conn,
            _MissingTickerClient(),
            "linear",
            "linear",
            ["TONUSDT"],
            disabled,
            1_700_000_000,
        )
        assert "TONUSDT" in disabled
        assert disabled["TONUSDT"] > 1_700_000_000
        assert missing == []
        row = conn.execute(
            "SELECT action, details_json FROM decision_log ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["action"] == "SYMBOL_DISABLED"
        assert json.loads(row["details_json"])["reason_code"] == "INSTRUMENT_METADATA_ABSENT"
    finally:
        conn.close()
