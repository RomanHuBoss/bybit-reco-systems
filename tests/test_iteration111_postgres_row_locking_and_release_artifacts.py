from __future__ import annotations

from pathlib import Path

from app import db
from app.db_backend import POSTGRES


class _FakeCursor:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class _FakePostgresConn:
    db_engine = POSTGRES

    def __init__(self):
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params=()):
        self.calls.append((str(sql), tuple(params)))
        normalized = " ".join(str(sql).split()).lower()
        if "from recommendations" in normalized:
            return _FakeCursor(
                {
                    "rec_id": "R-1",
                    "ts": 1,
                    "venue": "linear",
                    "symbol": "BTCUSDT",
                    "bot_type": "futures_grid",
                    "direction": "long",
                    "account_mode": "unified",
                    "margin_mode": "isolated",
                    "score": 0.7,
                    "confidence": 0.8,
                    "expected_rr": 1.5,
                    "risk_score": 0.2,
                    "params_json": "{}",
                    "reasons_json": "{}",
                    "blocks_json": "[]",
                    "status": "recommended",
                    "ttl_sec": 600,
                    "model_version": "test",
                    "features_ref_ts": 1,
                    "publication_root_rec_id": "R-1",
                    "is_outcome_label_root": 1,
                }
            )
        if "from bot_instances" in normalized:
            return _FakeCursor(
                {
                    "bot_id": "B-1",
                    "started_ts": 1,
                    "stopped_ts": None,
                    "venue": "linear",
                    "symbol": "BTCUSDT",
                    "bot_type": "futures_grid",
                    "mode_json": "{}",
                    "params_json": "{}",
                    "state_json": "{}",
                    "status": "running",
                    "origin_rec_id": "R-1",
                    "publication_root_rec_id": "R-1",
                }
            )
        raise AssertionError(f"unexpected SQL: {sql}")


def test_get_recommendation_and_bot_instance_support_postgres_row_locking() -> None:
    conn = _FakePostgresConn()

    rec = db.get_recommendation_by_id(conn, "R-1", for_update=True)
    bot = db.get_bot_instance(conn, "B-1", for_update=True)

    assert rec is not None
    assert bot is not None
    assert any("FROM recommendations WHERE rec_id=? FOR UPDATE" in sql for sql, _ in conn.calls)
    assert any("FROM bot_instances WHERE bot_id=? FOR UPDATE" in sql for sql, _ in conn.calls)


def test_standalone_migrations_include_publication_root_running_guards() -> None:
    root = Path(__file__).resolve().parents[1]
    sqlite_sql = (root / "migrations" / "init.sql").read_text(encoding="utf-8")
    postgres_sql = (root / "migrations" / "init_postgres.sql").read_text(encoding="utf-8")

    for payload in (sqlite_sql, postgres_sql):
        assert "idx_bot_publication_root_status" in payload
        assert "idx_bot_running_publication_root_unique" in payload
        assert "publication_root_rec_id" in payload


def test_audit_reports_exist_for_release_history() -> None:
    root = Path(__file__).resolve().parents[1]
    assert (root / "docs" / "AUDIT_REPORT_2026-04-15.md").exists()
    assert (root / "docs" / "AUDIT_REPORT_2026-04-10.md").exists()
    assert (root / "docs" / "AUDIT_REPORT_2026-04-08.md").exists()
