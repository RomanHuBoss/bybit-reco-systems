from __future__ import annotations

import math
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest

from app import db, outcomes
from app import recommender as recommender_module
from app.calibration import FEATURE_NAMES
from app.strategy_router import evaluate_candidate
from app.trend_events import (
    TREND_EVENT_TYPES,
    TrendEventModel,
    build_trend_event_assessment,
)


def _seed_candles(conn, ts0: int, candles: list[tuple[float, float, float, float]]) -> None:
    db.upsert_ohlcv(conn, [
        {
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": ts0 + index * 60,
            "open": open_px,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000.0,
        }
        for index, (open_px, high, low, close) in enumerate(candles)
    ])


def _params() -> dict:
    return {
        "strategy_family": "directional_trend",
        "entry_model": "single_position_no_pyramiding",
        "label_horizon_hours": 12,
        "cost_model": {
            "execution_cost_bps": 10.0,
            "expected_funding_events": 0,
            "expected_funding_bps": 0.0,
        },
        "trade_plan": {
            "reference_price": 100.0,
            "entry_model": "single_position_no_pyramiding",
            "levels": {
                "take_profit": {"price": 104.0},
                "stop_loss": {"price": 98.0},
            },
        },
    }


def _run_outcome(tmp_path: Path, candles, *, exitp: float = 100.0):
    conn = db.connect(str(tmp_path / "event.db"))
    db.init_db(conn)
    ts0 = 1_700_000_000
    _seed_candles(conn, ts0, candles)
    diagnostics: dict[str, object] = {}
    result = outcomes._directional_trend_outcome(
        conn,
        "linear",
        "BTCUSDT",
        100.0,
        exitp,
        ts0,
        ts0 + len(candles) * 60,
        "long",
        _params(),
        diagnostics=diagnostics,
    )
    conn.close()
    return result, diagnostics


def test_trend_outcome_persists_explicit_first_touch_event_types(tmp_path: Path) -> None:
    tp_result, tp_diag = _run_outcome(
        tmp_path,
        [(100.0, 101.0, 99.0, 100.5), (100.5, 104.2, 100.0, 103.9)],
        exitp=103.9,
    )
    assert tp_result is not None
    assert tp_diag["event_type"] == "TP_FIRST"

    sl_result, sl_diag = _run_outcome(
        tmp_path,
        [(100.0, 101.0, 99.0, 99.5), (99.5, 100.0, 97.8, 98.1)],
        exitp=98.1,
    )
    assert sl_result is not None
    assert sl_diag["event_type"] == "SL_FIRST"

    horizon_result, horizon_diag = _run_outcome(
        tmp_path,
        [(100.0, 101.0, 99.0, 100.4), (100.4, 101.5, 99.5, 101.0)],
        exitp=101.0,
    )
    assert horizon_result is not None
    assert horizon_diag["event_type"] == "HORIZON_EXIT"


def test_same_minute_tp_and_sl_is_explicit_ambiguous_censoring(tmp_path: Path) -> None:
    result, diagnostics = _run_outcome(
        tmp_path,
        [(100.0, 104.5, 97.5, 101.0)],
        exitp=101.0,
    )
    assert result is None
    assert diagnostics["event_type"] == "AMBIGUOUS"
    assert diagnostics["reason"] == "directional_tp_sl_intrabar_order_unobservable"


def test_reco_outcomes_schema_and_join_expose_event_type(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "schema.db"))
    db.init_db(conn)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(reco_outcomes)").fetchall()}
    assert "event_type" in cols
    conn.close()


def test_trend_event_model_emits_normalized_three_way_probabilities() -> None:
    model = TrendEventModel(
        fitted=True,
        classes=TREND_EVENT_TYPES,
        coef=[[0.0] * len(FEATURE_NAMES) for _ in TREND_EVENT_TYPES],
        intercept=[math.log(0.60), math.log(0.20), math.log(0.20)],
        n_samples=300,
        holdout_status="accepted",
        holdout_log_loss=0.70,
        holdout_null_log_loss=1.05,
        probability_error_bound=0.03,
        horizon_exit_mean_return=0.005,
        horizon_exit_return_lower_bound=0.0,
        policy_fingerprint="a" * 64,
    )
    probs = model.predict_proba([0.0] * len(FEATURE_NAMES))
    assert set(probs) == set(TREND_EVENT_TYPES)
    assert sum(probs.values()) == pytest.approx(1.0)
    assert probs["TP_FIRST"] == pytest.approx(0.60)


