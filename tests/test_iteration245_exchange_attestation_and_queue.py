from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app import calibration, db, outcomes, recommender


def _seed_recommendation_and_bot(conn, *, suffix: str, stopped: bool = True) -> tuple[str, str, int]:
    ts = int(time.time()) - 1_000
    rec_id = f"R-245-{suffix}"
    bot_id = f"B-245-{suffix}"
    db.insert_recommendations(
        conn,
        [{
            "rec_id": rec_id,
            "ts": ts,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "long",
            "account_mode": "one_way",
            "margin_mode": "cross",
            "score": 0.5,
            "confidence": 0.6,
            "expected_rr": 1.0,
            "risk_score": 0.2,
            "params": {},
            "reasons": {},
            "blocks": [],
            "status": "executed",
            "ttl_sec": 3_600,
            "model_version": recommender.RECOMMENDER_MODEL_VERSION,
            "features_ref_ts": ts - 60,
            "publication_root_rec_id": rec_id,
            "is_outcome_label_root": True,
        }],
    )
    db.insert_bot_instance(
        conn,
        {
            "bot_id": bot_id,
            "started_ts": ts + 10,
            "stopped_ts": ts + 500 if stopped else None,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "mode": {"direction": "long"},
            "params": {},
            "state": {},
            "status": "stopped" if stopped else "running",
            "origin_rec_id": rec_id,
            "publication_root_rec_id": rec_id,
        },
    )
    return rec_id, bot_id, ts


def _insert_flat_execution_pair(conn, rec_id: str, bot_id: str, ts: int) -> None:
    for suffix, side, price, gross in (
        ("buy", "Buy", 100.0, 0.0),
        ("sell", "Sell", 101.0, 2.0),
    ):
        db.insert_execution_event(
            conn,
            {
                "event_id": f"EV-245-{suffix}",
                "bot_id": bot_id,
                "origin_rec_id": rec_id,
                "ts": ts + (100 if side == "Buy" else 200),
                "symbol": "BTCUSDT",
                "event_type": "execution",
                "source": "bybit_execution",
                "external_event_id": f"EXT-245-{suffix}",
                "external_order_id": f"ORD-245-{suffix}",
                "side": side,
                "qty": 0.1,
                "price": price,
                "order_price": price,
                "benchmark_price": price,
                "benchmark_ts": ts + (99 if side == "Buy" else 199),
                "benchmark_source": "pre_submit_mid",
                "gross_pnl": gross,
                "fee": 0.1,
                "funding": 0.0,
                "slippage": 0.0,
                "currency": "USDT",
                "meta": {},
            },
        )


def test_flat_local_ledger_is_not_live_profit_without_terminal_exchange_reconciliation(tmp_path) -> None:
    conn = db.connect(str(tmp_path / "unreconciled.sqlite"))
    db.init_db(conn)
    try:
        rec_id, bot_id, ts = _seed_recommendation_and_bot(conn, suffix="unreconciled")
        _insert_flat_execution_pair(conn, rec_id, bot_id, ts)

        summary = db.get_bot_execution_summary(conn, bot_id)
        assert summary["position_flat"] is True
        assert summary["exchange_reconciled"] is False
        assert summary["total_pnl_finalized"] is False
        assert summary["evidence_grade"] is False
        assert db.list_realized_net_events(conn, since_ts=ts) == []
    finally:
        conn.close()


def test_matching_terminal_exchange_reconciliation_unlocks_finalized_live_pnl(tmp_path) -> None:
    conn = db.connect(str(tmp_path / "reconciled.sqlite"))
    db.init_db(conn)
    try:
        rec_id, bot_id, ts = _seed_recommendation_and_bot(conn, suffix="reconciled")
        _insert_flat_execution_pair(conn, rec_id, bot_id, ts)
        result = db.insert_execution_reconciliation(
            conn,
            {
                "reconciliation_id": "XR-245-ok",
                "bot_id": bot_id,
                "origin_rec_id": rec_id,
                "ts": ts + 600,
                "source": "bybit_private_reconciliation",
                "external_snapshot_id": "BYBIT-SNAPSHOT-245-ok",
                "position_qty": 0.0,
                "open_order_count": 0,
                "execution_event_count": 2,
                "funding_event_count": 0,
                "realized_pnl_gross": 2.0,
                "fee": 0.2,
                "funding": 0.0,
                "currency": "USDT",
                "complete": True,
                "meta": {"cursor": "closed-pnl:245"},
            },
        )
        assert result == "inserted"

        summary = db.get_bot_execution_summary(conn, bot_id)
        assert summary["exchange_reconciled"] is True
        assert summary["total_pnl_finalized"] is True
        assert summary["evidence_grade"] is True
        events = db.list_realized_net_events(conn, since_ts=ts)
        assert sum(event["net_pnl"] for event in events) == pytest.approx(1.8)
    finally:
        conn.close()


