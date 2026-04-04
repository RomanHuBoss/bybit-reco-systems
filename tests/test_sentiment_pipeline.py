from __future__ import annotations

import math
import pytest
from pathlib import Path

from app import db
from app.sentiment import _score_text, combine_global_sentiment
from app.sentiment_features import compute_sentiment_agg, compute_symbol_sentiment_map



def test_score_text_uses_whole_tokens_not_substrings():
    assert _score_text('The bank approved a startup grant.') == 0.0
    assert _score_text('Bull breakout and growth with no hack') > 0.0



def test_combine_global_sentiment_applies_min_source_weight():
    combined = combine_global_sentiment([
        {
            'scope': 'global',
            'key': 'fng',
            'sentiment': 1.0,
            'volume': 1,
            'sources': {'fng': 80},
            'tags': ['fear_greed'],
        },
        {
            'scope': 'global',
            'key': 'rss',
            'sentiment': -1.0,
            'volume': 60,
            'sources': {'rss': 60},
            'tags': ['rss'],
        },
    ])
    assert combined is not None
    # FnG should still matter despite volume=1; without minimum source weight this would be ~-0.97.
    assert combined['sentiment'] > -0.9
    assert combined['key'] == 'crypto'



def test_sentiment_feature_aggregation_compresses_duplicate_symbol_points(tmp_path: Path, monkeypatch):
    conn = db.connect(str(tmp_path / 'sentiment_features.db'))
    db.init_db(conn)
    now = 1_700_000_000
    monkeypatch.setattr('app.sentiment_features.time.time', lambda: now)

    # Multiple inserts in the same 15m bucket should not count as independent observations.
    for idx in range(6):
        db.insert_sentiment_point(
            conn,
            'symbol',
            'BTCUSDT',
            now - 300 + idx * 30,
            0.8,
            0.0,
            9,
            {'test': True},
            ['synthetic'],
        )
    db.insert_sentiment_point(
        conn,
        'symbol',
        'BTCUSDT',
        now - 3600,
        -0.2,
        0.0,
        1,
        {'test': True},
        ['synthetic'],
    )
    db.insert_sentiment_point(
        conn,
        'global',
        'crypto',
        now - 120,
        0.6,
        0.0,
        4,
        {'test': True},
        ['synthetic'],
    )

    symbol_map = compute_symbol_sentiment_map(conn, horizon_sec=6 * 3600)
    assert 'BTCUSDT' in symbol_map
    effective_score, bucket_count = symbol_map['BTCUSDT']
    assert bucket_count == 2
    assert effective_score > 0.0

    agg = compute_sentiment_agg(conn, scope='global', key='crypto')
    assert agg['data_quality']['has_data'] is True
    assert agg['regime'] in {'risk_on', 'neutral'}
    conn.close()


def test_sentiment_feature_aggregation_ignores_legacy_infinite_rows(tmp_path: Path, monkeypatch):
    conn = db.connect(str(tmp_path / "sentiment_inf_guard.db"))
    db.init_db(conn)
    now = 1_700_000_000
    monkeypatch.setattr('app.sentiment_features.time.time', lambda: now)

    conn.execute(
        """INSERT INTO sentiment(scope, key, ts, sentiment, velocity, volume, sources_json, tags_json)
               VALUES('global', 'crypto', ?, 1e999, 0.0, 1, '{}', '[]')""",
        (now - 60,),
    )
    db.insert_sentiment_point(
        conn,
        'global',
        'crypto',
        now - 30,
        0.2,
        0.0,
        3,
        {'test': True},
        ['synthetic'],
    )

    agg = compute_sentiment_agg(conn, scope='global', key='crypto')
    assert agg['n_points_7d'] == 1
    assert agg['effective_score'] == 0.2
    assert agg['regime'] == 'neutral'
    assert math.isfinite(agg['ewma']['1h'])
    assert math.isfinite(agg['ewma']['6h'])
    assert math.isfinite(agg['impulse']['v_1h'])
    conn.close()


def test_symbol_sentiment_map_ignores_legacy_infinite_rows(tmp_path: Path, monkeypatch):
    conn = db.connect(str(tmp_path / "symbol_sentiment_inf_guard.db"))
    db.init_db(conn)
    now = 1_700_000_000
    monkeypatch.setattr('app.sentiment_features.time.time', lambda: now)

    conn.execute(
        """INSERT INTO sentiment(scope, key, ts, sentiment, velocity, volume, sources_json, tags_json)
               VALUES('symbol', 'BTCUSDT', ?, 1e999, 0.0, 4, '{}', '[]')""",
        (now - 120,),
    )
    db.insert_sentiment_point(
        conn,
        'symbol',
        'BTCUSDT',
        now - 90,
        0.4,
        0.0,
        4,
        {'test': True},
        ['synthetic'],
    )

    symbol_map = compute_symbol_sentiment_map(conn, horizon_sec=6 * 3600)
    assert 'BTCUSDT' in symbol_map
    effective_score, bucket_count = symbol_map['BTCUSDT']
    assert bucket_count == 1
    assert effective_score == pytest.approx(0.4)
    assert math.isfinite(effective_score)
    conn.close()


def test_insert_sentiment_points_rejects_non_finite_and_negative_volume(tmp_path: Path):
    conn = db.connect(str(tmp_path / "sentiment_write_guard.db"))
    db.init_db(conn)

    with pytest.raises(ValueError):
        db.insert_sentiment_points(
            conn,
            [
                {
                    'scope': 'global',
                    'key': 'crypto',
                    'ts': 1_700_000_000,
                    'sentiment': float('inf'),
                    'velocity': 0.0,
                    'volume': 1,
                    'sources': {},
                    'tags': [],
                }
            ],
        )

    with pytest.raises(ValueError):
        db.insert_sentiment_point(
            conn,
            'global',
            'crypto',
            1_700_000_000,
            0.1,
            0.0,
            -1,
            {},
            [],
        )

    assert db.get_sentiment_series(conn, 'global', 'crypto', limit=10) == []
    conn.close()
