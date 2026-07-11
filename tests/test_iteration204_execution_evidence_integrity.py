from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db


@pytest.fixture()
def conn(tmp_path: Path):
    connection = db.connect(str(tmp_path / "execution-evidence.db"))
    db.init_db(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def client_and_conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "execution-evidence-api.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    sys.modules.pop("app.main", None)
    app_main = importlib.import_module("app.main")
    app_main.app.router.on_startup.clear()
    connection = db.connect(str(db_path))
    db.init_db(connection)
    client = TestClient(app_main.app)
    try:
        yield client, connection
    finally:
        client.close()
        connection.close()


def _seed_recommendation_and_bot(conn, *, stopped: bool = False) -> tuple[str, str, int]:
    ts = int(time.time()) - 600
    rec_id = "R-evidence-204"
    bot_id = "B-evidence-204"
    db.insert_recommendations(
        conn,
        [
            {
                "rec_id": rec_id,
                "ts": ts,
                "venue": "linear",
                "symbol": "BTCUSDT",
                "bot_type": "futures_grid",
                "direction": "long",
                "account_mode": "one_way",
                "margin_mode": "isolated",
                "score": 0.61,
                "confidence": 0.72,
                "expected_rr": 1.5,
                "risk_score": 0.2,
                "params": {},
                "reasons": {},
                "blocks": [],
                "status": "executed",
                "ttl_sec": 3600,
                "model_version": "test",
                "features_ref_ts": ts - 60,
                "publication_root_rec_id": rec_id,
                "is_outcome_label_root": 1,
            }
        ],
    )
    db.insert_bot_instance(
        conn,
        {
            "bot_id": bot_id,
            "started_ts": ts + 60,
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


def test_execution_evidence_net_pnl_includes_fee_funding_without_double_counting_slippage(conn) -> None:
    rec_id, bot_id, ts = _seed_recommendation_and_bot(conn)

    execution = {
        "event_id": "EV-204-exec",
        "bot_id": bot_id,
        "origin_rec_id": rec_id,
        "ts": ts + 120,
        "symbol": "BTCUSDT",
        "event_type": "execution",
        "source": "bybit_execution",
        "external_event_id": "exec-204-1",
        "external_order_id": "order-204-1",
        "side": "Sell",
        "qty": 0.1,
        "price": 101.0,
        "order_price": 100.0,
        "benchmark_price": 106.0,
        "benchmark_ts": ts + 119,
        "benchmark_source": "pre_submit_mid",
        "gross_pnl": 10.0,
        "fee": 2.0,
        "funding": 0.0,
        "slippage": 0.5,
        "currency": "USDT",
        "meta": {"is_maker": False},
    }
    funding = {
        "event_id": "EV-204-funding",
        "bot_id": bot_id,
        "origin_rec_id": rec_id,
        "ts": ts + 180,
        "symbol": "BTCUSDT",
        "event_type": "funding",
        "source": "bybit_transaction_log",
        "external_event_id": "txn-204-1",
        "external_order_id": None,
        "side": None,
        "qty": None,
        "price": None,
        "gross_pnl": 0.0,
        "fee": 0.0,
        "funding": -1.0,
        "slippage": 0.0,
        "currency": "USDT",
        "meta": {},
    }

    assert db.insert_execution_event(conn, execution) == "inserted"
    assert db.insert_execution_event(conn, funding) == "inserted"
    assert db.insert_execution_event(conn, dict(execution)) == "duplicate"

    summary = db.get_bot_execution_summary(conn, bot_id)
    assert summary["event_count"] == 2
    assert summary["execution_count"] == 1
    assert summary["funding_event_count"] == 1
    assert summary["realized_pnl_gross"] == pytest.approx(10.0)
    assert summary["realized_fee"] == pytest.approx(2.0)
    assert summary["realized_funding"] == pytest.approx(-1.0)
    assert summary["realized_slippage"] == pytest.approx(0.5)
    assert summary["realized_pnl_net"] == pytest.approx(7.0)

    changed = dict(execution)
    changed["event_id"] = "EV-204-exec-retry"
    changed["fee"] = 1.0
    with pytest.raises(ValueError, match="external_event_id"):
        db.insert_execution_event(conn, changed)


def test_live_validation_records_are_linked_to_immutable_rec_id(conn) -> None:
    rec_id, bot_id, ts = _seed_recommendation_and_bot(conn, stopped=True)
    db.insert_execution_event(
        conn,
        {
            "event_id": "EV-204-validation",
            "bot_id": bot_id,
            "origin_rec_id": rec_id,
            "ts": ts + 120,
            "symbol": "BTCUSDT",
            "event_type": "execution",
            "source": "bybit_execution",
            "external_event_id": "exec-204-validation",
            "external_order_id": "order-204-validation",
            "side": "Sell",
            "qty": 0.1,
            "price": 101.0,
            "order_price": 100.0,
            "benchmark_price": 103.0,
            "benchmark_ts": ts + 119,
            "benchmark_source": "pre_submit_mid",
            "gross_pnl": -4.0,
            "fee": 0.5,
            "funding": 0.0,
            "slippage": 0.2,
            "currency": "USDT",
            "meta": {},
        },
    )

    records = db.list_live_validation_records(conn, limit=10)
    record = next(item for item in records if item["bot_id"] == bot_id)
    assert record["rec_id"] == rec_id
    assert record["publication_root_rec_id"] == rec_id
    assert record["confidence"] == pytest.approx(0.72)
    assert record["realized_pnl_net"] == pytest.approx(-4.5)
    assert record["evidence_grade"] is True


def test_execution_evidence_api_rejects_incomplete_bybit_execution(client_and_conn) -> None:
    client, conn = client_and_conn
    rec_id, bot_id, ts = _seed_recommendation_and_bot(conn)

    response = client.post(
        f"/api/v1/bots/{bot_id}/execution-evidence",
        json={
            "event_id": "EV-api-204",
            "event_type": "execution",
            "source": "bybit_execution",
            "external_event_id": "exec-api-204",
            "ts": ts + 120,
            "gross_pnl": 1.0,
            "fee": 0.1,
            "funding": 0.0,
            "slippage": 0.0,
            "currency": "USDT",
        },
        headers={"X-API-Key": "test-admin-key"},
    )
    assert response.status_code == 422
    assert "external_order_id" in str(response.json())



def test_legacy_trade_net_includes_funding_and_reports_slippage_without_double_counting(conn) -> None:
    rec_id, bot_id, ts = _seed_recommendation_and_bot(conn)
    db.insert_trade(
        conn,
        {
            "trade_id": "T-legacy-costs-204",
            "bot_id": bot_id,
            "ts": ts + 120,
            "symbol": "BTCUSDT",
            "pnl": 10.0,
            "fee": 2.0,
            "funding": -1.0,
            "slippage": 0.5,
            "meta": {},
        },
    )
    summary = db.get_bot_trade_summary(conn, bot_id)
    assert summary["realized_funding"] == pytest.approx(-1.0)
    assert summary["realized_slippage"] == pytest.approx(0.5)
    assert summary["realized_pnl_net"] == pytest.approx(7.0)
    assert db.sum_daily_pnl(conn, ts) == pytest.approx(7.0)

def test_risk_stream_prefers_execution_evidence_without_double_counting(conn) -> None:
    from app.risk import compute_risk_status

    rec_id, bot_id, ts = _seed_recommendation_and_bot(conn)
    db.insert_execution_event(
        conn,
        {
            "event_id": "EV-risk-204",
            "bot_id": bot_id,
            "origin_rec_id": rec_id,
            "ts": ts + 180,
            "symbol": "BTCUSDT",
            "event_type": "execution",
            "source": "bybit_execution",
            "external_event_id": "exec-risk-204",
            "external_order_id": "order-risk-204",
            "side": "Sell",
            "qty": 0.1,
            "price": 100.0,
            "order_price": 99.0,
            "benchmark_price": 105.0,
            "benchmark_ts": ts + 179,
            "benchmark_source": "pre_submit_mid",
            "gross_pnl": -10.0,
            "fee": 1.0,
            "funding": 0.0,
            "slippage": 0.5,
            "currency": "USDT",
            "meta": {},
        },
    )
    # Simulate a pre-upgrade/corrupted mixed database by bypassing the supported
    # insert API. Risk accounting must still prefer exact evidence and not double count.
    conn.execute(
        """INSERT INTO trades(trade_id, bot_id, ts, symbol, pnl, fee, funding, slippage, meta_json)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        ("T-shadow-legacy-204", bot_id, ts + 120, "BTCUSDT", 100.0, 0.0, 0.0, 0.0, "{}"),
    )
    conn.commit()

    events = db.list_realized_net_events(conn, since_ts=ts)
    assert [event["source"] for event in events] == ["execution_evidence"]
    assert events[0]["net_pnl"] == pytest.approx(-11.0)
    assert db.sum_daily_pnl(conn, ts) == pytest.approx(-11.0)

    status = compute_risk_status(
        conn,
        {"cooldown_after_loss_min": 30, "max_daily_dd_usdt": 1_000.0},
    )
    assert status.daily_pnl == pytest.approx(-11.0)
    assert status.daily_dd == pytest.approx(11.0)
    assert status.cooldown_active is True


def test_execution_slippage_is_derived_and_mixed_ledgers_fail_closed(conn) -> None:
    rec_id, bot_id, ts = _seed_recommendation_and_bot(conn)
    base = {
        "event_id": "EV-derived-204",
        "bot_id": bot_id,
        "origin_rec_id": rec_id,
        "ts": ts + 120,
        "symbol": "BTCUSDT",
        "event_type": "execution",
        "source": "bybit_execution",
        "external_event_id": "exec-derived-204",
        "external_order_id": "order-derived-204",
        "side": "Buy",
        "qty": 2.0,
        "price": 101.5,
        "gross_pnl": 0.0,
        "fee": 0.2,
        "funding": 0.0,
        "currency": "USDT",
        "meta": {},
    }
    with pytest.raises(ValueError, match="order_price"):
        db.insert_execution_event(conn, base)

    complete = {
        **base,
        "order_price": 102.0,
        "benchmark_price": 101.0,
        "benchmark_ts": ts + 119,
        "benchmark_source": "pre_submit_mid",
    }
    mismatched = {**complete, "slippage": 0.2}
    with pytest.raises(ValueError, match="slippage does not match"):
        db.insert_execution_event(conn, mismatched)

    db.insert_trade(
        conn,
        {
            "trade_id": "T-before-evidence-204",
            "bot_id": bot_id,
            "ts": ts + 100,
            "symbol": "BTCUSDT",
            "pnl": 0.0,
            "fee": 0.0,
            "funding": 0.0,
            "slippage": 0.0,
            "meta": {},
        },
    )
    with pytest.raises(ValueError, match="cannot mix"):
        db.insert_execution_event(conn, complete)


def test_existing_sqlite_schema_is_upgraded_additively(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "legacy-v1.0.15.db"
    initial = db.connect(str(db_path))
    db.init_db(initial)
    initial.close()

    raw = sqlite3.connect(db_path)
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.executescript(
        """
        DROP INDEX IF EXISTS idx_execution_evidence_external_unique;
        DROP INDEX IF EXISTS idx_execution_evidence_bot_ts;
        DROP INDEX IF EXISTS idx_execution_evidence_rec_ts;
        DROP TABLE execution_evidence;
        DROP TABLE trades;
        CREATE TABLE trades(
          trade_id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, ts INTEGER NOT NULL,
          symbol TEXT NOT NULL, pnl REAL NOT NULL, fee REAL NOT NULL, meta_json TEXT NOT NULL
        );
        CREATE TABLE execution_evidence(
          event_id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, origin_rec_id TEXT NOT NULL,
          ts INTEGER NOT NULL, symbol TEXT NOT NULL, event_type TEXT NOT NULL, source TEXT NOT NULL,
          external_event_id TEXT NOT NULL, external_order_id TEXT, side TEXT, qty REAL, price REAL,
          gross_pnl REAL NOT NULL, fee REAL NOT NULL, funding REAL NOT NULL, slippage REAL NOT NULL,
          currency TEXT NOT NULL, meta_json TEXT NOT NULL
        );
        """
    )
    raw.commit()
    raw.close()

    upgraded = db.connect(str(db_path))
    db.init_db(upgraded)
    try:
        trade_columns = db._table_columns(upgraded, "trades")
        evidence_columns = db._table_columns(upgraded, "execution_evidence")
    finally:
        upgraded.close()
    assert {"funding", "slippage"}.issubset(trade_columns)
    assert {"order_price", "benchmark_price", "benchmark_ts", "benchmark_source"}.issubset(evidence_columns)


def test_exact_execution_evidence_read_endpoints_require_admin_key(client_and_conn) -> None:
    client, _conn = client_and_conn
    for path in ("/api/v1/execution-evidence", "/api/v1/validation/live-evidence"):
        denied = client.get(path)
        assert denied.status_code == 401
        allowed = client.get(path, headers={"X-API-Key": "test-admin-key"})
        assert allowed.status_code == 200


def test_release_builder_excludes_runtime_lock_database(tmp_path: Path) -> None:
    from scripts.build_release import build_release
    from zipfile import ZipFile

    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "release.zip"
    build_release(root, output)
    with ZipFile(output) as archive:
        names = archive.namelist()
    assert all(not name.endswith("data/app.runtime_locks.sqlite") for name in names)
    assert all(not name.endswith(".db") and not name.endswith(".db-wal") and not name.endswith(".db-shm") for name in names)