def test_reconciliation_before_bot_stop_is_rejected_at_persistence_boundary(tmp_path) -> None:
    conn = db.connect(str(tmp_path / "pre-stop-reconciliation.sqlite"))
    db.init_db(conn)
    try:
        rec_id, bot_id, ts = _seed_recommendation_and_bot(conn, suffix="pre-stop")
        _insert_flat_execution_pair(conn, rec_id, bot_id, ts)
        with pytest.raises(ValueError, match="at or after bot stop"):
            db.insert_execution_reconciliation(
                conn,
                {
                    "reconciliation_id": "XR-245-pre-stop",
                    "bot_id": bot_id,
                    "origin_rec_id": rec_id,
                    "ts": ts + 400,
                    "source": "bybit_private_reconciliation",
                    "external_snapshot_id": "BYBIT-SNAPSHOT-245-pre-stop",
                    "position_qty": 0.0,
                    "open_order_count": 0,
                    "execution_event_count": 2,
                    "funding_event_count": 0,
                    "realized_pnl_gross": 2.0,
                    "fee": 0.2,
                    "funding": 0.0,
                    "currency": "USDT",
                    "complete": True,
                    "meta": {},
                },
            )
    finally:
        conn.close()


def test_reconciliation_monetary_mismatch_cannot_finalize_live_pnl(tmp_path) -> None:
    conn = db.connect(str(tmp_path / "mismatched-reconciliation.sqlite"))
    db.init_db(conn)
    try:
        rec_id, bot_id, ts = _seed_recommendation_and_bot(conn, suffix="mismatch")
        _insert_flat_execution_pair(conn, rec_id, bot_id, ts)
        db.insert_execution_reconciliation(
            conn,
            {
                "reconciliation_id": "XR-245-mismatch",
                "bot_id": bot_id,
                "origin_rec_id": rec_id,
                "ts": ts + 600,
                "source": "bybit_private_reconciliation",
                "external_snapshot_id": "BYBIT-SNAPSHOT-245-mismatch",
                "position_qty": 0.0,
                "open_order_count": 0,
                "execution_event_count": 2,
                "funding_event_count": 0,
                "realized_pnl_gross": 2.0,
                "fee": 0.19,
                "funding": 0.0,
                "currency": "USDT",
                "complete": True,
                "meta": {},
            },
        )

        summary = db.get_bot_execution_summary(conn, bot_id)
        assert summary["exchange_reconciled"] is False
        assert summary["total_pnl_finalized"] is False
        assert "fee_mismatch" in summary["exchange_reconciliation_failures"]
        assert db.list_realized_net_events(conn, since_ts=ts) == []
    finally:
        conn.close()