def _trend_candidate() -> dict:
    return {
        "rec_id": "R-trend",
        "bot_type": "directional_trend",
        "direction": "long",
        "status": "recommended",
        "confidence": 0.75,
        "params": _params(),
        "reasons": {
            "outcome_policy": {
                "comparison_return_basis": "unlevered_net_return_on_committed_notional_v1",
            },
            "confidence_model": {
                "source": "bot_logreg",
                "fitted": True,
                "policy_fingerprint": "a" * 64,
                "selected_policy_expectancy_status": "positive",
                "terminal_selected_policy_expectancy_status": "positive",
                "selected_policy_confidence_threshold": 0.60,
                "selected_policy_weighted_mean_return": 0.005,
                "terminal_selected_policy_weighted_mean_return": 0.004,
                "selected_policy_weighted_mean_return_lower_bound": 0.002,
                "selected_policy_weighted_temporal_mean_return_lower_bound": 0.0018,
                "terminal_selected_policy_weighted_mean_return_lower_bound": 0.0015,
                "terminal_selected_policy_weighted_temporal_mean_return_lower_bound": 0.0013,
                "selected_policy_weighted_expected_shortfall": -0.01,
            },
            "operator_metrics": {
                "empirical_expectancy": {
                    "decision_ready": True,
                    "gate_status": "positive",
                },
            },
        },
    }


def test_trend_event_assessment_prices_tp_sl_and_timeout_separately() -> None:
    rec = _trend_candidate()
    model = TrendEventModel(
        fitted=True,
        classes=TREND_EVENT_TYPES,
        coef=[[0.0] * len(FEATURE_NAMES) for _ in TREND_EVENT_TYPES],
        intercept=[math.log(0.60), math.log(0.20), math.log(0.20)],
        n_samples=300,
        holdout_status="accepted",
        holdout_log_loss=0.70,
        holdout_null_log_loss=1.05,
        probability_error_bound=0.03,
        horizon_exit_mean_return=0.005,
        horizon_exit_return_lower_bound=0.0,
        policy_fingerprint="a" * 64,
    )
    assessment = build_trend_event_assessment(rec, [0.0] * len(FEATURE_NAMES), model)
    assert assessment["ready"] is True
    assert assessment["tp_first_probability"] == pytest.approx(0.60)
    assert assessment["sl_first_probability"] == pytest.approx(0.20)
    assert assessment["event_expected_net_return"] > 0.0
    assert assessment["event_expected_net_return_lower_bound"] > 0.0
    assert assessment["tp_first_probability_lower_bound"] > assessment["sl_first_probability_upper_bound"]


def test_router_rejects_trend_without_first_touch_model() -> None:
    evaluation = evaluate_candidate(_trend_candidate())
    assert evaluation["eligible"] is False
    assert "TREND_FIRST_TOUCH_MODEL_REQUIRED" in evaluation["reason_codes"]


def test_router_uses_first_touch_ev_not_binary_hit_rate() -> None:
    rec = _trend_candidate()
    rec["reasons"]["trend_event_model"] = {
        "ready": True,
        "source": "trend_event_softmax",
        "model_version": "trend-first-touch-softmax-v2",
        "outcome_label_version": "directional_trend_label_v2",
        "policy_fingerprint": "a" * 64,
        "tp_first_probability": 0.30,
        "sl_first_probability": 0.55,
        "horizon_exit_probability": 0.15,
        "tp_first_probability_lower_bound": 0.25,
        "sl_first_probability_upper_bound": 0.60,
        "event_expected_net_return": -0.003,
        "event_expected_net_return_lower_bound": -0.006,
    }
    evaluation = evaluate_candidate(rec)
    assert evaluation["eligible"] is False
    assert "TREND_FIRST_TOUCH_EXPECTANCY_NON_POSITIVE" in evaluation["reason_codes"]


def test_new_lineage_versions_are_explicit() -> None:
    assert recommender_module.TREND_OUTCOME_LABEL_VERSION == "directional_trend_label_v2"
    assert recommender_module.TREND_STRATEGY_CONTRACT_VERSION == "directional_trend_v2"


