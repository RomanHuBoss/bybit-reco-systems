from __future__ import annotations

import time
from pathlib import Path

import pytest

from app import db
from app.main import _execution_symbol_direction_conflict_blocks


@pytest.fixture()
def conn(tmp_path: Path):
    path = tmp_path / "direction-conflict.db"
    conn = db.connect(str(path))
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


def _running_bot(
    *,
    bot_id: str,
    direction: str | None,
    origin_rec_id: str = "R-existing",
    publication_root_rec_id: str | None = None,
    venue: str = "linear",
    symbol: str = "BTCUSDT",
    bot_type: str = "futures_grid",
):
    mode = {"account_mode": "UNIFIED", "margin_mode": "cross"}
    if direction is not None:
        mode["direction"] = direction
    return {
        "bot_id": bot_id,
        "started_ts": int(time.time()),
        "stopped_ts": None,
        "venue": venue,
        "symbol": symbol,
        "bot_type": bot_type,
        "mode": mode,
        "params": {"grid_count": 8},
        "state": {"marker": bot_id},
        "status": "running",
        "origin_rec_id": origin_rec_id,
        "publication_root_rec_id": publication_root_rec_id or origin_rec_id,
    }


def _candidate(direction: str, *, rec_id: str = "R-new", root: str | None = None, venue: str = "linear", symbol: str = "BTCUSDT"):
    return {
        "rec_id": rec_id,
        "publication_root_rec_id": root or rec_id,
        "venue": venue,
        "symbol": symbol,
        "bot_type": "futures_grid",
        "direction": direction,
    }


def test_one_way_execution_blocks_opposite_direction_on_same_symbol(conn):
    assert db.insert_bot_instance(conn, _running_bot(bot_id="B-long", direction="long")) == "inserted"

    blocks = _execution_symbol_direction_conflict_blocks(conn, _candidate("short"))

    assert [block["code"] for block in blocks] == ["OPPOSITE_SYMBOL_DIRECTION_RUNNING"]
    assert blocks[0]["existing_direction"] == "long"
    assert blocks[0]["candidate_direction"] == "short"
    assert blocks[0]["bot_id"] == "B-long"


def test_one_way_execution_allows_same_direction_when_symbol_cap_permits(conn):
    assert db.insert_bot_instance(conn, _running_bot(bot_id="B-long", direction="long")) == "inserted"

    assert _execution_symbol_direction_conflict_blocks(conn, _candidate("long")) == []


def test_one_way_execution_treats_neutral_as_incompatible_with_directional_bot(conn):
    assert db.insert_bot_instance(conn, _running_bot(bot_id="B-neutral", direction="neutral")) == "inserted"

    blocks = _execution_symbol_direction_conflict_blocks(conn, _candidate("long"))

    assert [block["code"] for block in blocks] == ["OPPOSITE_SYMBOL_DIRECTION_RUNNING"]
    assert blocks[0]["existing_direction"] == "neutral"
    assert blocks[0]["candidate_direction"] == "long"


def test_one_way_execution_skips_same_publication_root_only_for_same_direction_reattach(conn):
    assert (
        db.insert_bot_instance(
            conn,
            _running_bot(
                bot_id="B-chain",
                direction="long",
                origin_rec_id="R-old",
                publication_root_rec_id="ROOT-1",
            ),
        )
        == "inserted"
    )

    assert _execution_symbol_direction_conflict_blocks(conn, _candidate("long", rec_id="R-new", root="ROOT-1")) == []


def test_one_way_execution_blocks_same_publication_root_direction_flip(conn):
    assert (
        db.insert_bot_instance(
            conn,
            _running_bot(
                bot_id="B-chain-long",
                direction="long",
                origin_rec_id="R-old",
                publication_root_rec_id="ROOT-FLIP",
            ),
        )
        == "inserted"
    )

    blocks = _execution_symbol_direction_conflict_blocks(conn, _candidate("short", rec_id="R-new", root="ROOT-FLIP"))

    assert [block["code"] for block in blocks] == ["OPPOSITE_SYMBOL_DIRECTION_RUNNING"]
    assert blocks[0]["existing_direction"] == "long"
    assert blocks[0]["candidate_direction"] == "short"
    assert blocks[0]["same_publication_root"] is True


def test_one_way_execution_blocks_when_existing_bot_direction_is_unknown(conn):
    assert db.insert_bot_instance(conn, _running_bot(bot_id="B-unknown", direction=None)) == "inserted"

    blocks = _execution_symbol_direction_conflict_blocks(conn, _candidate("long"))

    assert [block["code"] for block in blocks] == ["EXISTING_SYMBOL_DIRECTION_UNKNOWN"]
    assert blocks[0]["existing_direction"] is None
    assert blocks[0]["candidate_direction"] == "long"


def test_one_way_execution_ignores_other_symbols_and_venues(conn):
    assert db.insert_bot_instance(conn, _running_bot(bot_id="B-eth", direction="short", symbol="ETHUSDT")) == "inserted"
    assert db.insert_bot_instance(conn, _running_bot(bot_id="B-spot", direction="short", venue="spot", origin_rec_id="R-spot")) == "inserted"

    assert _execution_symbol_direction_conflict_blocks(conn, _candidate("long", symbol="BTCUSDT")) == []