def test_censored_outcome_roots_cannot_starve_newer_matured_root(tmp_path, monkeypatch) -> None:
    conn = db.connect(str(tmp_path / "outcome-queue.sqlite"))
    db.init_db(conn)
    now = int(time.time())
    legacy_base = now - 20 * 3600
    for index in range(12):
        ts = legacy_base + index * 60
        rec_id = f"R-245-censored-{index}"
        db.insert_recommendations(
            conn,
            [{
                "rec_id": rec_id,
                "ts": ts,
                "venue": "linear",
                "symbol": f"MISS{index}USDT",
                "bot_type": "futures_grid",
                "direction": "neutral",
                "account_mode": "one_way",
                "margin_mode": "cross",
                "score": 0.2,
                "confidence": 0.6,
                "expected_rr": 1.0,
                "risk_score": 0.2,
                "params": {"grid_count": 5, "grid_spacing_pct": 1.0},
                "reasons": {},
                "blocks": [],
                "status": "recommended",
                "ttl_sec": 1_800,
                "model_version": "queue-test",
                "features_ref_ts": ts,
            }],
        )
        db.upsert_outcome_observability(
            conn,
            rec_id=rec_id,
            recommendation_ts=ts,
            label_due_ts=ts + 12 * 3600 + 120,
            state="censored",
            reason="terminal_missing_history",
        )

    good_ts = now - 14 * 3600
    good_rec = {
        "rec_id": "R-245-good-after-censored",
        "ts": good_ts,
        "venue": "linear",
        "symbol": "BTCUSDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "account_mode": "one_way",
        "margin_mode": "cross",
        "score": 0.2,
        "confidence": 0.6,
        "expected_rr": 1.0,
        "risk_score": 0.2,
        "params": {
            "grid_count": 5,
            "grid_levels": 5,
            "grid_spacing_pct": 1.0,
            "price_range_lower": 95.0,
            "price_range_upper": 105.0,
            "cost_model": {"execution_cost_bps": 0.0, "expected_funding_bps": 0.0},
            "trade_plan": {
                "grid_count": 5,
                "cost_model": {"execution_cost_bps": 0.0, "expected_funding_bps": 0.0},
                "levels": {
                    "range": {"lower": 95.0, "upper": 105.0},
                    "kill_switch": {"lower": 94.0, "upper": 106.0},
                },
            },
        },
        "reasons": {},
        "blocks": [],
        "status": "recommended",
        "ttl_sec": 1_800,
        "model_version": "queue-test",
        "features_ref_ts": good_ts,
    }
    db.insert_recommendations(conn, [good_rec])
    entry_ts = good_ts + 60
    exit_ts = entry_ts + 12 * 3600
    db.upsert_ohlcv(
        conn,
        [{
            "venue": "linear",
            "symbol": "BTCUSDT",
            "tf_sec": 60,
            "ts": candle_ts,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 100.0,
        } for candle_ts in range(entry_ts, exit_ts + 60, 60)],
    )
    monkeypatch.setattr(outcomes, "settings", SimpleNamespace(llm_reviewer_enabled=False))
    try:
        assert outcomes.compute_outcomes_once(conn, max_to_process=1) == 1
        assert db.outcome_exists(conn, good_rec["rec_id"]) is True
    finally:
        conn.close()


def test_waiting_outcome_attempts_are_rotated_instead_of_starving_queue(tmp_path, monkeypatch) -> None:
    conn = db.connect(str(tmp_path / "outcome-wait-rotation.sqlite"))
    db.init_db(conn)
    now = int(time.time())
    legacy_base = now - 20 * 3600
    for index in range(12):
        ts = legacy_base + index * 60
        db.insert_recommendations(
            conn,
            [{
                "rec_id": f"R-245-waiting-{index}",
                "ts": ts,
                "venue": "linear",
                "symbol": f"NODATA{index}USDT",
                "bot_type": "futures_grid",
                "direction": "neutral",
                "account_mode": "one_way",
                "margin_mode": "cross",
                "score": 0.2,
                "confidence": 0.6,
                "expected_rr": 1.0,
                "risk_score": 0.2,
                "params": {"grid_count": 5, "grid_spacing_pct": 1.0},
                "reasons": {},
                "blocks": [],
                "status": "recommended",
                "ttl_sec": 1_800,
                "model_version": "queue-test",
                "features_ref_ts": ts,
            }],
        )

    good_ts = now - 14 * 3600
    good_rec = {
        "rec_id": "R-245-good-after-waiting",
        "ts": good_ts,
        "venue": "linear",
        "symbol": "ETHUSDT",
        "bot_type": "futures_grid",
        "direction": "neutral",
        "account_mode": "one_way",
        "margin_mode": "cross",
        "score": 0.2,
        "confidence": 0.6,
        "expected_rr": 1.0,
        "risk_score": 0.2,
        "params": {
            "grid_count": 5,
            "grid_levels": 5,
            "grid_spacing_pct": 1.0,
            "price_range_lower": 95.0,
            "price_range_upper": 105.0,
            "cost_model": {"execution_cost_bps": 0.0, "expected_funding_bps": 0.0},
            "trade_plan": {
                "grid_count": 5,
                "cost_model": {"execution_cost_bps": 0.0, "expected_funding_bps": 0.0},
                "levels": {
                    "range": {"lower": 95.0, "upper": 105.0},
                    "kill_switch": {"lower": 94.0, "upper": 106.0},
                },
            },
        },
        "reasons": {},
        "blocks": [],
        "status": "recommended",
        "ttl_sec": 1_800,
        "model_version": "queue-test",
        "features_ref_ts": good_ts,
    }
    db.insert_recommendations(conn, [good_rec])
    entry_ts = good_ts + 60
    exit_ts = entry_ts + 12 * 3600
    db.upsert_ohlcv(
        conn,
        [{
            "venue": "linear",
            "symbol": "ETHUSDT",
            "tf_sec": 60,
            "ts": candle_ts,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 100.0,
        } for candle_ts in range(entry_ts, exit_ts + 60, 60)],
    )
    monkeypatch.setattr(outcomes, "settings", SimpleNamespace(llm_reviewer_enabled=False))
    try:
        assert outcomes.compute_outcomes_once(conn, max_to_process=1) == 0
        assert outcomes.compute_outcomes_once(conn, max_to_process=1) == 1
        assert db.outcome_exists(conn, good_rec["rec_id"]) is True
    finally:
        conn.close()


