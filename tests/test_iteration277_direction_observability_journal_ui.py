from __future__ import annotations

import json
import math
import random
import subprocess

import pytest
from pathlib import Path

from app import db
from app.direction import TF_WEIGHTS, aggregate_direction, vote_for_tf
from app.policy import canonical_policy_fingerprint


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "ui" / "static" / "app.js"
STYLES = ROOT / "app" / "ui" / "static" / "styles.css"


def _geometric_ohlc(seed: int = 230, size: int = 120) -> tuple[list[float], list[float], list[float]]:
    rng = random.Random(seed)
    closes = [100.0]
    previous_return = 0.0
    for index in range(size - 1):
        current_return = (
            0.45 * previous_return
            + rng.gauss(0.0, 0.004)
            + 0.0001 * math.sin(index / 7.0)
        )
        closes.append(closes[-1] * math.exp(current_return))
        previous_return = current_return
    highs = [value * math.exp(0.002 + rng.random() * 0.0015) for value in closes]
    lows = [value * math.exp(-(0.002 + rng.random() * 0.0015)) for value in closes]
    return closes, highs, lows


def _log_mirror_ohlc(
    closes: list[float], highs: list[float], lows: list[float]
) -> tuple[list[float], list[float], list[float]]:
    pivot_squared = closes[0] ** 2
    return (
        [pivot_squared / value for value in closes],
        [pivot_squared / value for value in lows],
        [pivot_squared / value for value in highs],
    )


def test_direction_vote_is_antisymmetric_under_log_return_mirror() -> None:
    original = _geometric_ohlc()
    mirrored = _log_mirror_ohlc(*original)

    long_side = vote_for_tf(*original)
    short_side = vote_for_tf(*mirrored)

    assert long_side["indicator_space"] == "log_price_v1"
    assert short_side["indicator_space"] == "log_price_v1"
    assert long_side["score"] == pytest.approx(-short_side["score"], abs=1e-12)
    assert long_side["slope_norm"] == pytest.approx(-short_side["slope_norm"], abs=1e-12)
    assert long_side["contrib"]["ma_slope"] == pytest.approx(
        -short_side["contrib"]["ma_slope"], abs=1e-12
    )
    assert long_side["contrib"]["macd"] == pytest.approx(
        -short_side["contrib"]["macd"], abs=1e-12
    )
    assert long_side["contrib"]["rsi"] == pytest.approx(
        -short_side["contrib"]["rsi"], abs=1e-12
    )
    assert long_side["contrib"]["bollinger"] == pytest.approx(
        -short_side["contrib"]["bollinger"], abs=1e-12
    )

    original_direction = aggregate_direction({tf: dict(long_side) for tf in TF_WEIGHTS})["direction"]
    mirrored_direction = aggregate_direction({tf: dict(short_side) for tf in TF_WEIGHTS})["direction"]
    assert mirrored_direction == {
        "long": "short",
        "short": "long",
        "neutral": "neutral",
    }[original_direction]


def _policy_contract() -> dict:
    return {
        "schema_version": "candidate-policy-v3",
        "selection": {"min_score_to_recommend": 0.14, "mean_reversion_min_score": 0.25},
        "calibration": {"label_due_grace_sec": 120},
    }


def _recommendation(
    *, rec_id: str, symbol: str, ts: int, fingerprint: str, contract: dict, eligible: bool
) -> dict:
    return {
        "rec_id": rec_id,
        "ts": ts,
        "venue": "linear",
        "symbol": symbol,
        "bot_type": "futures_grid",
        "direction": "short",
        "account_mode": "unified",
        "margin_mode": "isolated",
        "score": 0.20 if eligible else 0.10,
        "confidence": 0.55,
        "expected_rr": 0.20,
        "risk_score": 0.10,
        "params": {},
        "reasons": {
            "feature_snapshot": {
                "mean_reversion_evidence_valid": 1,
                "mean_reversion_score": 0.30 if eligible else 0.20,
            },
            "decision_layers": {"no_trade_reasons": []},
            "risk_checks": {"passed": True, "blocks": []},
            "outcome_policy": {
                "eligible": True,
                "policy_evaluation_eligible": eligible,
                "policy_contract": contract,
                "policy_fingerprint": fingerprint,
                "label_due_ts": ts + 3_720,
                "calibration_role": (
                    "current_policy_evaluation" if eligible else "shadow_exploration"
                ),
                "sample_role": "shadow_no_trade",
                "reason": "model_thesis_or_launch_gate",
            },
        },
        "blocks": [],
        "status": "no_trade",
        "ttl_sec": 900,
        "model_version": "model-277",
        "features_ref_ts": ts,
        "publication_root_rec_id": rec_id,
        "is_outcome_label_root": True,
    }


