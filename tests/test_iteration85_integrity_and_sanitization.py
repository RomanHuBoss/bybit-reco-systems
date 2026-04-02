from __future__ import annotations

import time
from pathlib import Path

from app import db
from app.calibration import fit_logreg
from app.outcomes import compute_outcomes_once
from app import sentiment as sentiment_module


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def get(self, *args, **kwargs):
        return _FakeResponse(self.payload)


def test_compute_outcomes_skips_recommendations_with_venue_bot_type_mismatch(tmp_path: Path) -> None:
    conn = db.connect(str(tmp_path / "outcomes_integrity.db"))
    db.init_db(conn)
    try:
        ts_now = int(time.time())
        rec_ts = ts_now - 7 * 3600
        db.insert_recommendations(
            conn,
            [
                {
                    "rec_id": "R-bad-venue",
                    "ts": rec_ts,
                    "venue": "linear",
                    "symbol": "BTCUSDT",
                    "bot_type": "spot_grid",
                    "direction": "long",
                    "account_mode": "spot",
                    "margin_mode": "cash",
                    "score": 0.8,
                    "confidence": 0.7,
                    "expected_rr": 1.3,
                    "risk_score": 0.2,
                    "params": {},
                    "reasons": {},
                    "blocks": [],
                    "status": "recommended",
                    "ttl_sec": 3600,
                    "model_version": "test",
                    "features_ref_ts": rec_ts,
                }
            ],
        )

        done = compute_outcomes_once(conn, horizon_sec=30 * 60, max_to_process=10)

        assert done == 0
        assert db.outcome_exists(conn, "R-bad-venue") is False
        log = conn.execute(
            "SELECT action, details_json FROM decision_log WHERE rec_id=? ORDER BY id DESC LIMIT 1",
            ("R-bad-venue",),
        ).fetchone()
        assert log is not None
        assert log["action"] == "OUTCOME_SKIP_UNSUPPORTED_DIRECTION"
        assert '"venue": "linear"' in log["details_json"]
    finally:
        conn.close()


def test_fit_logreg_skips_malformed_rows_instead_of_crashing() -> None:
    base_reasons = {
        "direction_agg": {
            "direction_confidence_calibrated": 0.7,
            "coherence": 0.6,
            "strength": {"all": 0.3},
            "regime_confidence": 0.55,
        },
        "cost_model": {"spread_bps": 7.0},
        "effective_sentiment": 0.1,
        "open_interest": {"oi_4h_chg_pct": 4.0},
        "funding": {"carry_cost_bps_8h": 3.0},
        "liquidity": {"tier": "high"},
        "btc_beta": {"correlation": 0.35},
        "top_positive_factors": [{"feature": "atr_pct", "value": 0.012}],
    }
    good_rows = [
        {"score": 0.85, "success": 1, "ts": 1_700_000_000, "reasons": base_reasons},
        {"score": 0.65, "success": 1, "ts": 1_700_000_100, "reasons": base_reasons},
        {"score": -0.55, "success": 0, "ts": 1_700_000_200, "reasons": base_reasons},
        {"score": -0.75, "success": 0, "ts": 1_700_000_300, "reasons": base_reasons},
    ]
    dirty_rows = [
        {"score": "broken", "success": 1, "ts": 1_700_000_400, "reasons": base_reasons},
        {"score": 0.1, "success": "NaN", "ts": "oops", "reasons": base_reasons},
    ]

    model = fit_logreg(good_rows + dirty_rows, min_samples=4, logreg_min_samples=4)

    assert model.fitted is True
    assert model.n_samples == 4
    assert len(model.coef) > 0


def test_fetch_reddit_sentiment_sanitizes_non_finite_upvote_ratio(monkeypatch) -> None:
    monkeypatch.setattr(sentiment_module, "REDDIT_RSS", {"BTCUSDT": "https://reddit.test/btc"})
    client = _FakeClient(
        {
            "data": {
                "children": [
                    {
                        "data": {
                            "title": "completely neutral headline",
                            "selftext": "",
                            "upvote_ratio": "NaN",
                        }
                    }
                ]
            }
        }
    )

    result = sentiment_module.fetch_reddit_sentiment(client)

    assert "BTCUSDT" in result
    assert result["BTCUSDT"]["sentiment"] == 0.0


def test_fetch_coingecko_momentum_keeps_valid_symbols_when_neighbor_row_is_malformed(monkeypatch) -> None:
    monkeypatch.setattr(
        sentiment_module,
        "COINGECKO_IDS",
        {"BTCUSDT": "bitcoin", "ETHUSDT": "ethereum"},
    )
    client = _FakeClient(
        [
            {
                "id": "bitcoin",
                "price_change_percentage_24h": 4.0,
                "price_change_percentage_7d_in_currency": 9.0,
                "total_volume": "oops",
            },
            {
                "id": "ethereum",
                "price_change_percentage_24h": 7.5,
                "price_change_percentage_7d_in_currency": 11.0,
                "total_volume": 2500000,
            },
        ]
    )

    result = sentiment_module.fetch_coingecko_momentum(client)

    assert set(result) == {"BTCUSDT", "ETHUSDT"}
    assert result["BTCUSDT"]["volume"] == 1
    assert result["ETHUSDT"]["volume"] == 2500000
    assert result["ETHUSDT"]["sentiment"] > 0.0