def test_trend_event_model_fits_purged_chronological_three_class_data(tmp_path: Path) -> None:
    from app.calibration import FEATURE_NAMES
    from app.trend_events import fit_trend_event_model, load_trend_event_model, save_trend_event_model, trend_event_storage_key

    rows = []
    classes = ["TP_FIRST", "SL_FIRST", "HORIZON_EXIT"]
    base_ts = 1_700_000_000
    for index in range(120):
        event_type = classes[index % 3]
        snapshot = {name: 0.0 for name in FEATURE_NAMES}
        if event_type == "TP_FIRST":
            snapshot.update(range_score=0.10, trend_strength=0.90, coherence=0.90, score=0.80)
            ret = 0.04
        elif event_type == "SL_FIRST":
            snapshot.update(range_score=0.20, trend_strength=0.20, coherence=0.20, score=-0.80)
            ret = -0.02
        else:
            snapshot.update(range_score=0.80, trend_strength=0.50, coherence=0.50, score=0.0)
            ret = 0.002
        decision_ts = base_ts + index * 13 * 3600
        rows.append({
            "bot_type": "directional_trend",
            "event_type": event_type,
            "ts": decision_ts,
            "label_available_ts": decision_ts + 12 * 3600 + 60,
            "ret": ret,
            "score": snapshot["score"],
            "reasons": {"feature_snapshot": snapshot},
        })

    model = fit_trend_event_model(
        rows,
        min_samples=60,
        policy_fingerprint="b" * 64,
        outcome_label_version="directional_trend_label_v2",
    )
    assert model.fitted is True
    assert model.holdout_status == "accepted"
    assert model.holdout_log_loss < model.holdout_null_log_loss
    assert model.class_counts == {"TP_FIRST": 40, "SL_FIRST": 40, "HORIZON_EXIT": 40}

    conn = db.connect(str(tmp_path / "event_model.db"))
    db.init_db(conn)
    key = trend_event_storage_key("b" * 64)
    save_trend_event_model(conn, key, model)
    loaded = load_trend_event_model(conn, key)
    assert loaded is not None and loaded.fitted is True
    assert loaded.predict_proba([0.1, 0.9, 0.0, 0.0, 0.0, 0.9, 0.0, 0.8, 0.0, 0.0, 0.0, 0.0, 0.0])["TP_FIRST"] > 0.70
    conn.close()


def test_status_and_frontend_expose_first_touch_readiness() -> None:
    root = Path(__file__).resolve().parents[1]
    main_source = (root / "app" / "main.py").read_text()
    ui_source = (root / "app" / "ui" / "static" / "app.js").read_text()
    assert '"trend_first_touch_model"' in main_source
    assert '"first_touch_model"' in main_source
    assert "P(TP раньше SL)" in ui_source
    assert "P(SL раньше TP)" in ui_source
    assert "First-touch EV" in ui_source


def test_release_documents_and_iterative_pdf_match_current_contract() -> None:
    from html import unescape
    import re
    from zipfile import ZipFile
    from pypdf import PdfReader

    root = Path(__file__).resolve().parents[1]
    prompt_pdf = root / "Bybit_Recommender_Iteration_Prompt.pdf"
    prompt_md = root / "docs" / "Bybit_Recommender_Iteration_Prompt.md"
    operator_docx = root / "docs" / "instrukciya_operatora_bybit_recommender.docx"
    infographic = root / "how_to_trade.png"

    prompt_text = "\n".join(page.extract_text() or "" for page in PdfReader(prompt_pdf).pages)
    for expected in (
        "v1.4.5",
        "exchange-executable",
        "generatedsizing",
        "directional_trend",
        "TP_FIRST",
        "SL_FIRST",
        "HORIZON_EXIT",
        "strategy-profitability-router-v3",
    ):
        assert expected in prompt_text
    assert "Поддерживаемый bot_type — `futures_grid`" not in prompt_text

    md_text = prompt_md.read_text()
    assert "TP_FIRST / SL_FIRST / HORIZON_EXIT" in md_text
    assert "first-touch" in md_text

    with ZipFile(operator_docx) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    operator_text = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", xml)))
    assert "Версия документа: 1.4.6" in operator_text
    assert "P(TP раньше SL)" in operator_text
    assert "AMBIGUOUS" in operator_text

    assert infographic.stat().st_size > 100_000


