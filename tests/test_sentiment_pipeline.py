from __future__ import annotations

import math
import pytest
from pathlib import Path

from app import db
from app.sentiment import (
    _score_text,
    blend_per_symbol,
    collect_sentiment_once,
    combine_global_sentiment,
    fetch_fear_greed,
    fetch_reddit_sentiment,
    global_market_momentum_point,
)
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


class _DummyResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _DummyClient:
    def __init__(self, payload):
        self.payload = payload

    def get(self, *args, **kwargs):
        return _DummyResponse(self.payload)


def test_fetch_fear_greed_ignores_non_finite_upstream_payload(monkeypatch):
    monkeypatch.setattr('app.sentiment.time.time', lambda: 1_700_000_000)

    point = fetch_fear_greed(_DummyClient({'data': [{'value': 'NaN', 'value_classification': 'bad'}]}))

    assert point is None


def test_blend_per_symbol_skips_poisoned_sources_instead_of_creating_fake_extreme(monkeypatch):
    monkeypatch.setattr('app.sentiment.time.time', lambda: 1_700_000_100)

    blended = blend_per_symbol(
        rss_map={'BTCUSDT': [0.2, float('nan')]},
        reddit_map={'BTCUSDT': {'sentiment': float('nan')}},
        trending_map={'BTCUSDT': {'sentiment': 0.6}},
        momentum_map={'BTCUSDT': {'sentiment': 0.4, 'velocity': float('nan')}},
    )

    assert 'BTCUSDT' in blended
    point = blended['BTCUSDT']
    assert point['sentiment'] == pytest.approx((0.4 * 0.45 + 0.2 * 0.15 + 0.6 * 0.10) / (0.45 + 0.15 + 0.10))
    assert point['velocity'] == 0.0
    assert point['sources']['reddit'] is None
    assert point['sources']['rss_mentions'] == 1
    assert point['sources']['sources_used'] == ['momentum', 'rss', 'trending']
    assert math.isfinite(point['sentiment'])


def test_combine_global_sentiment_skips_non_finite_source_rows():
    combined = combine_global_sentiment([
        {
            'scope': 'global',
            'key': 'broken',
            'sentiment': float('nan'),
            'volume': float('nan'),
            'sources': {'broken': True},
            'tags': ['broken'],
        },
        {
            'scope': 'global',
            'key': 'market',
            'sentiment': 0.25,
            'volume': 4,
            'sources': {'market': True},
            'tags': ['market_momentum'],
        },
    ])

    assert combined is not None
    assert combined['sentiment'] == pytest.approx(0.25)
    assert combined['volume'] == 6
    assert combined['sources'] == {'market': {'market': True}}
    assert combined['tags'] == ['market_momentum']

def test_fetch_reddit_sentiment_skips_poisoned_posts_but_keeps_valid_neighbors(monkeypatch):
    monkeypatch.setattr('app.sentiment.REDDIT_RSS', {'BTCUSDT': 'https://reddit.test/btc'})
    monkeypatch.setattr('app.sentiment.time.time', lambda: 1_700_000_200)

    client = _DummyClient({
        'data': {
            'children': [
                'broken-row',
                {'data': 'bad-payload'},
                {
                    'data': {
                        'title': 'bull breakout',
                        'selftext': 'record growth',
                        'upvote_ratio': 0.9,
                    }
                },
            ]
        }
    })

    points = fetch_reddit_sentiment(client)

    assert 'BTCUSDT' in points
    point = points['BTCUSDT']
    assert point['volume'] == 1
    assert point['sources']['posts_analyzed'] == 1
    assert point['sentiment'] > 0.0


def test_global_market_momentum_point_skips_non_dict_entries():
    point = global_market_momentum_point({
        'BROKEN': 'not-a-dict',
        'BTCUSDT': {'sentiment': 0.4, 'volume': 25_000_000},
        'ETHUSDT': {'sentiment': float('nan'), 'volume': 5_000_000},
    })

    assert point is not None
    assert point['sentiment'] == pytest.approx(0.4)
    assert point['sources'] == {'symbols_used': 1}
    assert point['volume'] == 1


def test_blend_per_symbol_skips_non_dict_source_blocks(monkeypatch):
    monkeypatch.setattr('app.sentiment.time.time', lambda: 1_700_000_250)

    blended = blend_per_symbol(
        rss_map={'BTCUSDT': [0.1]},
        reddit_map={'BTCUSDT': 'broken'},
        trending_map={'BTCUSDT': ['broken-list']},
        momentum_map={'BTCUSDT': {'sentiment': 0.5, 'velocity': 0.3}},
    )

    assert 'BTCUSDT' in blended
    point = blended['BTCUSDT']
    assert point['sentiment'] == pytest.approx((0.5 * 0.45 + 0.1 * 0.15) / (0.45 + 0.15))
    assert point['velocity'] == pytest.approx(0.3)
    assert point['sources']['reddit'] is None
    assert point['sources']['trending'] is False
    assert point['sources']['sources_used'] == ['momentum', 'rss']


def test_combine_global_sentiment_skips_non_dict_source_rows_too():
    combined = combine_global_sentiment([
        'broken-row',
        {
            'scope': 'global',
            'key': 'market',
            'sentiment': 0.15,
            'volume': 3,
            'sources': {'market': True},
            'tags': ['market_momentum'],
        },
    ])

    assert combined is not None
    assert combined['sentiment'] == pytest.approx(0.15)
    assert combined['volume'] == 6
    assert combined['sources'] == {'market': {'market': True}}


def test_collect_sentiment_once_tolerates_malformed_adapter_returns(monkeypatch):
    monkeypatch.setattr('app.sentiment.fetch_fear_greed', lambda client: {'scope': 'global', 'key': 'fng', 'sentiment': 0.2, 'volume': 1, 'sources': {'fng': 60}, 'tags': ['fear_greed'], 'ts': 1_700_000_300, 'velocity': 0.0})
    monkeypatch.setattr('app.sentiment.fetch_rss_sentiment', lambda client: ({'scope': 'global', 'key': 'rss', 'sentiment': -0.1, 'volume': 5, 'sources': {'rss': True}, 'tags': ['news_rss'], 'ts': 1_700_000_300, 'velocity': 0.0}, {'BTCUSDT': [0.2]}))
    monkeypatch.setattr('app.sentiment.fetch_reddit_sentiment', lambda client: 'broken')
    monkeypatch.setattr('app.sentiment.fetch_coingecko_trending', lambda client: ['broken'])
    monkeypatch.setattr('app.sentiment.fetch_coingecko_momentum', lambda client: {'BTCUSDT': {'sentiment': 0.4, 'velocity': 0.1, 'volume': 1000, 'sources': {'cg': True}, 'tags': ['coingecko_momentum'], 'scope': 'symbol', 'key': 'BTCUSDT', 'ts': 1_700_000_300}})
    monkeypatch.setattr('app.sentiment.time.time', lambda: 1_700_000_300)

    points = collect_sentiment_once()

    globals_ = [p for p in points if p.get('scope') == 'global']
    symbols = [p for p in points if p.get('scope') == 'symbol']
    assert any(p.get('key') == 'fng' for p in globals_)
    assert any(p.get('key') == 'rss' for p in globals_)
    assert any(p.get('key') == 'crypto_market_momentum' for p in globals_)
    assert any(p.get('key') == 'crypto' for p in globals_)
    blended = next(p for p in symbols if p.get('key') == 'BTCUSDT')
    assert blended['sources']['sources_used'] == ['momentum', 'rss']
    assert math.isfinite(blended['sentiment'])

