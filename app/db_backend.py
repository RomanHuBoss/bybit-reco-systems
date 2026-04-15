from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.pq import TransactionStatus as PgTransactionStatus
except Exception:  # pragma: no cover - optional dependency at runtime
    psycopg = None
    dict_row = None
    PgTransactionStatus = None

SQLITE = "sqlite"
POSTGRES = "postgresql"

_POSTGRES_DSN_PREFIXES = ("postgres://", "postgresql://")

_POSTGRES_UPSERT_KEYS: dict[str, tuple[str, ...]] = {
    "app_config": ("key",),
    "features": ("venue", "symbol", "ts"),
    "funding_rate": ("symbol", "ts"),
    "market_regime": ("ts",),
    "ohlcv": ("venue", "symbol", "tf_sec", "ts"),
    "open_interest": ("symbol", "ts"),
    "recommendations": ("rec_id",),
    "reco_outcomes": ("rec_id",),
    "runtime_locks": ("lock_key",),
    "sentiment": ("scope", "key", "ts"),
    "ticker_snap": ("venue", "symbol", "ts"),
}


def is_postgres_target(target: str | None) -> bool:
    value = str(target or "").strip().lower()
    return value.startswith(_POSTGRES_DSN_PREFIXES)


def postgres_driver_required_error() -> RuntimeError:
    return RuntimeError(
        "PostgreSQL mode requires installed package 'psycopg[binary]'. "
        "Install runtime dependencies via `pip install -r requirements.txt` "
        "or switch configuration to DB_ENGINE=sqlite."
    )


if psycopg is not None:
    OPERATIONAL_ERRORS = (sqlite3.OperationalError, psycopg.OperationalError)
    INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)
else:  # pragma: no cover - exercised only when psycopg missing
    OPERATIONAL_ERRORS = (sqlite3.OperationalError,)
    INTEGRITY_ERRORS = (sqlite3.IntegrityError,)


class PostgresCursor:
    def __init__(self, cursor: Any):
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        try:
            return int(self._cursor.rowcount)
        except Exception:
            return -1

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def close(self) -> None:
        try:
            self._cursor.close()
        except Exception:
            pass


class PostgresConnection:
    db_engine = POSTGRES

    def __init__(self, dsn: str):
        if psycopg is None:  # pragma: no cover - exercised when optional dependency is absent
            raise postgres_driver_required_error()
        self._dsn = str(dsn)
        self._conn = psycopg.connect(self._dsn, autocommit=False, row_factory=dict_row)

    @property
    def in_transaction(self) -> bool:
        try:
            return self._conn.info.transaction_status != PgTransactionStatus.IDLE
        except Exception:
            return False

    def execute(self, sql: str, params: Iterable[Any] | None = None):
        translated_sql, translated_params = translate_sql(sql, params, engine=POSTGRES)
        cur = self._conn.cursor()
        cur.execute(translated_sql, translated_params)
        return PostgresCursor(cur)

    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]):
        translated_sql, _ = translate_sql(sql, (), engine=POSTGRES)
        cur = self._conn.cursor()
        cur.executemany(translated_sql, list(seq_of_params))
        return PostgresCursor(cur)

    def executescript(self, script: str) -> None:
        for statement in split_sql_script(script):
            translated_sql, translated_params = translate_sql(statement, (), engine=POSTGRES)
            if not translated_sql.strip():
                continue
            cur = self._conn.cursor()
            try:
                cur.execute(translated_sql, translated_params)
            finally:
                cur.close()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


def connect(target: str):
    if is_postgres_target(target):
        return PostgresConnection(str(target))

    db_path = Path(str(target)).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=60.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=60000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
    except Exception:
        pass
    return conn


def split_sql_script(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    prev = ""
    for ch in str(script or ""):
        if ch == "'" and not in_double and prev != "\\":
            in_single = not in_single
        elif ch == '"' and not in_single and prev != "\\":
            in_double = not in_double
        if ch == ";" and not in_single and not in_double:
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        else:
            current.append(ch)
        prev = ch
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def translate_sql(sql: str, params: Iterable[Any] | None = None, *, engine: str | None = None) -> tuple[str, tuple[Any, ...]]:
    target_engine = str(engine or SQLITE).strip().lower()
    parameters = tuple(params or ())
    if target_engine != POSTGRES:
        return sql, parameters

    text = str(sql or "").strip()
    if not text:
        return text, parameters

    pragma_table = _translate_pragma_table_info(text)
    if pragma_table is not None:
        return pragma_table, parameters

    if text.upper().startswith("PRAGMA "):
        return "SELECT 1", ()

    if re.fullmatch(r"BEGIN\s+IMMEDIATE", text, flags=re.IGNORECASE):
        return "BEGIN", ()

    text = _translate_insert_or_replace(text)
    text = _translate_json_extract(text)
    text = _replace_qmark_placeholders(text)
    return text, parameters


def describe_target(target: str) -> str:
    value = str(target or "")
    if not is_postgres_target(value):
        try:
            return str(Path(value).resolve())
        except Exception:
            return value
    return re.sub(r":([^:@/]+)@", ":***@", value, count=1)


def _translate_pragma_table_info(sql: str) -> str | None:
    match = re.fullmatch(r"PRAGMA\s+table_info\(([^)]+)\)", sql.strip(), flags=re.IGNORECASE)
    if not match:
        return None
    table = match.group(1).strip().strip("'\"")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise ValueError(f"Unsupported table name in PRAGMA table_info: {table}")
    return (
        "SELECT column_name AS name "
        "FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = '" + table + "' "
        "ORDER BY ordinal_position"
    )


def _translate_insert_or_replace(sql: str) -> str:
    match = re.fullmatch(
        r"INSERT\s+OR\s+REPLACE\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)\s*VALUES\s*\((.*)\)\s*",
        sql.strip().rstrip(";"),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return sql

    table = match.group(1)
    columns = [part.strip() for part in match.group(2).split(",") if part.strip()]
    values_sql = match.group(3).strip()
    conflict_cols = list(_POSTGRES_UPSERT_KEYS.get(table, ()))
    if not conflict_cols:
        raise ValueError(f"No PostgreSQL upsert mapping configured for table: {table}")

    updates = [f"{col}=EXCLUDED.{col}" for col in columns if col not in conflict_cols]
    if updates:
        conflict_action = "DO UPDATE SET " + ", ".join(updates)
    else:
        conflict_action = "DO NOTHING"

    return (
        f"INSERT INTO {table}({', '.join(columns)}) VALUES({values_sql}) "
        f"ON CONFLICT ({', '.join(conflict_cols)}) {conflict_action}"
    )


def _translate_json_extract(sql: str) -> str:
    pattern = re.compile(
        r"json_extract\(\s*([A-Za-z_][A-Za-z0-9_\.]*?)\s*,\s*'\$\.([A-Za-z0-9_\.]+)'\s*\)",
        flags=re.IGNORECASE,
    )

    def _replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        path = match.group(2).split(".")
        pg_path = ",".join(path)
        return f"({expr}::jsonb #>> '{{{pg_path}}}')"

    return pattern.sub(_replace, sql)


def _replace_qmark_placeholders(sql: str) -> str:
    out: list[str] = []
    in_single = False
    in_double = False
    prev = ""
    for ch in sql:
        if ch == "'" and not in_double and prev != "\\":
            in_single = not in_single
            out.append(ch)
        elif ch == '"' and not in_single and prev != "\\":
            in_double = not in_double
            out.append(ch)
        elif ch == "?" and not in_single and not in_double:
            out.append("%s")
        else:
            out.append(ch)
        prev = ch
    return "".join(out)
