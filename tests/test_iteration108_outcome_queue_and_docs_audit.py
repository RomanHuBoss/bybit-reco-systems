from __future__ import annotations

from pathlib import Path

from app import db
from app.outcomes import compute_outcomes_once


def _settings_for_llm_enabled() -> object:
    class _S:
        llm_reviewer_enabled = True
    return _S()


def test_compute_outcomes_llm_sql_prefilter_reaches_newer_matured_row(tmp_path: Path, monkeypatch) -> None:
    import app.outcomes as outcomes_module

    conn = db.connect(str(tmp_path / 'iteration108_outcomes.db'))
    db.init_db(conn)
    now = db.now_ts()
    legacy_base_ts = now - 20 * 3600
    good_ts = now - 14 * 3600

    legacy_recs = []
    for idx in range(20):
        ts = legacy_base_ts + idx * 60
        legacy_recs.append(
            {
                'rec_id': f'R-legacy-{idx}',
                'ts': ts,
                'venue': 'linear',
                'symbol': f'LEGACY{idx}USDT',
                'bot_type': 'futures_grid',
                'direction': 'neutral',
                'account_mode': 'one_way',
                'margin_mode': 'cross',
                'score': 0.1,
                'confidence': 0.6,
                'expected_rr': 1.1,
                'risk_score': 0.2,
                'params': {'grid_levels': 5, 'grid_spacing_pct': 1.0},
                'reasons': {},
                'blocks': [],
                'status': 'recommended',
                'ttl_sec': 1800,
                'model_version': 'test',
                'features_ref_ts': ts,
            }
        )

    good_rec = {
        'rec_id': 'R-good-llm-ok',
        'ts': good_ts,
        'venue': 'linear',
        'symbol': 'BTCUSDT',
        'bot_type': 'futures_grid',
        'direction': 'neutral',
        'account_mode': 'one_way',
        'margin_mode': 'cross',
        'score': 0.2,
        'confidence': 0.72,
        'expected_rr': 1.2,
        'risk_score': 0.15,
        'params': {
            'grid_count': 5,
            'grid_levels': 5,
            'grid_spacing_pct': 1.0,
            'price_range_lower': 95.0,
            'price_range_upper': 105.0,
            'cost_model': {'execution_cost_bps': 0.0, 'expected_funding_bps': 0.0},
            'trade_plan': {
                'grid_count': 5,
                'cost_model': {'execution_cost_bps': 0.0, 'expected_funding_bps': 0.0},
                'levels': {
                    'range': {'lower': 95.0, 'upper': 105.0},
                    'kill_switch': {'lower': 94.0, 'upper': 106.0},
                },
            },
        },
        'reasons': {'llm_review': {'status': 'ok', 'mode': 'advisory', 'gate_decision': 'pass'}},
        'blocks': [],
        'status': 'recommended',
        'ttl_sec': 1800,
        'model_version': 'test',
        'features_ref_ts': good_ts,
    }

    db.insert_recommendations(conn, legacy_recs + [good_rec])

    entry_ts = good_ts + 60
    exit_ts = entry_ts + 12 * 3600
    db.upsert_ohlcv(
        conn,
        [
            {
                'venue': 'linear',
                'symbol': 'BTCUSDT',
                'tf_sec': 60,
                'ts': candle_ts,
                'open': 100.0 if candle_ts < exit_ts else 100.1,
                'high': 101.0 if candle_ts < exit_ts else 100.6,
                'low': 99.5 if candle_ts < exit_ts else 99.7,
                'close': 100.3 if candle_ts < exit_ts else 100.0,
                'volume': 10.0 if candle_ts < exit_ts else 8.0,
            }
            for candle_ts in range(entry_ts, exit_ts + 60, 60)
        ],
    )

    monkeypatch.setattr(outcomes_module, 'settings', _settings_for_llm_enabled())

    try:
        processed = compute_outcomes_once(conn, horizon_sec=30 * 60, max_to_process=1)

        assert processed == 1
        assert db.outcome_exists(conn, 'R-good-llm-ok') is True
        assert db.outcome_exists(conn, 'R-legacy-0') is False
    finally:
        conn.close()


def test_release_docs_do_not_reference_external_audit_report_artifacts() -> None:
    root = Path(__file__).resolve().parent.parent
    readme = (root / 'README.md').read_text(encoding='utf-8')
    changelog = (root / 'CHANGELOG.md').read_text(encoding='utf-8')

    for payload in (readme, changelog):
        assert 'AUDIT_REPORT_' not in payload
        assert 'docs/AUDIT_REPORT_' not in payload
        assert 'docs/audit_' not in payload.lower()