def test_corrupted_policy_label_due_is_counted_as_unresolved_not_omitted(tmp_path) -> None:
    conn = db.connect(str(tmp_path / "policy-due.sqlite"))
    db.init_db(conn)
    try:
        ts = int(time.time()) - 14 * 3600
        fingerprint = "c" * 64
        db.insert_recommendations(
            conn,
            [{
                "rec_id": "R-245-invalid-due",
                "ts": ts,
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "neutral",
                "account_mode": "one_way",
                "margin_mode": "cross",
                "score": 0.2,
                "confidence": 0.6,
                "expected_rr": 1.0,
                "risk_score": 0.2,
                "params": {},
                "reasons": {
                    "outcome_policy": {
                        "eligible": True,
                        "policy_evaluation_eligible": True,
                        "policy_fingerprint": fingerprint,
                        "label_due_ts": ts + 100 * 86400,
                    }
                },
                "blocks": [],
                "status": "no_trade",
                "ttl_sec": 1_800,
                "model_version": recommender.RECOMMENDER_MODEL_VERSION,
                "features_ref_ts": ts,
            }],
        )
        diagnostics = db.get_policy_outcome_observability(
            conn,
            model_version=recommender.RECOMMENDER_MODEL_VERSION,
            policy_fingerprint=fingerprint,
        )
        assert diagnostics["matured_total"] == 1
        assert diagnostics["unresolved_total"] == 1
        assert diagnostics["invalid_contract_total"] == 1
    finally:
        conn.close()


def test_dynamic_cache_rejects_payload_from_other_policy(tmp_path, monkeypatch) -> None:
    conn = db.connect(str(tmp_path / "cache-policy.sqlite"))
    db.init_db(conn)
    fingerprint = "d" * 64
    key = recommender.policy_calibration_storage_key(
        calibration.GLOBAL_LOGREG_KEY,
        fingerprint,
    )
    calibration.save_logreg_to_db(
        conn,
        key,
        calibration.LogRegScaler(
            fitted=True,
            coef=[0.1] * calibration.N_FEATURES,
            platt=calibration.PlattScaler(fitted=True),
            saved_ts=int(time.time()),
            n_samples=320,
            return_samples=320,
            expectancy_status="positive",
            weighted_mean_return=0.01,
            policy_fingerprint="e" * 64,
        ),
    )
    monkeypatch.setattr(
        recommender,
        "_fit_global_logreg",
        lambda *_args, **_kwargs: calibration.LogRegScaler(
            fitted=False,
            expectancy_status="insufficient",
            policy_fingerprint=fingerprint,
        ),
    )
    try:
        loaded = recommender._load_or_fit_global_logreg(
            conn,
            min_samples=80,
            policy_fingerprint=fingerprint,
            settings_obj=SimpleNamespace(
                llm_reviewer_enabled=False,
                mean_reversion_min_score=0.25,
            ),
        )
        assert loaded.fitted is False
    finally:
        conn.close()