def _synthetic_event_rows(count: int = 120, *, include_availability: bool = True) -> list[dict]:
    from app.calibration import FEATURE_NAMES

    rows = []
    classes = ["TP_FIRST", "SL_FIRST", "HORIZON_EXIT"]
    base_ts = 1_710_000_000
    for index in range(count):
        event_type = classes[index % 3]
        snapshot = {name: 0.0 for name in FEATURE_NAMES}
        snapshot.update(
            trend_strength=0.9 if event_type == "TP_FIRST" else 0.2,
            coherence=0.9 if event_type == "TP_FIRST" else 0.3,
            range_score=0.8 if event_type == "HORIZON_EXIT" else 0.1,
            score={"TP_FIRST": 0.8, "SL_FIRST": -0.8, "HORIZON_EXIT": 0.0}[event_type],
        )
        ts = base_ts + index * 13 * 3600
        row = {
            "bot_type": "directional_trend",
            "event_type": event_type,
            "ts": ts,
            "ret": {"TP_FIRST": 0.04, "SL_FIRST": -0.02, "HORIZON_EXIT": 0.002}[event_type],
            "score": snapshot["score"],
            "reasons": {"feature_snapshot": snapshot},
        }
        if include_availability:
            row["label_available_ts"] = ts + 12 * 3600 + 60
        rows.append(row)
    return rows


def test_event_model_rejects_labels_without_exact_availability() -> None:
    from app.trend_events import fit_trend_event_model

    model = fit_trend_event_model(
        _synthetic_event_rows(include_availability=False),
        min_samples=60,
        policy_fingerprint="c" * 64,
        outcome_label_version="directional_trend_label_v2",
    )
    assert model.fitted is False
    assert model.n_samples == 0


def test_terminal_holdout_is_never_refit_into_the_deployed_event_model(monkeypatch) -> None:
    from app import trend_events

    fit_lengths: list[int] = []

    def fake_fit(xs, ys, *, class_count, **kwargs):
        fit_lengths.append(len(xs))
        return [[0.0] * len(trend_events.FEATURE_NAMES) for _ in range(class_count)], [0.0] * class_count

    losses = iter([0.50, 1.00])
    monkeypatch.setattr(trend_events, "_fit_softmax", fake_fit)
    monkeypatch.setattr(trend_events, "_multiclass_log_loss", lambda labels, probs: next(losses))
    model = trend_events.fit_trend_event_model(
        _synthetic_event_rows(),
        min_samples=60,
        policy_fingerprint="d" * 64,
        outcome_label_version="directional_trend_label_v2",
    )
    assert model.fitted is True
    assert len(fit_lengths) == 2
    assert fit_lengths[1] == fit_lengths[0]
    assert fit_lengths[1] < model.n_samples


def test_probability_uncertainty_is_not_artificially_capped_at_twenty_percent(monkeypatch) -> None:
    from app import trend_events

    monkeypatch.setattr(
        trend_events,
        "_fit_softmax",
        lambda xs, ys, *, class_count, **kwargs: (
            [[0.0] * len(trend_events.FEATURE_NAMES) for _ in range(class_count)],
            [0.0] * class_count,
        ),
    )
    losses = iter([0.50, 1.00])
    monkeypatch.setattr(trend_events, "_multiclass_log_loss", lambda labels, probs: next(losses))
    model = trend_events.fit_trend_event_model(
        _synthetic_event_rows(90),
        min_samples=60,
        policy_fingerprint="e" * 64,
        outcome_label_version="directional_trend_label_v2",
    )
    assert model.fitted is True
    assert model.probability_error_bound is not None
    assert model.probability_error_bound > 0.20


def test_first_touch_lower_ev_allocates_uncertainty_to_the_worst_exit() -> None:
    rec = _trend_candidate()
    model = TrendEventModel(
        fitted=True,
        classes=TREND_EVENT_TYPES,
        coef=[[0.0] * len(FEATURE_NAMES) for _ in TREND_EVENT_TYPES],
        intercept=[math.log(0.60), math.log(0.20), math.log(0.20)],
        n_samples=300,
        holdout_status="accepted",
        holdout_log_loss=0.70,
        holdout_null_log_loss=1.05,
        probability_error_bound=0.10,
        horizon_exit_mean_return=-0.01,
        horizon_exit_return_lower_bound=-0.06,
        policy_fingerprint="a" * 64,
        outcome_label_version="directional_trend_label_v2",
    )
    assessment = build_trend_event_assessment(rec, [0.0] * len(FEATURE_NAMES), model)
    assert assessment["event_expected_net_return"] > 0.0
    assert assessment["event_expected_net_return_lower_bound"] < 0.0
    assert assessment["ready"] is False
    assert "TREND_FIRST_TOUCH_EXPECTANCY_NON_POSITIVE" in assessment["reason_codes"]
