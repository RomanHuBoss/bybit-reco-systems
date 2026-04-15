from __future__ import annotations

import importlib
import sys

import pytest

from app import db
from app.db_backend import POSTGRES, PostgresConnection, postgres_driver_required_error, translate_sql


@pytest.fixture()
def reload_settings_module():
    sys.modules.pop("app.settings", None)
    module = importlib.import_module("app.settings")
    return module


def test_load_settings_supports_postgres_mode(monkeypatch: pytest.MonkeyPatch, reload_settings_module) -> None:
    monkeypatch.setenv("DB_ENGINE", "postgresql")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@127.0.0.1:5432/bybit_reco")
    monkeypatch.delenv("RUNTIME_LOCK_DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("RUNTIME_LOCK_DB_PATH", raising=False)

    settings = reload_settings_module.load_settings()

    assert settings.db_engine == POSTGRES
    assert settings.db_path == "postgresql://user:secret@127.0.0.1:5432/bybit_reco"
    assert settings.runtime_lock_db_path == settings.db_path


def test_load_settings_requires_explicit_database_url(monkeypatch: pytest.MonkeyPatch, reload_settings_module) -> None:
    monkeypatch.setenv("DB_ENGINE", "postgresql")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="DATABASE_URL is required when DB_ENGINE=postgresql"):
        reload_settings_module.load_settings()


def test_load_settings_supports_dedicated_postgres_runtime_lock_db(monkeypatch: pytest.MonkeyPatch, reload_settings_module) -> None:
    monkeypatch.setenv("DB_ENGINE", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@127.0.0.1:5432/bybit_reco")
    monkeypatch.setenv("RUNTIME_LOCK_DATABASE_URL", "postgresql://user:secret@127.0.0.1:5432/bybit_reco_locks")

    settings = reload_settings_module.load_settings()

    assert settings.db_engine == POSTGRES
    assert settings.runtime_lock_db_path.endswith("/bybit_reco_locks")


def test_runtime_lock_db_path_keeps_postgres_dsn_unchanged() -> None:
    dsn = "postgresql://user:secret@127.0.0.1:5432/bybit_reco"
    assert db.runtime_lock_db_path(dsn) == dsn


def test_translate_sql_converts_insert_or_replace_for_postgres() -> None:
    sql, params = translate_sql(
        "INSERT OR REPLACE INTO app_config(key, value_json, updated_ts) VALUES(?,?,?)",
        ("a", "{}", 1),
        engine=POSTGRES,
    )

    assert params == ("a", "{}", 1)
    assert "INSERT INTO app_config" in sql
    assert "ON CONFLICT (key) DO UPDATE SET" in sql
    assert "%s" in sql


def test_translate_sql_converts_json_extract_for_postgres() -> None:
    sql, _ = translate_sql(
        "SELECT LOWER(COALESCE(json_extract(r.reasons_json, '$.llm_review.status'), '')) = 'ok'",
        (),
        engine=POSTGRES,
    )

    assert "r.reasons_json::jsonb #>> '{llm_review,status}'" in sql
    assert "json_extract" not in sql


def test_translate_sql_converts_sqlite_pragma_table_info_for_postgres() -> None:
    sql, params = translate_sql("PRAGMA table_info(recommendations)", (), engine=POSTGRES)

    assert params == ()
    assert "information_schema.columns" in sql
    assert "table_name = 'recommendations'" in sql


def test_postgres_connection_without_psycopg_has_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.db_backend.psycopg", None)

    with pytest.raises(RuntimeError) as excinfo:
        PostgresConnection("postgresql://user:secret@127.0.0.1:5432/bybit_reco")

    msg = str(excinfo.value)
    assert "psycopg[binary]" in msg
    assert "pip install -r requirements.txt" in msg
    assert "DB_ENGINE=sqlite" in msg


def test_postgres_driver_required_error_mentions_safe_recovery_path() -> None:
    msg = str(postgres_driver_required_error())
    assert "psycopg[binary]" in msg
    assert "DB_ENGINE=sqlite" in msg