def test_fresh_positive_cache_is_disabled_when_supporting_rows_disappear(tmp_path) -> None:
    conn = db.connect(str(tmp_path / "cache-support.sqlite"))
    db.init_db(conn)
    fingerprint = "f" * 64
    key = recommender.policy_calibration_storage_key(
        calibration.GLOBAL_LOGREG_KEY,
        fingerprint,
    )
    calibration.save_logreg_to_db(
        conn,
        key,
        calibration.LogRegScaler(
            fitted=True,
            coef=[0.1] * calibration.N_FEATURES,
            intercept=0.0,
            platt=calibration.PlattScaler(fitted=True),
            saved_ts=int(time.time()),
            n_samples=320,
            return_samples=320,
            expectancy_status="positive",
            weighted_mean_return=0.01,
            weighted_mean_return_lower_bound=0.005,
            policy_fingerprint=fingerprint,
            oof_status="sufficient",
                oof_samples=80,
                oof_required_samples=80,
                oof_skill_status="accepted",
                oof_final_samples=80,
                oof_required_final_samples=80,
                oof_final_decision_cohorts=5,
                oof_required_final_decision_cohorts=5,
                selected_policy_expectancy_status="positive",
                selected_policy_confidence_threshold=0.52,
                selected_policy_samples=80,
                selected_policy_weighted_mean_return=0.01,
                selected_policy_weighted_effective_return_samples=80.0,
                selected_policy_weighted_mean_return_lower_bound=0.005,
                selected_policy_temporal_cluster_count=20,
                selected_policy_minimum_temporal_clusters=20,
                selected_policy_weighted_effective_temporal_clusters=20.0,
                selected_policy_weighted_temporal_mean_return=0.01,
                selected_policy_weighted_temporal_mean_return_lower_bound=0.005,
                terminal_selected_policy_expectancy_status="positive",
                terminal_selected_policy_samples=80,
                terminal_selected_policy_required_samples=80,
                terminal_selected_policy_weighted_mean_return=0.01,
                terminal_selected_policy_weighted_effective_return_samples=80.0,
                terminal_selected_policy_weighted_mean_return_lower_bound=0.005,
                terminal_selected_policy_temporal_cluster_count=5,
                terminal_selected_policy_required_temporal_clusters=5,
                terminal_selected_policy_weighted_effective_temporal_clusters=5.0,
                terminal_selected_policy_weighted_temporal_mean_return_lower_bound=0.005,
            ),
    )
    try:
        loaded = recommender._load_or_fit_global_logreg(
            conn,
            min_samples=80,
            policy_fingerprint=fingerprint,
            settings_obj=SimpleNamespace(
                llm_reviewer_enabled=False,
                mean_reversion_min_score=0.25,
            ),
        )
        assert loaded.fitted is False
        assert loaded.expectancy_status == "censored"
        assert loaded.policy_unresolved_total >= 320
    finally:
        conn.close()


def test_policy_fingerprint_covers_universe_and_reviewer_gate() -> None:
    base = SimpleNamespace(
        mean_reversion_min_score=0.25,
        min_score_to_recommend=0.08,
        min_conf_to_recommend=0.52,
        require_conf_gate=True,
        calib_min_samples=80,
        taker_fee_bps_linear=6.0,
        llm_reviewer_enabled=True,
        llm_reviewer_min_confidence=0.65,
        symbols_linear=["BTCUSDT"],
        venues=["linear"],
    )
    changed_universe = SimpleNamespace(**{**vars(base), "symbols_linear": ["BTCUSDT", "ETHUSDT"]})
    changed_reviewer = SimpleNamespace(**{**vars(base), "llm_reviewer_min_confidence": 0.75})

    fp = recommender.calibration_policy_fingerprint(base, {"max_leverage": 3})
    assert fp != recommender.calibration_policy_fingerprint(
        changed_universe,
        {"max_leverage": 3},
    )
    assert fp != recommender.calibration_policy_fingerprint(
        changed_reviewer,
        {"max_leverage": 3},
    )