def _insert_outcome(conn, recommendation: dict, ret: float) -> None:
    ts = int(recommendation["ts"])
    db.insert_outcome(
        conn,
        {
            "rec_id": recommendation["rec_id"],
            "ts": ts,
            "venue": "linear",
            "symbol": recommendation["symbol"],
            "bot_type": "futures_grid",
            "direction": "short",
            "horizon_sec": 3_600,
            "label_available_ts": ts + 3_720,
            "entry_close": 100.0,
            "exit_close": 100.0 * (1.0 + ret),
            "ret": ret,
            "success": int(ret > 0.0),
        },
    )


def test_outcome_stats_expose_temporal_dependence_and_do_not_mix_eligibility_cohorts(
    tmp_path: Path, monkeypatch
) -> None:
    base_ts = 1_700_000_000
    monkeypatch.setattr(db, "now_ts", lambda: base_ts + 20_000)
    contract = _policy_contract()
    fingerprint = canonical_policy_fingerprint(contract)
    rows = [
        _recommendation(
            rec_id="R-shadow-a", symbol="BTCUSDT", ts=base_ts,
            fingerprint=fingerprint, contract=contract, eligible=False,
        ),
        _recommendation(
            rec_id="R-shadow-b", symbol="ETHUSDT", ts=base_ts,
            fingerprint=fingerprint, contract=contract, eligible=False,
        ),
        _recommendation(
            rec_id="R-cal-a", symbol="SOLUSDT", ts=base_ts + 1_800,
            fingerprint=fingerprint, contract=contract, eligible=True,
        ),
        _recommendation(
            rec_id="R-cal-b", symbol="XRPUSDT", ts=base_ts + 7_200,
            fingerprint=fingerprint, contract=contract, eligible=True,
        ),
    ]

    conn = db.connect(str(tmp_path / "observability.db"))
    try:
        db.init_db(conn)
        db.insert_recommendations(conn, rows)
        for index, row in enumerate(rows):
            _insert_outcome(conn, row, 0.01 if index % 2 == 0 else -0.01)

        stats = db.get_outcomes_stats(
            conn,
            scope="current_policy",
            current_model_version="model-277",
            policy_fingerprint=fingerprint,
        )
    finally:
        conn.close()

    sample = stats["sample_observability"]
    assert sample == {
        "rows": 4,
        "unique_timestamps": 3,
        "unique_symbols": 4,
        "temporal_clusters": 2,
        "max_non_overlapping_windows": 2,
        "start_span_sec": 7_200,
    }
    grouped = stats["by_bot_cohort"]
    assert {(row["eligibility_cohort"], row["total"]) for row in grouped} == {
        ("calibration_eligible", 2),
        ("shadow_exploration", 2),
    }
    assert all("sample_observability" in row for row in grouped)


def _extract_journal_functions(source: str) -> str:
    start = source.index("function journalActionTone")
    end = source.index("async function loadDecisions", start)
    return source[start:end]


def test_decision_journal_renders_wide_structured_cards_instead_of_a_cramped_json_cell() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")
    block = _extract_journal_functions(source)
    script = f"""
const escapeHtml = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const humanizeOperatorText = value => String(value || '—');
const formatTs = value => `T:${{value}}`;
const botTypeLabel = value => value === 'directional_trend' ? 'Направленный тренд' : 'Фьючерсная сетка';
const renderDirectionBadge = value => `<b>${{escapeHtml(value)}}</b>`;
const pillStatus = value => `<i>${{escapeHtml(value)}}</i>`;
{block}
const html = renderDecisionJournal([{{
  ts: 1700000000,
  action: 'COLLECT_ERROR',
  rec_id: 'R-very-long-audit-identity-1234567890',
  operator: 'system',
  symbol: 'BTCUSDT',
  bot_type: 'directional_trend',
  direction: 'long',
  recommendation_status: 'blocked',
  details: {{ error: '<img src=x onerror=alert(1)>', nested: {{ count: 3, reason: 'stale' }} }}
}}]);
process.stdout.write(JSON.stringify({{html}}));
"""
    completed = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    html = json.loads(completed.stdout)["html"]

    assert "decision-journal-card" in html
    assert "decision-journal-details" in html
    assert "Ошибка сбора данных" in html
    assert "Заблокировано" in html
    assert "<details" in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert '"error":' not in html
    assert "JSON.stringify(row.details" not in source
    assert 'showModalHtml("Журнал решений", html, { wide: true })' in source
    assert ".decision-journal-card" in styles
    assert ".decision-journal-detail-grid" in styles