def _seed_policy_outcome(
    conn,
    *,
    suffix: str,
    contract: dict,
    fingerprint: str,
    exit_close: float,
) -> tuple[str, int]:
    ts = int(time.time()) - 50_000
    rec_id = f"R-245-policy-{suffix}"
    db.insert_recommendations(
        conn,
        [{
            "rec_id": rec_id,
            "ts": ts,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "long",
            "account_mode": "one_way",
            "margin_mode": "cross",
            "score": 0.5,
            "confidence": 0.6,
            "expected_rr": 1.0,
            "risk_score": 0.2,
            "params": {},
            "reasons": {
                "outcome_policy": {
                    "eligible": True,
                    "policy_evaluation_eligible": True,
                    "policy_fingerprint": fingerprint,
                    "policy_contract": contract,
                    "label_due_ts": ts + 12 * 3600 + 120,
                },
            },
            "blocks": [],
            "status": "executed",
            "ttl_sec": 3_600,
            "model_version": recommender.RECOMMENDER_MODEL_VERSION,
            "features_ref_ts": ts - 60,
            "publication_root_rec_id": rec_id,
            "is_outcome_label_root": True,
        }],
    )
    db.insert_outcome(
        conn,
        {
            "rec_id": rec_id,
            "ts": ts,
            "venue": "linear",
            "symbol": "BTCUSDT",
            "bot_type": "futures_grid",
            "direction": "long",
            "horizon_sec": 12 * 3600,
            "label_available_ts": ts + 12 * 3600 + 60,
            "entry_close": 100.0,
            "exit_close": exit_close,
            "ret": 0.01,
            "success": 1,
        },
    )
    return rec_id, ts


def test_tampered_policy_contract_cannot_become_a_labeled_support_row(tmp_path) -> None:
    conn = db.connect(str(tmp_path / "tampered-policy.sqlite"))
    db.init_db(conn)
    original_contract = {"schema_version": "candidate-policy-v1", "floor": 0.25}
    fingerprint = recommender.calibration_policy_contract_fingerprint(original_contract)
    try:
        _seed_policy_outcome(
            conn,
            suffix="tampered",
            contract={**original_contract, "floor": 0.99},
            fingerprint=fingerprint,
            exit_close=101.0,
        )
        diagnostics = db.get_policy_outcome_observability(
            conn,
            model_version=recommender.RECOMMENDER_MODEL_VERSION,
            policy_fingerprint=fingerprint,
        )
        assert diagnostics["matured_total"] == 1
        assert diagnostics["labeled_total"] == 0
        assert diagnostics["unresolved_total"] == 1
        assert diagnostics["invalid_contract_total"] == 1
    finally:
        conn.close()


def test_invalid_direction_label_revokes_fresh_direction_cache(tmp_path) -> None:
    conn = db.connect(str(tmp_path / "invalid-direction-label.sqlite"))
    db.init_db(conn)
    contract = {"schema_version": "candidate-policy-v1", "floor": 0.25}
    fingerprint = recommender.calibration_policy_contract_fingerprint(contract)
    key = recommender.policy_calibration_storage_key(
        recommender.DIRECTION_CALIBRATION_KEY,
        fingerprint,
    )
    try:
        _seed_policy_outcome(
            conn,
            suffix="invalid-direction",
            contract=contract,
            fingerprint=fingerprint,
            exit_close=0.0,
        )
        calibration.save_platt_to_db(
            conn,
            key,
            calibration.PlattScaler(
                a=3.0,
                b=-1.0,
                fitted=True,
                saved_ts=int(time.time()),
                policy_fingerprint=fingerprint,
            ),
        )
        loaded = recommender._load_or_fit_direction_calibrator(
            conn,
            min_samples=80,
            policy_fingerprint=fingerprint,
            settings_obj=SimpleNamespace(
                llm_reviewer_enabled=False,
                mean_reversion_min_score=0.25,
            ),
        )
        diagnostics = db.get_policy_outcome_observability(
            conn,
            model_version=recommender.RECOMMENDER_MODEL_VERSION,
            policy_fingerprint=fingerprint,
        )
        assert diagnostics["invalid_labeled_total"] == 1
        assert loaded.fitted is False
    finally:
        conn.close()
