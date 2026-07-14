from __future__ import annotations

import json
import math
import sqlite3
import secrets
import time
from pathlib import Path
from typing import Any, Iterable
from .bot_types import is_supported_bot_type, sql_in_clause
from .grid_math import strict_integer
from .policy import canonical_policy_fingerprint, is_sha256_fingerprint
from .db_backend import connect as backend_connect, describe_target, is_postgres_target, OPERATIONAL_ERRORS, INTEGRITY_ERRORS, POSTGRES, SQLITE
import logging

logger = logging.getLogger(__name__)

MARKET_DATA_MAX_FUTURE_SKEW_SEC = 300
LATEST_ROW_SCAN_LIMIT = 1024

ACTIONABLE_RECOMMENDATION_STATUSES: frozenset[str] = frozenset({"recommended", "active"})
VISIBLE_RECOMMENDATION_STATUSES: frozenset[str] = ACTIONABLE_RECOMMENDATION_STATUSES
ACTIVE_PUBLICATION_STATUSES: frozenset[str] = frozenset({"recommended", "active", "executed"})
EXPIRABLE_RECOMMENDATION_STATUSES: frozenset[str] = frozenset({"recommended", "active", "pending"})
LLM_OUTCOME_READY_STATUSES: frozenset[str] = frozenset({"ok"})


def is_actionable_recommendation_status(status: Any) -> bool:
    return str(status or "").strip().lower() in ACTIONABLE_RECOMMENDATION_STATUSES


def is_expirable_recommendation_status(status: Any) -> bool:
    return str(status or "").strip().lower() in EXPIRABLE_RECOMMENDATION_STATUSES


def _explicit_true(value: Any) -> bool:
    """Parse only explicit true values; unknown/ambiguous persistence data is false."""
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


MIGRATION_INIT_SQL = Path(__file__).resolve().parent.parent / "migrations" / "init.sql"

def runtime_lock_db_path(db_path: str) -> str:
    if is_postgres_target(db_path):
        return str(db_path)
    base = Path(str(db_path)).expanduser()
    return str(base.with_name(f"{base.stem}.runtime_locks.sqlite"))


def connect(db_path: str):
    try:
        return backend_connect(db_path)
    except Exception:
        logger.debug("db connect failed for %s", describe_target(db_path), exc_info=True)
        raise


def connect_runtime_locks(db_path: str):
    return connect(runtime_lock_db_path(db_path))


def begin_immediate(conn: sqlite3.Connection) -> None:
    """Start a write transaction eagerly to serialize mutating API flows.

    SQLite defaults to DEFERRED transactions, which means two writers can both read
    the same pre-update state and only contend later on the first write. For API
    paths that change bot/recommendation lifecycle state, we want to lock the write
    side before making any business-logic decisions so the read-check-write sequence
    stays coherent.
    """
    try:
        in_txn = bool(getattr(conn, "in_transaction", False))
    except Exception:
        in_txn = False
    if not in_txn:
        conn.execute("BEGIN IMMEDIATE")

def _json_dumps_safe(value: Any, *, canonical: bool = False) -> str:
    """JSON-сериализация, не допускающая non-finite числа.

    SQLite хранит JSON как TEXT, поэтому `NaN`/`Infinity` легко проскальзывают как
    невалидный с точки зрения RFC payload и потом начинают по-разному вести себя
    в Python, HTTP-ответах и JS-клиенте. Для проектной целостности храним только
    стандартный JSON и fail-closed на любом non-finite значении.
    """
    kwargs = {
        "ensure_ascii": False,
        "allow_nan": False,
    }
    if canonical:
        kwargs.update({"sort_keys": True, "separators": (",", ":")})
    try:
        return json.dumps(value, **kwargs)
    except (TypeError, ValueError) as exc:
        raise ValueError("json payload must not contain non-finite numbers") from exc


def _json_dumps_canonical(value: Any) -> str:
    """Стабильная JSON-сериализация для идемпотентных сравнений.

    Для audit/idempotency нам важна семантика payload, а не случайный порядок
    ключей в Python-словаре или в повторном HTTP-запросе. Поэтому все JSON-поля,
    участвующие в duplicate-detection, приводим к каноническому виду.
    Одновременно запрещаем `NaN`/`Infinity`, чтобы duplicate-detection не работал
    поверх невалидного JSON.
    """
    return _json_dumps_safe(value, canonical=True)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {str(row["name"]) for row in cur.fetchall()}


def _ensure_recommendation_publication_columns(conn: sqlite3.Connection) -> None:
    cols = _table_columns(conn, "recommendations")
    if "publication_root_rec_id" not in cols:
        conn.execute("ALTER TABLE recommendations ADD COLUMN publication_root_rec_id TEXT")
    if "is_outcome_label_root" not in cols:
        conn.execute("ALTER TABLE recommendations ADD COLUMN is_outcome_label_root INTEGER NOT NULL DEFAULT 1")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reco_publication_root_ts ON recommendations(publication_root_rec_id, ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reco_outcome_root_ts ON recommendations(is_outcome_label_root, ts DESC)")


def _normalize_bot_publication_root(value: Any) -> str | None:
    root = str(value or "").strip()
    return root or None


def _bot_publication_root_backfill_needed(conn: sqlite3.Connection) -> bool:
    """Проверяет, есть ли реально незаполненные publication roots у ботов.

    Полный backfill всех bot_instances на каждом старте плохо масштабируется: на
    живой базе это превращает обычный перезапуск в дорогой исторический re-scan.
    Для штатного пути достаточно дешёвой проверки `LIMIT 1`; полную ретро-правку
    запускаем только если действительно нашли legacy-строки без materialized root.
    """
    cur = conn.execute(
        """SELECT 1
               FROM bot_instances
              WHERE publication_root_rec_id IS NULL
                 OR TRIM(COALESCE(publication_root_rec_id, '')) = ''
              LIMIT 1"""
    )
    return cur.fetchone() is not None



def _ensure_funding_rate_columns(conn: sqlite3.Connection) -> None:
    cols = _table_columns(conn, "funding_rate")
    if "funding_interval_min" not in cols:
        conn.execute("ALTER TABLE funding_rate ADD COLUMN funding_interval_min REAL")


def _ensure_trade_cost_columns(conn: sqlite3.Connection) -> None:
    cols = _table_columns(conn, "trades")
    if "funding" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN funding REAL NOT NULL DEFAULT 0")
    if "slippage" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN slippage REAL NOT NULL DEFAULT 0")


def _ensure_execution_evidence_columns(conn: sqlite3.Connection) -> None:
    """Apply additive upgrades to an already-created evidence ledger."""
    cols = _table_columns(conn, "execution_evidence")
    if "order_price" not in cols:
        conn.execute("ALTER TABLE execution_evidence ADD COLUMN order_price REAL")
    if "benchmark_price" not in cols:
        conn.execute("ALTER TABLE execution_evidence ADD COLUMN benchmark_price REAL")
    if "benchmark_ts" not in cols:
        conn.execute("ALTER TABLE execution_evidence ADD COLUMN benchmark_ts BIGINT")
    if "benchmark_source" not in cols:
        conn.execute("ALTER TABLE execution_evidence ADD COLUMN benchmark_source TEXT")


def _ensure_outcome_label_availability_column(conn: sqlite3.Connection) -> None:
    cols = _table_columns(conn, "reco_outcomes")
    if "label_available_ts" not in cols:
        # Nullable by design: legacy labels do not expose their exact first
        # tradeable candle, so fabricating a timestamp would reintroduce leakage.
        conn.execute("ALTER TABLE reco_outcomes ADD COLUMN label_available_ts BIGINT")


def _ensure_bot_publication_root_columns(conn: sqlite3.Connection) -> None:
    cols = _table_columns(conn, "bot_instances")
    if "publication_root_rec_id" not in cols:
        conn.execute("ALTER TABLE bot_instances ADD COLUMN publication_root_rec_id TEXT")
    if _bot_publication_root_backfill_needed(conn):
        _backfill_bot_publication_root(conn)
    duplicates = _find_running_publication_root_duplicates(conn)
    if duplicates:
        raise RuntimeError(
            "Duplicate running bots detected for publication roots: "
            + ", ".join(f"{root} (count={count})" for root, count in duplicates)
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bot_publication_root_status "
        "ON bot_instances(publication_root_rec_id, status, started_ts DESC)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_running_publication_root_unique "
        "ON bot_instances(publication_root_rec_id) "
        "WHERE publication_root_rec_id IS NOT NULL AND status='running'"
    )


def _backfill_bot_publication_root(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "SELECT bot_id, origin_rec_id, publication_root_rec_id FROM bot_instances ORDER BY started_ts ASC, bot_id ASC"
    )
    rows = cur.fetchall()
    if not rows:
        return 0

    cache: dict[str, str | None] = {}
    updates: list[tuple[str | None, str]] = []
    for row in rows:
        current_root = _normalize_bot_publication_root(row["publication_root_rec_id"])
        if current_root is not None:
            continue
        origin_rec_id = str(row["origin_rec_id"] or "").strip()
        root_id: str | None = None
        if origin_rec_id:
            if origin_rec_id not in cache:
                rec = get_recommendation_by_id(conn, origin_rec_id)
                cache[origin_rec_id] = _normalize_bot_publication_root(
                    (rec or {}).get("publication_root_rec_id") or origin_rec_id
                )
            root_id = cache.get(origin_rec_id)
        if root_id is None:
            root_id = origin_rec_id or None
        updates.append((root_id, row["bot_id"]))

    if updates:
        conn.executemany(
            "UPDATE bot_instances SET publication_root_rec_id=? WHERE bot_id=?",
            updates,
        )
    return len(updates)


def _find_running_publication_root_duplicates(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    cur = conn.execute(
        """SELECT publication_root_rec_id, COUNT(*) AS c
               FROM bot_instances
              WHERE status='running'
                AND publication_root_rec_id IS NOT NULL
                AND TRIM(publication_root_rec_id) <> ''
              GROUP BY publication_root_rec_id
            HAVING COUNT(*) > 1
              ORDER BY publication_root_rec_id ASC"""
    )
    out: list[tuple[str, int]] = []
    for row in cur.fetchall():
        out.append((str(row["publication_root_rec_id"]), int(row["c"])))
    return out


def _recommendation_publication_backfill_needed(conn: sqlite3.Connection) -> bool:
    """Быстрая проверка, нужен ли legacy-backfill publication lineage.

    На реальной базе рекомендаций исторический полный проход по всем строкам на
    каждом `python main.py` быстро становится узким местом. Для обычного рестарта
    нам важно понять только одно: остались ли строки без materialized lineage.
    Если нет, тяжёлый Python backfill пропускаем.
    """
    cur = conn.execute(
        """SELECT 1
               FROM recommendations
              WHERE publication_root_rec_id IS NULL
                 OR TRIM(COALESCE(publication_root_rec_id, '')) = ''
                 OR is_outcome_label_root IS NULL
              LIMIT 1"""
    )
    return cur.fetchone() is not None



def backfill_recommendation_publication_lineage(conn: sqlite3.Connection) -> int:
    cols = _table_columns(conn, "recommendations")
    if "publication_root_rec_id" not in cols or "is_outcome_label_root" not in cols:
        return 0

    cur = conn.execute(
        """SELECT rec_id, ts, reasons_json, publication_root_rec_id, is_outcome_label_root
               FROM recommendations
               ORDER BY ts ASC, rec_id ASC"""
    )
    rows = cur.fetchall()
    if not rows:
        return 0

    lineage_by_rec_id: dict[str, str] = {}
    updates: list[tuple[str, int, str]] = []

    for row in rows:
        rec_id = str(row["rec_id"] or "")
        reasons = _json_loads_mapping_or_default(row["reasons_json"], {})
        dedupe = reasons.get("publication_dedupe") if isinstance(reasons, dict) else {}
        if not isinstance(dedupe, dict):
            dedupe = {}
        previous_rec_id = str(dedupe.get("previous_rec_id") or "").strip()
        active_reuse = _explicit_true(dedupe.get("active_reuse")) or str(dedupe.get("decision") or "").strip().lower() == "reuse_active"

        root_rec_id = rec_id
        is_label_root = 1
        if active_reuse and previous_rec_id:
            root_rec_id = lineage_by_rec_id.get(previous_rec_id, previous_rec_id)
            is_label_root = 0

        lineage_by_rec_id[rec_id] = root_rec_id

        current_root = str(row["publication_root_rec_id"] or "").strip()
        try:
            current_label_root = int(row["is_outcome_label_root"] or 0)
        except Exception:
            current_label_root = 0
        if current_root != root_rec_id or current_label_root != is_label_root:
            updates.append((root_rec_id, is_label_root, rec_id))

    if updates:
        conn.executemany(
            "UPDATE recommendations SET publication_root_rec_id=?, is_outcome_label_root=? WHERE rec_id=?",
            updates,
        )
    return len(updates)



def _recommendation_pending_hold_target(reasons_json: str | None) -> str:
    reasons = _json_loads_mapping_or_default(reasons_json, {})
    llm_review = reasons.get("llm_review") if isinstance(reasons.get("llm_review"), dict) else {}
    return str(llm_review.get("publish_target_status") or "").strip().lower()



def _is_publication_actionable_status(status: Any, reasons_json: str | None) -> bool:
    status_norm = str(status or "").strip().lower()
    if status_norm in ACTIVE_PUBLICATION_STATUSES:
        return True
    if status_norm != "pending":
        return False
    return _recommendation_pending_hold_target(reasons_json) in ACTIONABLE_RECOMMENDATION_STATUSES



def _backfill_first_tradeable_1m_candle_ts(conn: sqlite3.Connection, venue: str, symbol: str, ts_after: int) -> int | None:
    cur = conn.execute(
        """SELECT ts FROM ohlcv
           WHERE venue=? AND symbol=? AND tf_sec=60 AND ts>?
           ORDER BY ts ASC LIMIT 1""",
        (venue, symbol, int(ts_after)),
    )
    row = cur.fetchone()
    if not row:
        return None
    try:
        return int(row["ts"])
    except Exception:
        return None



def _backfill_effective_horizon_sec(bot_type: str, params: dict[str, Any] | None, fallback_horizon_sec: int = 12 * 3600) -> int:
    params = params if isinstance(params, dict) else {}

    def _hours_to_sec(value: Any) -> int | None:
        hours = strict_integer(value)
        if hours is None or hours <= 0:
            return None
        return int(hours * 3600)

    def _bounded_hours(hours: float) -> float:
        bounds = {
            "futures_grid": (6.0, 48.0),
        }
        lo, hi = bounds.get(bot_type, (0.5, 72.0))
        return max(lo, min(hi, float(hours)))

    trade_plan = params.get("trade_plan") if isinstance(params.get("trade_plan"), dict) else {}
    expected_horizon = trade_plan.get("expected_horizon") if isinstance(trade_plan.get("expected_horizon"), dict) else {}

    explicit_hours = (
        params.get("label_horizon_hours")
        or trade_plan.get("label_horizon_hours")
        or expected_horizon.get("label_horizon_hours")
    )
    explicit_sec = _hours_to_sec(explicit_hours)
    if explicit_sec is not None:
        return int(_bounded_hours(explicit_sec / 3600.0) * 3600)

    builtin = {"futures_grid": 12 * 3600}.get(bot_type)
    if builtin is not None:
        return int(builtin)

    max_sec = _hours_to_sec(expected_horizon.get("max_hours"))
    if max_sec is not None:
        return int(_bounded_hours(max_sec / 3600.0) * 3600)
    return int(fallback_horizon_sec)



def repair_async_llm_pending_publication_chains(conn: sqlite3.Connection) -> int:
    """Retrofit lineage for roots duplicated while async LLM review kept prior rows pending.

    Historical bug: publish-time dedupe ignored the previous cycle's `pending` rows even
    when they were only waiting for async LLM review and would later be restored to
    `recommended`/`active`. That allowed a new same-direction root every minute or two.
    We repair such rows by reusing the earlier open publication chain until its label
    horizon expires or an outcome is already present.
    """
    cols = _table_columns(conn, "recommendations")
    if "publication_root_rec_id" not in cols or "is_outcome_label_root" not in cols:
        return 0

    cur = conn.execute(
        """SELECT r.rec_id, r.ts, r.venue, r.symbol, r.bot_type, r.direction,
                     r.status, r.reasons_json, r.params_json, r.features_ref_ts,
                     r.publication_root_rec_id, r.is_outcome_label_root,
                     CASE WHEN o.rec_id IS NULL THEN 0 ELSE 1 END AS has_outcome
              FROM recommendations r
              LEFT JOIN reco_outcomes o ON o.rec_id = r.rec_id
              ORDER BY r.ts ASC, r.rec_id ASC"""
    )
    rows = cur.fetchall()
    if not rows:
        return 0

    active_chains: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    updates: list[tuple[str, int, str]] = []

    for row in rows:
        venue = str(row["venue"] or "")
        symbol = str(row["symbol"] or "")
        bot_type = str(row["bot_type"] or "")
        direction = str(row["direction"] or "neutral")
        rec_id = str(row["rec_id"] or "")
        ts = int(row["ts"] or 0)
        key = (venue, symbol, bot_type, direction)

        prev = active_chains.get(key)
        if prev is not None and ts >= int(prev.get("lock_until_ts") or 0):
            active_chains.pop(key, None)
            prev = None

        current_root = str(row["publication_root_rec_id"] or rec_id).strip() or rec_id
        try:
            current_is_root = int(row["is_outcome_label_root"] or 0)
        except Exception:
            current_is_root = 0

        actionable = _is_publication_actionable_status(row["status"], row["reasons_json"])
        if not actionable:
            continue

        if prev is not None:
            desired_root = str(prev.get("root_rec_id") or prev.get("rec_id") or rec_id)
            desired_is_root = 1 if rec_id == desired_root else 0
            if current_root != desired_root or current_is_root != desired_is_root:
                updates.append((desired_root, desired_is_root, rec_id))
            continue

        params = _json_loads_mapping_or_default(row["params_json"], {})
        signal_ref_ts = max(int(row["features_ref_ts"] or 0), ts)
        tradeable_ts = _backfill_first_tradeable_1m_candle_ts(conn, venue, symbol, signal_ref_ts)
        pseudo_entry_ts = int(tradeable_ts) if tradeable_ts is not None else int(signal_ref_ts) + 60
        effective_horizon_sec = _backfill_effective_horizon_sec(bot_type, params)
        lock_until_ts = int(pseudo_entry_ts) + int(effective_horizon_sec)

        desired_root = current_root or rec_id
        desired_is_root = 1 if desired_root == rec_id else 0
        if current_root != desired_root or current_is_root != desired_is_root:
            updates.append((desired_root, desired_is_root, rec_id))
        if desired_is_root:
            active_chains[key] = {
                "rec_id": rec_id,
                "root_rec_id": desired_root,
                "lock_until_ts": int(lock_until_ts),
            }

    if not updates:
        return 0
    conn.executemany(
        "UPDATE recommendations SET publication_root_rec_id=?, is_outcome_label_root=? WHERE rec_id=?",
        updates,
    )
    return len(updates)


def init_db(conn: sqlite3.Connection) -> None:
    migration_path = MIGRATION_INIT_SQL
    if getattr(conn, "db_engine", "sqlite") == POSTGRES:
        migration_path = MIGRATION_INIT_SQL.with_name("init_postgres.sql")
    sql = migration_path.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.execute("""CREATE TABLE IF NOT EXISTS runtime_locks (
      lock_key TEXT PRIMARY KEY,
      owner TEXT NOT NULL,
      heartbeat_ts INTEGER NOT NULL
    )""")
    _ensure_recommendation_publication_columns(conn)
    _ensure_funding_rate_columns(conn)
    _ensure_trade_cost_columns(conn)
    _ensure_execution_evidence_columns(conn)
    _ensure_outcome_label_availability_column(conn)
    if _recommendation_publication_backfill_needed(conn):
        # Полный historical lineage backfill может занимать заметное время на живой
        # БД. На штатном рестарте запускаем его только если реально нашли legacy-
        # строки без materialized root/is_root. Глубокий async-LLM retrofit больше
        # не делаем автоматически на старте: он остаётся отдельной maintenance-
        # операцией через `repair_async_llm_pending_publication_chains()`.
        backfill_recommendation_publication_lineage(conn)
    _ensure_bot_publication_root_columns(conn)
    conn.commit()

def init_runtime_lock_db(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS runtime_locks (
      lock_key TEXT PRIMARY KEY,
      owner TEXT NOT NULL,
      heartbeat_ts INTEGER NOT NULL
    )""")
    conn.commit()


def _exception_sqlstate(exc: Exception) -> str:
    value = getattr(exc, "sqlstate", None)
    if value:
        return str(value).strip().upper()
    diag = getattr(exc, "diag", None)
    value = getattr(diag, "sqlstate", None) if diag is not None else None
    return str(value or "").strip().upper()


def _is_lock_retryable_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    sqlstate = _exception_sqlstate(exc)
    return (
        sqlstate in {"40P01", "40001", "55P03"}
        or "database is locked" in msg
        or "database table is locked" in msg
        or "busy" in msg
        or "deadlock detected" in msg
        or "взаимоблок" in msg
        or "could not serialize access" in msg
        or "lock timeout" in msg
        or "lock not available" in msg
    )


def _execute_lock_write_with_retry(op, *, attempts: int = 6, sleep_sec: float = 0.05):
    last_exc = None
    for attempt in range(max(1, int(attempts))):
        try:
            return op()
        except OPERATIONAL_ERRORS as exc:
            last_exc = exc
            if not _is_lock_retryable_error(exc) or attempt + 1 >= max(1, int(attempts)):
                raise
            time.sleep(float(sleep_sec) * (attempt + 1))
    if last_exc is not None:
        raise last_exc


def now_ts() -> int:
    return int(time.time())


def _is_plausible_market_ts(ts: int, *, max_future_skew_sec: int = MARKET_DATA_MAX_FUTURE_SKEW_SEC) -> bool:
    try:
        ts_int = int(ts)
    except Exception:
        return False
    if ts_int <= 0:
        return False
    return ts_int <= now_ts() + max(0, int(max_future_skew_sec))


def _json_loads_or_default(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        if isinstance(raw, str):
            # Python stdlib по умолчанию принимает NaN/Infinity как валидный JSON.
            # Для audit/UI/business-logic чтения это опасно: poisoned legacy payload
            # может не просто дожить до API-ответа, а изменить решение движка.
            # Здесь сохраняем остальную структуру, но non-finite токены гасим в None.
            loaded = json.loads(raw, parse_constant=lambda _token: None)
        else:
            loaded = raw
    except Exception:
        return default
    return loaded


def _json_loads_mapping_or_default(raw: Any, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Возвращает только JSON-object.

    Для operator/UI-facing структур важно не просто пережить битый JSON, но и не
    выпускать наружу неожиданную форму данных вроде list/str вместо dict. Иначе
    downstream-код начинает падать уже не на этапе чтения БД, а сильно позже — в
    UI, API или duplicate-detection ветках.
    """
    loaded = _json_loads_or_default(raw, None)
    if isinstance(loaded, dict):
        return dict(loaded)
    return dict(default or {})


def _json_loads_list_or_default(raw: Any, default: list[Any] | None = None) -> list[Any]:
    loaded = _json_loads_or_default(raw, None)
    if isinstance(loaded, list):
        return list(loaded)
    return list(default or [])


def _json_loads_list_of_mappings_or_default(raw: Any, default: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    return [dict(item) for item in _json_loads_list_or_default(raw, default) if isinstance(item, dict)]


def _json_loads_text_list_or_default(raw: Any, default: list[str] | None = None) -> list[str]:
    items = _json_loads_list_or_default(raw, None)
    if not items:
        return list(default or [])
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out if out else list(default or [])


def _finite_float_or_default(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(default)
    try:
        num = float(value)
    except Exception:
        return float(default)
    if not math.isfinite(num):
        return float(default)
    return num


def _require_finite_float(name: str, value: Any, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        num = float(value)
    except Exception as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(num):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and num < float(minimum):
        raise ValueError(f"{name} must be >= {minimum}")
    return num


def _require_non_negative_int(name: str, value: Any) -> int:
    num = strict_integer(value)
    if num is None:
        raise ValueError(f"{name} must be an integer >= 0")
    if num < 0:
        raise ValueError(f"{name} must be >= 0")
    return int(num)


def _require_positive_int(name: str, value: Any) -> int:
    num = strict_integer(value)
    if num is None or num <= 0:
        raise ValueError(f"{name} must be an integer > 0")
    return int(num)


def _savepoint_name(prefix: str = "sp") -> str:
    token = secrets.token_hex(6)
    return f"{prefix}_{token}"


def _begin_savepoint(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(f"SAVEPOINT {name}")


def _rollback_to_savepoint(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(f"ROLLBACK TO SAVEPOINT {name}")


def _release_savepoint(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(f"RELEASE SAVEPOINT {name}")


def _decode_sentiment_row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if not r:
        return None
    sentiment = _finite_float_or_default(r["sentiment"], float('nan'))
    velocity = _finite_float_or_default(r["velocity"], float('nan'))
    try:
        volume = int(r["volume"])
    except Exception:
        return None
    if not math.isfinite(sentiment) or not math.isfinite(velocity) or volume < 0:
        return None
    return {
        "scope": r["scope"],
        "key": r["key"],
        "ts": int(r["ts"]),
        "sentiment": float(max(-1.0, min(1.0, sentiment))),
        "velocity": float(velocity),
        "volume": int(volume),
        "sources": _json_loads_mapping_or_default(r["sources_json"], {}),
        "tags": _json_loads_text_list_or_default(r["tags_json"], []),
    }


def _commit_write_with_retry(
    conn: sqlite3.Connection,
    op,
    *,
    attempts: int = 6,
    sleep_sec: float = 0.05,
):
    last_exc = None
    for attempt in range(max(1, int(attempts))):
        try:
            result = op()
            conn.commit()
            return result
        except OPERATIONAL_ERRORS as exc:
            last_exc = exc
            if not _is_lock_retryable_error(exc) or attempt + 1 >= max(1, int(attempts)):
                raise
            try:
                conn.rollback()
            except Exception:
                logger.debug("rollback error", exc_info=True)
            time.sleep(float(sleep_sec) * (attempt + 1))
    if last_exc is not None:
        raise last_exc


def set_app_config_json(conn: sqlite3.Connection, key: str, value: Any, *, commit: bool = True) -> None:
    params = (str(key), _json_dumps_safe(value), now_ts())
    if commit:
        _commit_write_with_retry(
            conn,
            lambda: conn.execute(
                "INSERT OR REPLACE INTO app_config(key, value_json, updated_ts) VALUES(?,?,?)",
                params,
            ),
        )
        return
    conn.execute(
        "INSERT OR REPLACE INTO app_config(key, value_json, updated_ts) VALUES(?,?,?)",
        params,
    )


def get_app_config_json(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    cur = conn.execute("SELECT value_json FROM app_config WHERE key=?", (str(key),))
    row = cur.fetchone()
    if not row:
        return default
    try:
        return _json_loads_or_default(row["value_json"], default)
    except Exception:
        return default

def _canonical_ohlcv_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["venue"]), str(row["symbol"]), int(row["tf_sec"]), int(row["ts"]))
        deduped[key] = row
    return [deduped[key] for key in sorted(deduped)]


def upsert_ohlcv(conn: sqlite3.Connection, rows: list[dict[str, Any]], *, commit: bool = True) -> None:
    valid_rows = _canonical_ohlcv_rows([dict(r) for r in rows if _is_valid_ohlcv_row(r)])
    if not valid_rows:
        return
    params = [
        (r["venue"], r["symbol"], r["tf_sec"], r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"])
        for r in valid_rows
    ]
    sql = """INSERT OR REPLACE INTO ohlcv(venue,symbol,tf_sec,ts,open,high,low,close,volume)
           VALUES(?,?,?,?,?,?,?,?,?)"""

    def _op():
        return conn.executemany(sql, params)

    if commit:
        _commit_write_with_retry(conn, _op)
        return
    _op()

def insert_tickers(conn: sqlite3.Connection, rows: list[dict[str, Any]], *, commit: bool = True) -> None:
    valid_rows = [dict(r) for r in rows if _is_valid_ticker_row(r)]
    if not valid_rows:
        return
    conn.executemany(
        """INSERT OR REPLACE INTO ticker_snap(venue,symbol,ts,last,bid,ask,vol24h,turnover24h)
           VALUES(?,?,?,?,?,?,?,?)""",
        [(r["venue"], r["symbol"], r["ts"], r.get("last"), r.get("bid"), r.get("ask"), r.get("vol24h"), r.get("turnover24h")) for r in valid_rows],
    )
    if commit:
        conn.commit()

def insert_features(conn: sqlite3.Connection, venue: str, symbol: str, ts: int, features: dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO features(venue,symbol,ts,features_json) VALUES(?,?,?,?)""",
        (venue, symbol, ts, _json_dumps_safe(features)),
    )
    conn.commit()

def insert_regime(conn: sqlite3.Connection, ts: int, regime: dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO market_regime(ts, regime_json) VALUES(?,?)""",
        (ts, _json_dumps_safe(regime)),
    )
    conn.commit()

_RECOMMENDATION_COLUMNS: tuple[str, ...] = (
    "rec_id", "ts", "venue", "symbol", "bot_type", "direction", "account_mode", "margin_mode",
    "score", "confidence", "expected_rr", "risk_score",
    "params_json", "reasons_json", "blocks_json", "status", "ttl_sec", "model_version", "features_ref_ts",
    "publication_root_rec_id", "is_outcome_label_root",
)


def _normalize_outcome_root_flag(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    parsed = strict_integer(value)
    if parsed not in (0, 1):
        raise ValueError("recommendation.is_outcome_label_root must be boolean or exact 0/1")
    return int(parsed)


def _normalize_recommendation_number(name: str, value: Any) -> Any:
    """Reject Python booleans without breaking legacy poisoned-row tests.

    Older audit fixtures deliberately persist malformed TEXT in numeric SQLite
    columns to prove that downstream readers fail closed. Preserve that legacy
    test surface, while normalizing valid numeric strings and refusing actual
    bool/NaN/inf values that SQLite would silently coerce into plausible data.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name} must not be boolean")
    try:
        number = float(value)
    except Exception:
        return value
    if not math.isfinite(number):
        if isinstance(value, str):
            return value
        raise ValueError(f"{name} must be finite")
    return float(number)


def _normalize_recommendation_integer(name: str, value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError(f"{name} must not be boolean")
    parsed = strict_integer(value)
    if parsed is not None:
        if parsed <= 0:
            raise ValueError(f"{name} must be > 0")
        return int(parsed)
    if isinstance(value, (int, float)):
        raise ValueError(f"{name} must be an exact integer")
    # Legacy resilience tests intentionally store malformed TEXT and assert that
    # API/read paths sanitize it. Keep that compatibility while preventing bool
    # coercion at the persistence boundary.
    return value


def _normalize_recommendation_payload(r: dict[str, Any]) -> tuple[Any, ...]:
    rec_id = str(r["rec_id"]).strip()
    if not rec_id:
        raise ValueError("recommendation.rec_id must be non-empty")
    publication_root_rec_id = str(r.get("publication_root_rec_id") or rec_id).strip()
    if not publication_root_rec_id:
        raise ValueError("recommendation.publication_root_rec_id must be non-empty")
    is_outcome_label_root = _normalize_outcome_root_flag(
        r.get("is_outcome_label_root", publication_root_rec_id == rec_id)
    )
    return (
        rec_id,
        _normalize_recommendation_integer("recommendation.ts", r["ts"]),
        str(r["venue"]),
        str(r["symbol"]),
        str(r["bot_type"]),
        str(r["direction"]),
        str(r["account_mode"]),
        str(r["margin_mode"]),
        _normalize_recommendation_number("recommendation.score", r["score"]),
        _normalize_recommendation_number("recommendation.confidence", r["confidence"]),
        _normalize_recommendation_number("recommendation.expected_rr", r["expected_rr"]),
        _normalize_recommendation_number("recommendation.risk_score", r["risk_score"]),
        _json_dumps_canonical(r["params"]),
        _json_dumps_canonical(r["reasons"]),
        _json_dumps_canonical(r["blocks"]),
        str(r["status"]),
        _normalize_recommendation_integer("recommendation.ttl_sec", r["ttl_sec"]),
        str(r["model_version"]),
        _normalize_recommendation_integer("recommendation.features_ref_ts", r["features_ref_ts"]),
        publication_root_rec_id,
        is_outcome_label_root,
    )


def _normalize_stored_recommendation_payload(row: Any) -> tuple[Any, ...]:
    values = [row[column] for column in _RECOMMENDATION_COLUMNS]
    values[1] = _normalize_recommendation_integer("recommendation.ts", values[1])
    for idx, name in zip((8, 9, 10, 11), ("score", "confidence", "expected_rr", "risk_score")):
        values[idx] = _normalize_recommendation_number(f"recommendation.{name}", values[idx])
    for idx, default in ((12, {}), (13, {}), (14, [])):
        values[idx] = _json_dumps_canonical(_json_loads_or_default(values[idx], default))
    values[16] = _normalize_recommendation_integer("recommendation.ttl_sec", values[16])
    values[18] = _normalize_recommendation_integer("recommendation.features_ref_ts", values[18])
    values[20] = int(values[20])
    return tuple(values)


def insert_recommendations(conn: sqlite3.Connection, rows: list[dict[str, Any]], *, commit: bool = True) -> None:
    """Append immutable recommendation audit rows, allowing exact retry idempotency.

    A rec_id is an audit identity, not an upsert key. Replaying the exact payload is
    harmless; attempting to reuse the same id for different direction, economics,
    status or metadata fails closed and leaves the whole batch unchanged.
    """
    payload_by_id: dict[str, tuple[Any, ...]] = {}
    for raw in rows:
        payload = _normalize_recommendation_payload(raw)
        rec_id = str(payload[0])
        prior = payload_by_id.get(rec_id)
        if prior is not None and prior != payload:
            raise ValueError(f"rec_id={rec_id} appears multiple times with different payload")
        payload_by_id[rec_id] = payload
    if not payload_by_id:
        return

    placeholders = ",".join("?" for _ in _RECOMMENDATION_COLUMNS)
    columns = ",".join(_RECOMMENDATION_COLUMNS)
    insert_sql = (
        f"INSERT INTO recommendations({columns}) VALUES({placeholders}) "
        "ON CONFLICT(rec_id) DO NOTHING"
    )
    select_sql = f"SELECT {columns} FROM recommendations WHERE rec_id=?"
    savepoint = _savepoint_name("insert_recommendations")
    _begin_savepoint(conn, savepoint)
    try:
        for rec_id, payload in payload_by_id.items():
            conn.execute(insert_sql, payload)
            stored = conn.execute(select_sql, (rec_id,)).fetchone()
            if stored is None or _normalize_stored_recommendation_payload(stored) != payload:
                raise ValueError(f"rec_id={rec_id} already exists with different payload")
        _release_savepoint(conn, savepoint)
    except Exception:
        _rollback_to_savepoint(conn, savepoint)
        _release_savepoint(conn, savepoint)
        raise
    if commit:
        conn.commit()

def log_decision(
    conn: sqlite3.Connection,
    action: str,
    rec_id: str | None,
    operator: str | None | dict[str, Any],
    details: dict[str, Any] | None = None,
    *,
    commit: bool = True,
) -> None:
    # Backward-compatible guard: older call-sites used
    # log_decision(conn, action, rec_id, details). Treat that as operator=None
    # instead of failing during bootstrap/recovery paths.
    if details is None and isinstance(operator, dict):
        details = operator
        operator = None
    params = (now_ts(), action, rec_id, operator if isinstance(operator, str) else None, _json_dumps_safe(details or {}))
    if commit:
        _commit_write_with_retry(
            conn,
            lambda: conn.execute(
                """INSERT INTO decision_log(ts, action, rec_id, operator, details_json) VALUES(?,?,?,?,?)""",
                params,
            ),
        )
        return
    conn.execute(
        """INSERT INTO decision_log(ts, action, rec_id, operator, details_json) VALUES(?,?,?,?,?)""",
        params,
    )



def _is_valid_ticker_row(row: Any) -> bool:
    try:
        ts = strict_integer(row["ts"])
    except Exception:
        return False
    if ts is None or not _is_plausible_market_ts(ts):
        return False

    last = row["last"]
    bid = row["bid"]
    ask = row["ask"]
    turnover = row["turnover24h"]
    vol24h = row["vol24h"]

    def _optional_non_negative(value: Any, *, strictly_positive: bool = False) -> float | None:
        if value in (None, "") or isinstance(value, bool):
            return None
        try:
            num = float(value)
        except Exception:
            return None
        if not math.isfinite(num):
            return None
        if strictly_positive and num <= 0:
            return None
        if not strictly_positive and num < 0:
            return None
        return num

    last_num = _optional_non_negative(last, strictly_positive=True)
    if last not in (None, "") and last_num is None:
        return False
    bid_num = _optional_non_negative(bid, strictly_positive=True)
    ask_num = _optional_non_negative(ask, strictly_positive=True)
    if bid not in (None, "") and bid_num is None:
        return False
    if ask not in (None, "") and ask_num is None:
        return False
    if bid_num is not None and ask_num is not None and ask_num < bid_num:
        return False
    if turnover not in (None, "") and _optional_non_negative(turnover) is None:
        return False
    if vol24h not in (None, "") and _optional_non_negative(vol24h) is None:
        return False
    return True


def _sanitize_ticker_row(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = dict(row)
    try:
        ts = strict_integer(payload.get("ts"))
    except Exception:
        return None
    if ts is None or not _is_plausible_market_ts(ts):
        return None
    if not _is_valid_ticker_row(payload):
        # Historical rows may contain crossed quotes or non-finite values.
        # Keep the turnover/volume snapshot but drop the quote fields so cost-model
        # falls back to conservative defaults instead of treating bad quotes as free liquidity.
        bid = _finite_float_or_default(payload.get('bid'), float('nan'))
        ask = _finite_float_or_default(payload.get('ask'), float('nan'))
        last = _finite_float_or_default(payload.get('last'), float('nan'))
        if not math.isfinite(last) or last <= 0:
            payload['last'] = None
        if not (math.isfinite(bid) and bid > 0):
            payload['bid'] = None
        if not (math.isfinite(ask) and ask > 0):
            payload['ask'] = None
        if payload.get('bid') is not None and payload.get('ask') is not None and float(payload['ask']) < float(payload['bid']):
            payload['bid'] = None
            payload['ask'] = None
        if payload.get('vol24h') is not None and not math.isfinite(_finite_float_or_default(payload.get('vol24h'), float('nan'))):
            payload['vol24h'] = None
        if payload.get('turnover24h') is not None and not math.isfinite(_finite_float_or_default(payload.get('turnover24h'), float('nan'))):
            payload['turnover24h'] = None
    return payload

def _is_valid_ohlcv_row(row: Any) -> bool:
    numeric_fields = ("tf_sec", "ts", "open", "high", "low", "close", "volume")
    try:
        numeric_values = [row[key] for key in numeric_fields]
    except Exception:
        return False
    if any(isinstance(value, bool) for value in numeric_values):
        return False
    tf_sec = strict_integer(row["tf_sec"])
    ts = strict_integer(row["ts"])
    if tf_sec is None or tf_sec <= 0 or ts is None:
        return False
    try:
        open_px = float(row["open"])
        high_px = float(row["high"])
        low_px = float(row["low"])
        close_px = float(row["close"])
        volume = float(row["volume"] or 0.0)
    except Exception:
        return False
    if not _is_plausible_market_ts(ts):
        return False
    vals = (open_px, high_px, low_px, close_px, volume)
    if not all(math.isfinite(v) for v in vals):
        return False
    if min(open_px, high_px, low_px, close_px) <= 0:
        return False
    if volume < 0:
        return False
    if high_px < max(open_px, close_px, low_px):
        return False
    if low_px > min(open_px, close_px, high_px):
        return False
    return True


def get_latest_ohlcv(conn: sqlite3.Connection, venue: str, symbol: str, tf_sec: int, limit: int = 240) -> list[sqlite3.Row]:
    safe_limit = max(1, int(limit))
    fetch_limit = max(safe_limit, min(5000, safe_limit * 4))
    cur = conn.execute(
        """SELECT * FROM ohlcv WHERE venue=? AND symbol=? AND tf_sec=? ORDER BY ts DESC LIMIT ?""",
        (venue, symbol, tf_sec, fetch_limit),
    )
    # IMPORTANT CONTRACT:
    #   Returned rows are ordered newest -> oldest (ts DESC), matching the SQL.
    #   Callers that need oldest -> newest (e.g. indicator calculations) must reverse().
    #
    # Defensive filtering: historical DB rows may contain malformed OHLCV values from
    # prior builds or manual imports. Over-fetch before filtering so a cluster of bad
    # newest bars does not starve callers of older valid history.
    valid_rows = [row for row in cur.fetchall() if _is_valid_ohlcv_row(row)]
    return valid_rows[:safe_limit]


def get_latest_ohlcv_ts(conn: sqlite3.Connection, venue: str, symbol: str, tf_sec: int) -> int | None:
    cur = conn.execute(
        """SELECT * FROM ohlcv
           WHERE venue=? AND symbol=? AND tf_sec=?
           ORDER BY ts DESC
           LIMIT ?""",
        (venue, symbol, tf_sec, LATEST_ROW_SCAN_LIMIT),
    )
    for row in cur.fetchall():
        if _is_valid_ohlcv_row(row):
            return int(row["ts"])
    return None

def get_latest_ticker(conn: sqlite3.Connection, venue: str, symbol: str) -> dict[str, Any] | None:
    cur = conn.execute(
        """SELECT * FROM ticker_snap WHERE venue=? AND symbol=? ORDER BY ts DESC LIMIT ?""",
        (venue, symbol, LATEST_ROW_SCAN_LIMIT),
    )
    fallback: dict[str, Any] | None = None
    for row in cur.fetchall():
        payload = dict(row)
        if _is_valid_ticker_row(payload):
            return payload
        if fallback is None:
            fallback = _sanitize_ticker_row(payload)
    return fallback


def get_latest_ticker_ts(conn: sqlite3.Connection, venue: str, symbol: str) -> int | None:
    cur = conn.execute(
        """SELECT * FROM ticker_snap WHERE venue=? AND symbol=? ORDER BY ts DESC LIMIT ?""",
        (venue, symbol, LATEST_ROW_SCAN_LIMIT),
    )
    for row in cur.fetchall():
        payload = dict(row)
        if _is_valid_ticker_row(payload):
            try:
                return int(payload["ts"])
            except Exception:
                return None
    return None

def get_latest_features_ts(conn: sqlite3.Connection, venue: str, symbol: str) -> int | None:
    cur = conn.execute(
        """SELECT ts FROM features WHERE venue=? AND symbol=? ORDER BY ts DESC LIMIT 1""",
        (venue, symbol),
    )
    row = cur.fetchone()
    return int(row["ts"]) if row else None

def get_latest_features(conn: sqlite3.Connection, venue: str, symbol: str) -> dict[str, Any] | None:
    cur = conn.execute(
        """SELECT features_json FROM features WHERE venue=? AND symbol=? ORDER BY ts DESC LIMIT 1""",
        (venue, symbol),
    )
    row = cur.fetchone()
    return _json_loads_or_default(row["features_json"], None) if row else None

def get_active_bots(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    _supported_sql, _supported_params = sql_in_clause("bot_type")
    cur = conn.execute(
        f"""SELECT * FROM bot_instances
               WHERE status='running' AND {_supported_sql}""",
        _supported_params,
    )
    return list(cur.fetchall())


def count_active_bots_for_symbol(conn: sqlite3.Connection, venue: str, symbol: str) -> int:
    _supported_sql, _supported_params = sql_in_clause("bot_type")
    cur = conn.execute(
        f"""SELECT COUNT(1) AS c FROM bot_instances
               WHERE status='running' AND venue=? AND symbol=? AND {_supported_sql}""",
        (venue, symbol, *_supported_params),
    )
    return int(cur.fetchone()["c"])


def insert_bot_instance(conn: sqlite3.Connection, bot: dict[str, Any], *, commit: bool = True) -> str:
    """Insert a bot instance without replacing an existing logical bot.

    Returns:
      - ``inserted`` when a new row is written
      - ``duplicate_origin`` when another row with the same origin_rec_id already exists
      - ``duplicate_bot_id`` when the exact bot_id already exists with identical payload

    Raises:
      ValueError when the same bot_id already exists with different payload.
      integrity/backend error for other unexpected uniqueness conflicts.
    """
    payload = (
        bot["bot_id"],
        int(bot["started_ts"]),
        bot.get("stopped_ts"),
        bot["venue"],
        bot["symbol"],
        bot["bot_type"],
        _json_dumps_canonical(bot["mode"]),
        _json_dumps_canonical(bot["params"]),
        _json_dumps_canonical(bot["state"]),
        bot["status"],
        bot.get("origin_rec_id"),
        _normalize_bot_publication_root(bot.get("publication_root_rec_id") or bot.get("origin_rec_id")),
    )

    cur = conn.execute(
        """SELECT started_ts, stopped_ts, venue, symbol, bot_type,
                  mode_json, params_json, state_json, status, origin_rec_id, publication_root_rec_id
           FROM bot_instances WHERE bot_id=?""",
        (bot["bot_id"],),
    )
    row = cur.fetchone()
    if row:
        existing = (
            int(row["started_ts"]),
            row["stopped_ts"],
            row["venue"],
            row["symbol"],
            row["bot_type"],
            _json_dumps_canonical(_json_loads_mapping_or_default(row["mode_json"], {})),
            _json_dumps_canonical(_json_loads_mapping_or_default(row["params_json"], {})),
            _json_dumps_canonical(_json_loads_mapping_or_default(row["state_json"], {})),
            row["status"],
            row["origin_rec_id"],
            _normalize_bot_publication_root(row["publication_root_rec_id"]),
        )
        incoming = payload[1:]
        if existing == incoming:
            return "duplicate_bot_id"
        raise ValueError(f"bot_id={bot['bot_id']} already exists with different payload")

    savepoint = _savepoint_name("bot_insert")
    _begin_savepoint(conn, savepoint)
    try:
        conn.execute(
            """INSERT INTO bot_instances(
                bot_id, started_ts, stopped_ts, venue, symbol, bot_type,
                mode_json, params_json, state_json, status, origin_rec_id, publication_root_rec_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            payload,
        )
        _release_savepoint(conn, savepoint)
        if commit:
            conn.commit()
        return "inserted"
    except INTEGRITY_ERRORS:
        # PostgreSQL после IntegrityError переводит текущую транзакцию в failed
        # state и не даст даже SELECT до rollback/savepoint-rewind. Нам важно
        # классифицировать конфликт как duplicate_origin / duplicate_publication_root,
        # не откатывая всю внешнюю operator-транзакцию.
        _rollback_to_savepoint(conn, savepoint)
        _release_savepoint(conn, savepoint)
        origin_rec_id = bot.get("origin_rec_id")
        if origin_rec_id:
            existing = get_bot_by_origin_rec(conn, str(origin_rec_id))
            if existing is not None:
                return "duplicate_origin"
        publication_root_rec_id = _normalize_bot_publication_root(bot.get("publication_root_rec_id") or origin_rec_id)
        if publication_root_rec_id and str(bot.get("status") or "").strip().lower() == "running":
            existing_running = get_bot_by_publication_root(conn, publication_root_rec_id, status="running")
            if existing_running is not None:
                return "duplicate_publication_root_running"
        raise
    except Exception:
        _rollback_to_savepoint(conn, savepoint)
        _release_savepoint(conn, savepoint)
        raise

def stop_bot(conn: sqlite3.Connection, bot_id: str, *, stopped_ts: int | None = None, commit: bool = True) -> bool:
    cur = conn.execute("""SELECT bot_id FROM bot_instances WHERE bot_id=? AND status='running'""", (bot_id,))
    if not cur.fetchone():
        return False
    effective_stopped_ts = now_ts() if stopped_ts is None else _require_non_negative_int("stopped_ts", stopped_ts)
    conn.execute("""UPDATE bot_instances SET status='stopped', stopped_ts=? WHERE bot_id=?""", (effective_stopped_ts, bot_id))
    if commit:
        conn.commit()
    return True

def _decode_bot_row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if not r:
        return None
    return {
        "bot_id": r["bot_id"],
        "started_ts": r["started_ts"],
        "stopped_ts": r["stopped_ts"],
        "venue": r["venue"],
        "symbol": r["symbol"],
        "bot_type": r["bot_type"],
        "mode": _json_loads_mapping_or_default(r["mode_json"], {}),
        "params": _json_loads_mapping_or_default(r["params_json"], {}),
        "state": _json_loads_mapping_or_default(r["state_json"], {}),
        "status": r["status"],
        "origin_rec_id": r["origin_rec_id"],
        "publication_root_rec_id": _normalize_bot_publication_root(r["publication_root_rec_id"]),
    }


def get_bot_instance(conn: sqlite3.Connection, bot_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
    sql = "SELECT * FROM bot_instances WHERE bot_id=?"
    if for_update and getattr(conn, "db_engine", SQLITE) == POSTGRES:
        # Для PostgreSQL мутационные API-пути должны блокировать конкретную строку
        # bot_instances, иначе два одновременных trade/stop запроса могут прочитать
        # один и тот же state_json и затем перезаписать агрегаты друг друга.
        sql += " FOR UPDATE"
    cur = conn.execute(sql, (bot_id,))
    bot = _decode_bot_row(cur.fetchone())
    if bot and not is_supported_bot_type(bot.get("bot_type")):
        return None
    return bot


def get_bot_by_origin_rec(conn: sqlite3.Connection, origin_rec_id: str) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM bot_instances WHERE origin_rec_id=? ORDER BY started_ts DESC LIMIT 1", (origin_rec_id,))
    return _decode_bot_row(cur.fetchone())


def get_bot_by_publication_root(
    conn: sqlite3.Connection,
    publication_root_rec_id: str,
    *,
    status: str | None = None,
) -> dict[str, Any] | None:
    """Return the newest bot for a publication chain.

    Предпочитаем materialized ``publication_root_rec_id`` в ``bot_instances``. Это
    делает idempotency независимой от join к recommendations и позволяет держать
    инвариант "не более одного running bot на одну publication-chain" прямо на
    уровне БД. Для старых БД с ещё не заполненной колонкой оставляем fallback через
    join к recommendations.
    """
    root_id = str(publication_root_rec_id or "").strip()
    if not root_id:
        return None
    cols = _table_columns(conn, "bot_instances")
    if "publication_root_rec_id" in cols:
        sql = "SELECT * FROM bot_instances WHERE publication_root_rec_id=?"
        params: list[Any] = [root_id]
        if status is not None:
            sql += " AND status=?"
            params.append(str(status))
        sql += " ORDER BY started_ts DESC LIMIT 1"
        cur = conn.execute(sql, params)
        bot = _decode_bot_row(cur.fetchone())
        if bot is not None:
            return bot
    sql = """SELECT b.*
               FROM bot_instances b
               JOIN recommendations r ON r.rec_id = b.origin_rec_id
              WHERE COALESCE(NULLIF(TRIM(r.publication_root_rec_id), ''), r.rec_id) = ?"""
    params = [root_id]
    if status is not None:
        sql += " AND b.status=?"
        params.append(str(status))
    sql += " ORDER BY b.started_ts DESC LIMIT 1"
    cur = conn.execute(sql, params)
    return _decode_bot_row(cur.fetchone())


def list_bot_instances(conn: sqlite3.Connection, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if status:
        cur = conn.execute("SELECT * FROM bot_instances WHERE status=? ORDER BY started_ts DESC LIMIT ?", (status, limit))
    else:
        cur = conn.execute("SELECT * FROM bot_instances ORDER BY started_ts DESC LIMIT ?", (limit,))
    return [bot for r in cur.fetchall() if (bot := _decode_bot_row(r)) and is_supported_bot_type(bot.get("bot_type"))]


def update_bot_state(conn: sqlite3.Connection, bot_id: str, patch: dict[str, Any], merge: bool = True, *, commit: bool = True) -> bool:
    cur = conn.execute("SELECT state_json FROM bot_instances WHERE bot_id=?", (bot_id,))
    row = cur.fetchone()
    if not row:
        return False
    state = _json_loads_mapping_or_default(row["state_json"], {})
    state = {**state, **patch} if merge else dict(patch)
    conn.execute("UPDATE bot_instances SET state_json=? WHERE bot_id=?", (_json_dumps_canonical(state), bot_id))
    if commit:
        conn.commit()
    return True


def insert_trade(conn: sqlite3.Connection, trade: dict[str, Any], *, commit: bool = True) -> str:
    """Insert a legacy aggregate trade row in a conflict-aware way.

    ``pnl`` is gross realised PnL derived from actual fills. Funding follows
    Bybit transaction-log sign semantics (positive receipt, negative payment).
    Slippage is retained as an execution-quality diagnostic, but is already
    embedded in fill-based gross PnL and must not be subtracted a second time.
    Net PnL is therefore ``pnl + funding - fee``.
    """
    pnl_raw = trade.get("pnl")
    fee_raw = trade.get("fee")
    funding_raw = trade.get("funding")
    slippage_raw = trade.get("slippage")
    payload = (
        trade["trade_id"],
        trade["bot_id"],
        _require_positive_int("ts", trade["ts"]),
        trade["symbol"],
        _require_finite_float("pnl", 0.0 if pnl_raw is None else pnl_raw),
        _require_finite_float("fee", 0.0 if fee_raw is None else fee_raw, minimum=0.0),
        _require_finite_float("funding", 0.0 if funding_raw is None else funding_raw),
        _require_finite_float("slippage", 0.0 if slippage_raw is None else slippage_raw, minimum=0.0),
        _json_dumps_canonical(trade.get("meta") or {}),
    )
    select_sql = """SELECT bot_id, ts, symbol, pnl, fee, funding, slippage, meta_json
                      FROM trades WHERE trade_id=?"""
    cur = conn.execute(select_sql, (trade["trade_id"],))
    row = cur.fetchone()
    if row:
        existing = (
            row["bot_id"], int(row["ts"]), row["symbol"], float(row["pnl"]), float(row["fee"]),
            float(row["funding"]), float(row["slippage"]),
            _json_dumps_canonical(_json_loads_mapping_or_default(row["meta_json"], {})),
        )
        if existing == payload[1:]:
            return "duplicate"
        raise ValueError(f"trade_id={trade['trade_id']} already exists with different payload")

    if conn.execute("SELECT 1 FROM execution_evidence WHERE bot_id=? LIMIT 1", (trade["bot_id"],)).fetchone():
        raise ValueError("cannot mix legacy trades with execution evidence for the same bot")

    savepoint = _savepoint_name("trade_insert")
    _begin_savepoint(conn, savepoint)
    try:
        conn.execute(
            """INSERT INTO trades(trade_id, bot_id, ts, symbol, pnl, fee, funding, slippage, meta_json)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            payload,
        )
        _release_savepoint(conn, savepoint)
    except INTEGRITY_ERRORS:
        _rollback_to_savepoint(conn, savepoint)
        _release_savepoint(conn, savepoint)
        row = conn.execute(select_sql, (trade["trade_id"],)).fetchone()
        if row:
            existing = (
                row["bot_id"], int(row["ts"]), row["symbol"], float(row["pnl"]), float(row["fee"]),
                float(row["funding"]), float(row["slippage"]),
                _json_dumps_canonical(_json_loads_mapping_or_default(row["meta_json"], {})),
            )
            if existing == payload[1:]:
                return "duplicate"
            raise ValueError(f"trade_id={trade['trade_id']} already exists with different payload")
        raise
    except Exception:
        _rollback_to_savepoint(conn, savepoint)
        _release_savepoint(conn, savepoint)
        raise
    if commit:
        conn.commit()
    return "inserted"


def get_trade_by_id(conn: sqlite3.Connection, trade_id: str) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,))
    r = cur.fetchone()
    if not r:
        return None
    return {
        "trade_id": r["trade_id"],
        "bot_id": r["bot_id"],
        "ts": int(r["ts"]),
        "symbol": r["symbol"],
        "pnl": _finite_float_or_default(r["pnl"], 0.0),
        "fee": _finite_float_or_default(r["fee"], 0.0),
        "funding": _finite_float_or_default(r["funding"], 0.0),
        "slippage": _finite_float_or_default(r["slippage"], 0.0),
        "meta": _json_loads_mapping_or_default(r["meta_json"], {}),
    }


def list_trades(conn: sqlite3.Connection, bot_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if bot_id:
        cur = conn.execute("SELECT * FROM trades WHERE bot_id=? ORDER BY ts DESC LIMIT ?", (bot_id, limit))
    else:
        cur = conn.execute("SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,))
    out = []
    for r in cur.fetchall():
        pnl = _finite_float_or_default(r["pnl"], 0.0)
        fee = _finite_float_or_default(r["fee"], 0.0)
        funding = _finite_float_or_default(r["funding"], 0.0)
        slippage = _finite_float_or_default(r["slippage"], 0.0)
        out.append({
            "trade_id": r["trade_id"], "bot_id": r["bot_id"], "ts": r["ts"], "symbol": r["symbol"],
            "pnl": pnl, "fee": fee, "funding": funding, "slippage": slippage,
            "realized_pnl_net": pnl + funding - fee,
            "meta": _json_loads_mapping_or_default(r["meta_json"], {}),
        })
    return out


def get_bot_trade_summary(conn: sqlite3.Connection, bot_id: str) -> dict[str, Any]:
    cur = conn.execute(
        """SELECT ts, pnl, fee, funding, slippage
           FROM trades WHERE bot_id=? ORDER BY ts ASC, trade_id ASC""",
        (bot_id,),
    )
    trade_count = 0
    realized_pnl_gross = realized_fee = realized_funding = realized_slippage = 0.0
    last_trade_ts: int | None = None
    for row in cur.fetchall():
        trade_count += 1
        realized_pnl_gross += _finite_float_or_default(row["pnl"], 0.0)
        realized_fee += _finite_float_or_default(row["fee"], 0.0)
        realized_funding += _finite_float_or_default(row["funding"], 0.0)
        realized_slippage += _finite_float_or_default(row["slippage"], 0.0)
        try:
            last_trade_ts = int(row["ts"])
        except Exception:
            pass
    realized_pnl_net = realized_pnl_gross + realized_funding - realized_fee
    return {
        "trade_count": trade_count,
        "realized_pnl_gross": realized_pnl_gross,
        "realized_fee": realized_fee,
        "realized_funding": realized_funding,
        "realized_slippage": realized_slippage,
        "realized_pnl_net": realized_pnl_net,
        "realized_pnl": realized_pnl_net,
        "last_trade_ts": last_trade_ts,
        "evidence_grade": False,
    }


def _optional_execution_float(name: str, value: Any, *, strictly_positive: bool = False) -> float | None:
    if value is None or value == "":
        return None
    num = _require_finite_float(name, value)
    if strictly_positive and num <= 0:
        raise ValueError(f"{name} must be > 0")
    return num


def _normalized_execution_text(name: str, value: Any, *, required: bool = True) -> str | None:
    text = str(value or "").strip()
    if not text:
        if required:
            raise ValueError(f"{name} must be a non-empty string")
        return None
    if "\x00" in text:
        raise ValueError(f"{name} must not contain NUL byte")
    return text


def _normalize_execution_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("event_type") or "").strip().lower()
    source = str(event.get("source") or "").strip().lower()
    if event_type not in {"execution", "funding"}:
        raise ValueError("event_type must be execution or funding")
    expected_source = "bybit_execution" if event_type == "execution" else "bybit_transaction_log"
    if source != expected_source:
        raise ValueError(f"event_type={event_type} requires source={expected_source}")

    event_id = _normalized_execution_text("event_id", event.get("event_id"))
    bot_id = _normalized_execution_text("bot_id", event.get("bot_id"))
    origin_rec_id = _normalized_execution_text("origin_rec_id", event.get("origin_rec_id"))
    symbol = _normalized_execution_text("symbol", event.get("symbol"))
    external_event_id = _normalized_execution_text("external_event_id", event.get("external_event_id"))
    external_order_id = _normalized_execution_text("external_order_id", event.get("external_order_id"), required=False)
    side_raw = _normalized_execution_text("side", event.get("side"), required=False)
    side = None
    if side_raw is not None:
        side_lower = side_raw.lower()
        if side_lower not in {"buy", "sell"}:
            raise ValueError("side must be Buy or Sell")
        side = "Buy" if side_lower == "buy" else "Sell"

    qty = _optional_execution_float("qty", event.get("qty"), strictly_positive=True)
    price = _optional_execution_float("price", event.get("price"), strictly_positive=True)
    order_price = _optional_execution_float("order_price", event.get("order_price"), strictly_positive=True)
    benchmark_price = _optional_execution_float("benchmark_price", event.get("benchmark_price"), strictly_positive=True)
    benchmark_ts_raw = event.get("benchmark_ts")
    benchmark_ts = None if benchmark_ts_raw is None else _require_positive_int("benchmark_ts", benchmark_ts_raw)
    benchmark_source = _normalized_execution_text("benchmark_source", event.get("benchmark_source"), required=False)
    if benchmark_source is not None:
        benchmark_source = benchmark_source.lower()
        if benchmark_source not in {"pre_submit_mid", "pre_submit_opposite", "decision_reference"}:
            raise ValueError("benchmark_source must be pre_submit_mid, pre_submit_opposite or decision_reference")
    gross_pnl = _require_finite_float("gross_pnl", event.get("gross_pnl", 0.0))
    fee = _require_finite_float("fee", event.get("fee", 0.0))
    funding = _require_finite_float("funding", event.get("funding", 0.0))
    slippage_raw = event.get("slippage")
    slippage = None if slippage_raw is None else _require_finite_float("slippage", slippage_raw, minimum=0.0)
    currency = str(event.get("currency") or "").strip().upper()
    if currency != "USDT":
        raise ValueError("currency must be USDT for Bybit Linear USDT evidence")
    ts = _require_positive_int("ts", event.get("ts"))
    meta = event.get("meta") or {}
    if not isinstance(meta, dict):
        raise ValueError("meta must be an object")

    if event_type == "execution":
        missing = []
        if external_order_id is None:
            missing.append("external_order_id")
        if side is None:
            missing.append("side")
        if qty is None:
            missing.append("qty")
        if price is None:
            missing.append("price")
        if order_price is None:
            missing.append("order_price")
        if benchmark_price is None:
            missing.append("benchmark_price")
        if benchmark_ts is None:
            missing.append("benchmark_ts")
        if benchmark_source is None:
            missing.append("benchmark_source")
        if missing:
            raise ValueError("execution evidence requires " + ", ".join(missing))
        if funding != 0.0:
            raise ValueError("bybit_execution evidence must record funding as a separate funding event")
        assert side is not None and qty is not None and price is not None
        assert order_price is not None and benchmark_price is not None and benchmark_ts is not None
        if benchmark_ts > ts:
            raise ValueError("benchmark_ts must not be later than execution ts")
        computed_slippage = (
            max(0.0, price - benchmark_price) * qty
            if side == "Buy"
            else max(0.0, benchmark_price - price) * qty
        )
        if slippage is None:
            slippage = computed_slippage
        elif not math.isclose(slippage, computed_slippage, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(
                f"slippage does not match side/qty/benchmark_price/price: expected {computed_slippage}"
            )
    else:
        if funding == 0.0:
            raise ValueError("funding evidence requires a non-zero signed funding cashflow")
        if slippage is None:
            slippage = 0.0
        if any(value != 0.0 for value in (gross_pnl, fee, slippage)):
            raise ValueError("funding evidence must not mix gross_pnl, fee or slippage")
        if any(value is not None for value in (
            external_order_id, side, qty, price, order_price, benchmark_price, benchmark_ts, benchmark_source
        )):
            raise ValueError("funding evidence must not contain execution-only fields")

    return {
        "event_id": event_id,
        "bot_id": bot_id,
        "origin_rec_id": origin_rec_id,
        "ts": ts,
        "symbol": str(symbol).upper(),
        "event_type": event_type,
        "source": source,
        "external_event_id": external_event_id,
        "external_order_id": external_order_id,
        "side": side,
        "qty": qty,
        "price": price,
        "order_price": order_price,
        "benchmark_price": benchmark_price,
        "benchmark_ts": benchmark_ts,
        "benchmark_source": benchmark_source,
        "gross_pnl": gross_pnl,
        "fee": fee,
        "funding": funding,
        "slippage": slippage,
        "currency": currency,
        "meta": meta,
    }


def _execution_event_comparable(event: dict[str, Any]) -> tuple[Any, ...]:
    return (
        event["bot_id"], event["origin_rec_id"], int(event["ts"]), event["symbol"],
        event["event_type"], event["source"], event["external_event_id"],
        event.get("external_order_id"), event.get("side"), event.get("qty"), event.get("price"), event.get("order_price"),
        event.get("benchmark_price"), event.get("benchmark_ts"), event.get("benchmark_source"), event["gross_pnl"], event["fee"], event["funding"], event["slippage"], event["currency"],
        _json_dumps_canonical(event.get("meta") or {}),
    )


def _decode_execution_event_row(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "event_id": row["event_id"],
        "bot_id": row["bot_id"],
        "origin_rec_id": row["origin_rec_id"],
        "ts": int(row["ts"]),
        "symbol": row["symbol"],
        "event_type": row["event_type"],
        "source": row["source"],
        "external_event_id": row["external_event_id"],
        "external_order_id": row["external_order_id"],
        "side": row["side"],
        "qty": None if row["qty"] is None else _finite_float_or_default(row["qty"], 0.0),
        "price": None if row["price"] is None else _finite_float_or_default(row["price"], 0.0),
        "order_price": None if row["order_price"] is None else _finite_float_or_default(row["order_price"], 0.0),
        "benchmark_price": None if row["benchmark_price"] is None else _finite_float_or_default(row["benchmark_price"], 0.0),
        "benchmark_ts": None if row["benchmark_ts"] is None else int(row["benchmark_ts"]),
        "benchmark_source": row["benchmark_source"],
        "gross_pnl": _finite_float_or_default(row["gross_pnl"], 0.0),
        "fee": _finite_float_or_default(row["fee"], 0.0),
        "funding": _finite_float_or_default(row["funding"], 0.0),
        "slippage": _finite_float_or_default(row["slippage"], 0.0),
        "currency": row["currency"],
        "meta": _json_loads_mapping_or_default(row["meta_json"], {}),
    }


def get_execution_event_by_id(conn: sqlite3.Connection, event_id: str) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM execution_evidence WHERE event_id=?", (event_id,))
    return _decode_execution_event_row(cur.fetchone())


def get_execution_event_by_external_id(conn: sqlite3.Connection, source: str, external_event_id: str) -> dict[str, Any] | None:
    cur = conn.execute(
        "SELECT * FROM execution_evidence WHERE source=? AND external_event_id=?",
        (str(source).strip().lower(), str(external_event_id).strip()),
    )
    return _decode_execution_event_row(cur.fetchone())


def insert_execution_event(conn: sqlite3.Connection, event: dict[str, Any], *, commit: bool = True) -> str:
    normalized = _normalize_execution_event(event)
    bot = get_bot_instance(conn, normalized["bot_id"])
    if bot is None:
        raise ValueError(f"bot_id={normalized['bot_id']} does not exist")
    if str(bot.get("origin_rec_id") or "") != normalized["origin_rec_id"]:
        raise ValueError("origin_rec_id does not match immutable bot origin")
    if str(bot.get("symbol") or "").upper() != normalized["symbol"]:
        raise ValueError("symbol does not match bot symbol")
    if get_recommendation_by_id(conn, normalized["origin_rec_id"]) is None:
        raise ValueError("origin_rec_id does not exist")

    existing = get_execution_event_by_id(conn, normalized["event_id"])
    if existing is not None:
        if _execution_event_comparable(existing) == _execution_event_comparable(normalized):
            return "duplicate"
        raise ValueError(f"event_id={normalized['event_id']} already exists with different payload")
    external = get_execution_event_by_external_id(conn, normalized["source"], normalized["external_event_id"])
    if external is not None:
        if _execution_event_comparable(external) == _execution_event_comparable(normalized):
            return "duplicate"
        raise ValueError(
            f"external_event_id={normalized['external_event_id']} already exists with different payload"
        )

    if conn.execute("SELECT 1 FROM trades WHERE bot_id=? LIMIT 1", (normalized["bot_id"],)).fetchone():
        raise ValueError("cannot mix execution evidence with legacy trades for the same bot")

    payload = (
        normalized["event_id"], normalized["bot_id"], normalized["origin_rec_id"], normalized["ts"],
        normalized["symbol"], normalized["event_type"], normalized["source"], normalized["external_event_id"],
        normalized["external_order_id"], normalized["side"], normalized["qty"], normalized["price"], normalized["order_price"],
        normalized["benchmark_price"], normalized["benchmark_ts"], normalized["benchmark_source"], normalized["gross_pnl"], normalized["fee"], normalized["funding"], normalized["slippage"],
        normalized["currency"], _json_dumps_canonical(normalized["meta"]),
    )
    savepoint = _savepoint_name("execution_evidence_insert")
    _begin_savepoint(conn, savepoint)
    try:
        conn.execute(
            """INSERT INTO execution_evidence(
                 event_id, bot_id, origin_rec_id, ts, symbol, event_type, source,
                 external_event_id, external_order_id, side, qty, price, order_price,
                 benchmark_price, benchmark_ts, benchmark_source, gross_pnl,
                 fee, funding, slippage, currency, meta_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            payload,
        )
        _release_savepoint(conn, savepoint)
    except INTEGRITY_ERRORS:
        _rollback_to_savepoint(conn, savepoint)
        _release_savepoint(conn, savepoint)
        external = get_execution_event_by_external_id(conn, normalized["source"], normalized["external_event_id"])
        if external is not None:
            if _execution_event_comparable(external) == _execution_event_comparable(normalized):
                return "duplicate"
            raise ValueError(
                f"external_event_id={normalized['external_event_id']} already exists with different payload"
            )
        raise
    except Exception:
        _rollback_to_savepoint(conn, savepoint)
        _release_savepoint(conn, savepoint)
        raise
    if commit:
        conn.commit()
    return "inserted"


def list_execution_events(conn: sqlite3.Connection, bot_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    if bot_id:
        cur = conn.execute(
            "SELECT * FROM execution_evidence WHERE bot_id=? ORDER BY ts DESC, event_id DESC LIMIT ?",
            (bot_id, limit),
        )
    else:
        cur = conn.execute("SELECT * FROM execution_evidence ORDER BY ts DESC, event_id DESC LIMIT ?", (limit,))
    return [item for row in cur.fetchall() if (item := _decode_execution_event_row(row)) is not None]


def _normalize_execution_reconciliation(snapshot: dict[str, Any]) -> dict[str, Any]:
    source = str(snapshot.get("source") or "").strip().lower()
    if source != "bybit_private_reconciliation":
        raise ValueError("execution reconciliation requires source=bybit_private_reconciliation")
    reconciliation_id = _normalized_execution_text(
        "reconciliation_id",
        snapshot.get("reconciliation_id"),
    )
    bot_id = _normalized_execution_text("bot_id", snapshot.get("bot_id"))
    origin_rec_id = _normalized_execution_text(
        "origin_rec_id",
        snapshot.get("origin_rec_id"),
    )
    external_snapshot_id = _normalized_execution_text(
        "external_snapshot_id",
        snapshot.get("external_snapshot_id"),
    )
    ts = _require_positive_int("ts", snapshot.get("ts"))
    position_qty = _require_finite_float("position_qty", snapshot.get("position_qty"))
    open_order_count = _require_non_negative_int(
        "open_order_count",
        snapshot.get("open_order_count"),
    )
    execution_event_count = _require_non_negative_int(
        "execution_event_count",
        snapshot.get("execution_event_count"),
    )
    funding_event_count = _require_non_negative_int(
        "funding_event_count",
        snapshot.get("funding_event_count"),
    )
    realized_pnl_gross = _require_finite_float(
        "realized_pnl_gross",
        snapshot.get("realized_pnl_gross"),
    )
    fee = _require_finite_float("fee", snapshot.get("fee"))
    funding = _require_finite_float("funding", snapshot.get("funding"))
    currency = str(snapshot.get("currency") or "").strip().upper()
    if currency != "USDT":
        raise ValueError("currency must be USDT for Bybit Linear USDT reconciliation")
    complete_raw = snapshot.get("complete")
    if not isinstance(complete_raw, bool):
        raise ValueError("complete must be a boolean")
    meta = snapshot.get("meta") or {}
    if not isinstance(meta, dict):
        raise ValueError("meta must be an object")
    return {
        "reconciliation_id": reconciliation_id,
        "bot_id": bot_id,
        "origin_rec_id": origin_rec_id,
        "ts": ts,
        "source": source,
        "external_snapshot_id": external_snapshot_id,
        "position_qty": position_qty,
        "open_order_count": open_order_count,
        "execution_event_count": execution_event_count,
        "funding_event_count": funding_event_count,
        "realized_pnl_gross": realized_pnl_gross,
        "fee": fee,
        "funding": funding,
        "currency": currency,
        "complete": complete_raw,
        "meta": meta,
    }


def _decode_execution_reconciliation_row(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "reconciliation_id": row["reconciliation_id"],
        "bot_id": row["bot_id"],
        "origin_rec_id": row["origin_rec_id"],
        "ts": int(row["ts"]),
        "source": row["source"],
        "external_snapshot_id": row["external_snapshot_id"],
        "position_qty": _finite_float_or_default(row["position_qty"], 0.0),
        "open_order_count": int(row["open_order_count"]),
        "execution_event_count": int(row["execution_event_count"]),
        "funding_event_count": int(row["funding_event_count"]),
        "realized_pnl_gross": _finite_float_or_default(row["realized_pnl_gross"], 0.0),
        "fee": _finite_float_or_default(row["fee"], 0.0),
        "funding": _finite_float_or_default(row["funding"], 0.0),
        "currency": row["currency"],
        "complete": bool(int(row["complete"])),
        "meta": _json_loads_mapping_or_default(row["meta_json"], {}),
    }


def _execution_reconciliation_comparable(snapshot: dict[str, Any]) -> tuple[Any, ...]:
    return (
        snapshot["bot_id"],
        snapshot["origin_rec_id"],
        int(snapshot["ts"]),
        snapshot["source"],
        snapshot["external_snapshot_id"],
        snapshot["position_qty"],
        int(snapshot["open_order_count"]),
        int(snapshot["execution_event_count"]),
        int(snapshot["funding_event_count"]),
        snapshot["realized_pnl_gross"],
        snapshot["fee"],
        snapshot["funding"],
        snapshot["currency"],
        bool(snapshot["complete"]),
        _json_dumps_canonical(snapshot.get("meta") or {}),
    )


def get_execution_reconciliation_by_id(
    conn: sqlite3.Connection,
    reconciliation_id: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM execution_reconciliations WHERE reconciliation_id=?",
        (str(reconciliation_id),),
    ).fetchone()
    return _decode_execution_reconciliation_row(row)


def get_execution_reconciliation_by_external_id(
    conn: sqlite3.Connection,
    source: str,
    external_snapshot_id: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT * FROM execution_reconciliations
           WHERE source=? AND external_snapshot_id=?""",
        (str(source).strip().lower(), str(external_snapshot_id).strip()),
    ).fetchone()
    return _decode_execution_reconciliation_row(row)


def get_latest_execution_reconciliation(
    conn: sqlite3.Connection,
    bot_id: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT * FROM execution_reconciliations
           WHERE bot_id=? ORDER BY ts DESC, reconciliation_id DESC LIMIT 1""",
        (str(bot_id),),
    ).fetchone()
    return _decode_execution_reconciliation_row(row)


def list_execution_reconciliations(
    conn: sqlite3.Connection,
    bot_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    if bot_id:
        cur = conn.execute(
            """SELECT * FROM execution_reconciliations WHERE bot_id=?
               ORDER BY ts DESC, reconciliation_id DESC LIMIT ?""",
            (str(bot_id), int(limit)),
        )
    else:
        cur = conn.execute(
            """SELECT * FROM execution_reconciliations
               ORDER BY ts DESC, reconciliation_id DESC LIMIT ?""",
            (int(limit),),
        )
    return [
        item
        for row in cur.fetchall()
        if (item := _decode_execution_reconciliation_row(row)) is not None
    ]


def insert_execution_reconciliation(
    conn: sqlite3.Connection,
    snapshot: dict[str, Any],
    *,
    commit: bool = True,
) -> str:
    normalized = _normalize_execution_reconciliation(snapshot)
    bot = get_bot_instance(conn, normalized["bot_id"])
    if bot is None:
        raise ValueError(f"bot_id={normalized['bot_id']} does not exist")
    if str(bot.get("origin_rec_id") or "") != normalized["origin_rec_id"]:
        raise ValueError("origin_rec_id does not match immutable bot origin")
    if get_recommendation_by_id(conn, normalized["origin_rec_id"]) is None:
        raise ValueError("origin_rec_id does not exist")
    if str(bot.get("status") or "").strip().lower() != "stopped":
        raise ValueError("terminal execution reconciliation requires a stopped bot")
    stopped_ts = strict_integer(bot.get("stopped_ts"))
    if stopped_ts is None or stopped_ts <= 0 or normalized["ts"] < stopped_ts:
        raise ValueError("terminal reconciliation timestamp must be at or after bot stop")

    existing = get_execution_reconciliation_by_id(
        conn,
        normalized["reconciliation_id"],
    )
    if existing is not None:
        if _execution_reconciliation_comparable(existing) == _execution_reconciliation_comparable(normalized):
            return "duplicate"
        raise ValueError(
            f"reconciliation_id={normalized['reconciliation_id']} already exists with different payload"
        )
    external = get_execution_reconciliation_by_external_id(
        conn,
        normalized["source"],
        normalized["external_snapshot_id"],
    )
    if external is not None:
        if _execution_reconciliation_comparable(external) == _execution_reconciliation_comparable(normalized):
            return "duplicate"
        raise ValueError(
            f"external_snapshot_id={normalized['external_snapshot_id']} already exists with different payload"
        )

    payload = (
        normalized["reconciliation_id"],
        normalized["bot_id"],
        normalized["origin_rec_id"],
        normalized["ts"],
        normalized["source"],
        normalized["external_snapshot_id"],
        normalized["position_qty"],
        normalized["open_order_count"],
        normalized["execution_event_count"],
        normalized["funding_event_count"],
        normalized["realized_pnl_gross"],
        normalized["fee"],
        normalized["funding"],
        normalized["currency"],
        int(normalized["complete"]),
        _json_dumps_canonical(normalized["meta"]),
    )
    savepoint = _savepoint_name("execution_reconciliation_insert")
    _begin_savepoint(conn, savepoint)
    try:
        conn.execute(
            """INSERT INTO execution_reconciliations(
                 reconciliation_id, bot_id, origin_rec_id, ts, source,
                 external_snapshot_id, position_qty, open_order_count,
                 execution_event_count, funding_event_count, realized_pnl_gross,
                 fee, funding, currency, complete, meta_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            payload,
        )
        _release_savepoint(conn, savepoint)
    except INTEGRITY_ERRORS:
        _rollback_to_savepoint(conn, savepoint)
        _release_savepoint(conn, savepoint)
        existing = get_execution_reconciliation_by_id(
            conn,
            normalized["reconciliation_id"],
        )
        if existing is not None:
            if _execution_reconciliation_comparable(existing) == _execution_reconciliation_comparable(normalized):
                return "duplicate"
            raise ValueError(
                f"reconciliation_id={normalized['reconciliation_id']} already exists with different payload"
            )
        external = get_execution_reconciliation_by_external_id(
            conn,
            normalized["source"],
            normalized["external_snapshot_id"],
        )
        if external is not None and _execution_reconciliation_comparable(external) == _execution_reconciliation_comparable(normalized):
            return "duplicate"
        raise
    except Exception:
        _rollback_to_savepoint(conn, savepoint)
        _release_savepoint(conn, savepoint)
        raise
    if commit:
        conn.commit()
    return "inserted"


def get_bot_execution_summary(conn: sqlite3.Connection, bot_id: str) -> dict[str, Any]:
    cur = conn.execute(
        """SELECT event_type, ts, side, qty, gross_pnl, fee, funding, slippage
             FROM execution_evidence WHERE bot_id=?
             ORDER BY ts ASC, event_id ASC""",
        (bot_id,),
    )
    event_count = execution_count = funding_event_count = 0
    gross = fee = funding = slippage = 0.0
    buy_qty = sell_qty = 0.0
    execution_ledger_complete = True
    first_event_ts: int | None = None
    last_event_ts: int | None = None
    for row in cur.fetchall():
        event_count += 1
        event_type = str(row["event_type"] or "").strip().lower()
        if event_type == "execution":
            execution_count += 1
            side = str(row["side"] or "").strip().lower()
            qty = _finite_float_or_default(row["qty"], 0.0)
            if qty <= 0.0 or side not in {"buy", "sell"}:
                # Legacy/corrupted execution rows cannot prove the terminal
                # position. Keep their monetary fields visible for audit, but do
                # not let them become live-validation evidence.
                execution_ledger_complete = False
            elif side == "buy":
                buy_qty += qty
            else:
                sell_qty += qty
        elif event_type == "funding":
            funding_event_count += 1
        ts = int(row["ts"])
        first_event_ts = ts if first_event_ts is None else min(first_event_ts, ts)
        last_event_ts = ts if last_event_ts is None else max(last_event_ts, ts)
        gross += _finite_float_or_default(row["gross_pnl"], 0.0)
        fee += _finite_float_or_default(row["fee"], 0.0)
        funding += _finite_float_or_default(row["funding"], 0.0)
        slippage += _finite_float_or_default(row["slippage"], 0.0)

    net_position_qty = buy_qty - sell_qty
    total_executed_qty = buy_qty + sell_qty
    position_qty_tolerance = max(1e-12, total_executed_qty * 1e-9)
    position_flat = bool(
        execution_count > 0
        and execution_ledger_complete
        and abs(net_position_qty) <= position_qty_tolerance
    )
    bot_row = conn.execute(
        "SELECT status, stopped_ts FROM bot_instances WHERE bot_id=?",
        (bot_id,),
    ).fetchone()
    bot_stopped = bool(
        bot_row is not None
        and str(bot_row["status"] or "").strip().lower() == "stopped"
        and bot_row["stopped_ts"] is not None
    )
    reconciliation = get_latest_execution_reconciliation(conn, bot_id)
    reconciliation_failures: list[str] = []
    if reconciliation is None:
        reconciliation_failures.append("missing_terminal_exchange_reconciliation")
    else:
        if reconciliation.get("complete") is not True:
            reconciliation_failures.append("exchange_reconciliation_incomplete")
        if (
            bot_row is not None
            and bot_row["stopped_ts"] is not None
            and int(reconciliation.get("ts") or 0) < int(bot_row["stopped_ts"])
        ):
            reconciliation_failures.append("exchange_reconciliation_precedes_bot_stop")
        if last_event_ts is not None and int(reconciliation.get("ts") or 0) < last_event_ts:
            reconciliation_failures.append("exchange_reconciliation_precedes_last_event")
        if int(reconciliation.get("open_order_count") or 0) != 0:
            reconciliation_failures.append("exchange_open_orders_remain")
        snapshot_position = _finite_float_or_default(
            reconciliation.get("position_qty"),
            float("nan"),
        )
        if (
            not math.isfinite(snapshot_position)
            or abs(snapshot_position) > position_qty_tolerance
        ):
            reconciliation_failures.append("exchange_position_not_flat")
        if int(reconciliation.get("execution_event_count") or 0) != execution_count:
            reconciliation_failures.append("execution_event_count_mismatch")
        if int(reconciliation.get("funding_event_count") or 0) != funding_event_count:
            reconciliation_failures.append("funding_event_count_mismatch")
        for field_name, local_value in (
            ("realized_pnl_gross", gross),
            ("fee", fee),
            ("funding", funding),
        ):
            snapshot_value = _finite_float_or_default(
                reconciliation.get(field_name),
                float("nan"),
            )
            tolerance = max(
                1e-8,
                max(abs(local_value), abs(snapshot_value) if math.isfinite(snapshot_value) else 0.0)
                * 1e-9,
            )
            if (
                not math.isfinite(snapshot_value)
                or not math.isclose(
                    snapshot_value,
                    local_value,
                    rel_tol=0.0,
                    abs_tol=tolerance,
                )
            ):
                reconciliation_failures.append(f"{field_name}_mismatch")
    exchange_reconciled = bool(reconciliation is not None and not reconciliation_failures)
    total_pnl_finalized = bool(bot_stopped and position_flat and exchange_reconciled)

    net = gross + funding - fee
    return {
        "event_count": event_count,
        "execution_count": execution_count,
        "funding_event_count": funding_event_count,
        "realized_pnl_gross": gross,
        "realized_fee": fee,
        "realized_funding": funding,
        "realized_slippage": slippage,
        "slippage_is_diagnostic": True,
        "realized_pnl_net": net,
        "net_formula": "gross_pnl + funding - fee",
        "first_event_ts": first_event_ts,
        "last_event_ts": last_event_ts,
        "buy_qty": buy_qty,
        "sell_qty": sell_qty,
        "net_position_qty": net_position_qty,
        "position_qty_tolerance": position_qty_tolerance,
        "position_flat": position_flat,
        "execution_ledger_complete": execution_ledger_complete,
        "bot_stopped": bot_stopped,
        "exchange_reconciled": exchange_reconciled,
        "exchange_reconciliation": reconciliation,
        "exchange_reconciliation_failures": reconciliation_failures,
        "total_pnl_finalized": total_pnl_finalized,
        "position_reconciliation_model": "signed_execution_qty_plus_terminal_bybit_snapshot_v2",
        "evidence_grade": total_pnl_finalized,
    }


def list_live_validation_records(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    cur = conn.execute(
        """SELECT b.bot_id, b.started_ts
             FROM bot_instances b
             JOIN execution_evidence e ON e.bot_id=b.bot_id
             GROUP BY b.bot_id, b.started_ts
             ORDER BY b.started_ts DESC, b.bot_id DESC
             LIMIT ?""",
        (limit,),
    )
    out: list[dict[str, Any]] = []
    for row in cur.fetchall():
        bot = get_bot_instance(conn, row["bot_id"])
        if bot is None:
            continue
        rec_id = str(bot.get("origin_rec_id") or "").strip()
        rec = get_recommendation_by_id(conn, rec_id) if rec_id else None
        if rec is None:
            continue
        summary = get_bot_execution_summary(conn, bot["bot_id"])
        validation_ineligible_reasons: list[str] = []
        if str(bot.get("status") or "").strip().lower() != "stopped" or bot.get("stopped_ts") is None:
            validation_ineligible_reasons.append("bot_not_stopped")
        if int(summary.get("execution_count") or 0) <= 0:
            validation_ineligible_reasons.append("no_execution_events")
        if summary.get("execution_ledger_complete") is not True:
            validation_ineligible_reasons.append("execution_ledger_incomplete")
        if int(summary.get("execution_count") or 0) > 0 and summary.get("position_flat") is not True:
            validation_ineligible_reasons.append("residual_position")
        if summary.get("exchange_reconciled") is not True:
            failures = summary.get("exchange_reconciliation_failures") or []
            if isinstance(failures, list) and failures:
                validation_ineligible_reasons.extend(
                    f"exchange_reconciliation:{str(reason)}" for reason in failures
                )
            else:
                validation_ineligible_reasons.append("exchange_reconciliation_unavailable")
        if summary.get("total_pnl_finalized") is not True and not validation_ineligible_reasons:
            validation_ineligible_reasons.append("total_pnl_not_finalized")

        out.append({
            "bot_id": bot["bot_id"],
            "rec_id": rec_id,
            "publication_root_rec_id": bot.get("publication_root_rec_id") or rec.get("publication_root_rec_id") or rec_id,
            "venue": bot.get("venue"),
            "symbol": bot.get("symbol"),
            "bot_type": bot.get("bot_type"),
            "direction": rec.get("direction"),
            "recommendation_ts": rec.get("ts"),
            "started_ts": bot.get("started_ts"),
            "stopped_ts": bot.get("stopped_ts"),
            "bot_status": bot.get("status"),
            "score": rec.get("score"),
            "confidence": rec.get("confidence"),
            "expected_rr": rec.get("expected_rr"),
            "model_version": rec.get("model_version"),
            **summary,
            "validation_eligible": summary.get("total_pnl_finalized") is True,
            "validation_ineligible_reasons": validation_ineligible_reasons,
        })
    return out

def _recommended_row_passes_conf_filter(row: sqlite3.Row, min_conf: float, strict_min_conf: bool = False) -> bool:
    if not is_actionable_recommendation_status(row["status"]):
        return True
    conf = float(row["confidence"] or 0.0)
    if strict_min_conf:
        return conf >= float(min_conf)
    try:
        reasons = _json_loads_mapping_or_default(row["reasons_json"], {})
    except Exception:
        reasons = {}
    confidence_model = reasons.get("confidence_model") if isinstance(reasons, dict) else {}
    if not isinstance(confidence_model, dict):
        confidence_model = {}
    gate_applied = confidence_model.get("confidence_gate_applied")
    if gate_applied is None:
        source = str(confidence_model.get("source") or "")
        fitted = bool(confidence_model.get("fitted"))
        gate_applied = bool(fitted and source not in ("", "raw", "raw_proxy"))
    if not bool(gate_applied):
        return True
    return conf >= float(min_conf)


def get_recommendations(
    conn: sqlite3.Connection,
    venue: str | None,
    top_n: int,
    min_conf: float,
    statuses: list[str] | None = None,
    snapshot_ts: int | None = None,
    strict_min_conf: bool = False,
) -> list[dict[str, Any]]:
    _supported_sql, _supported_params = sql_in_clause("bot_type")
    if snapshot_ts is not None:
        q = f"""SELECT * FROM recommendations WHERE ts = ? AND {_supported_sql}"""
        params: list[Any] = [snapshot_ts, *_supported_params]
    else:
        # Use 24h window so executed/ignored/expired recs remain visible for audit
        q = f"""SELECT * FROM recommendations WHERE ts > ? AND {_supported_sql}"""
        params: list[Any] = [now_ts() - 86400, *_supported_params]
    if venue:
        q += " AND venue=?"
        params.append(venue)
    if statuses is not None:
        if not statuses:
            # Empty list → caller wants no statuses → return nothing
            return []
        placeholders = ",".join("?" for _ in statuses)
        q += f" AND status IN ({placeholders})"
        params.extend(statuses)
    q += " ORDER BY CASE status WHEN 'recommended' THEN 0 WHEN 'active' THEN 1 WHEN 'executed' THEN 2 WHEN 'ignored' THEN 3 WHEN 'pending' THEN 4 WHEN 'blocked' THEN 5 WHEN 'no_trade' THEN 6 WHEN 'suppressed' THEN 7 ELSE 8 END, confidence DESC, score DESC, ts DESC"
    cur = conn.execute(q, params)
    rows = []
    for r in cur.fetchall():
        if not _recommended_row_passes_conf_filter(r, min_conf=min_conf, strict_min_conf=strict_min_conf):
            continue
        rows.append({
            "rec_id": r["rec_id"],
            "ts": r["ts"],
            "venue": r["venue"],
            "symbol": r["symbol"],
            "bot_type": r["bot_type"],
            "direction": r["direction"],
            "account_mode": r["account_mode"],
            "margin_mode": r["margin_mode"],
            "score": r["score"],
            "confidence": r["confidence"],
            "expected_rr": r["expected_rr"],
            "risk_score": r["risk_score"],
            "params": _json_loads_mapping_or_default(r["params_json"], {}),
            "reasons": _json_loads_mapping_or_default(r["reasons_json"], {}),
            "blocks": _json_loads_list_of_mappings_or_default(r["blocks_json"], []),
            "status": r["status"],
            "ttl_sec": r["ttl_sec"],
            "model_version": r["model_version"],
            "features_ref_ts": r["features_ref_ts"],
            "publication_root_rec_id": str(r["publication_root_rec_id"] or r["rec_id"]).strip() or r["rec_id"],
            "is_outcome_label_root": bool(int(r["is_outcome_label_root"] or 0)),
        })
        if len(rows) >= int(top_n):
            break
    return rows


def get_recommendation_history(
    conn: sqlite3.Connection,
    *,
    venue: str,
    symbol: str,
    bot_type: str | None = None,
    limit: int = 500,
) -> tuple[list[dict[str, Any]], int]:
    """Return recent publication rows for one market pair in chronological order.

    The operator UI needs the raw publication sequence rather than a collapsed
    snapshot so recommendation changes can be inspected.  The query reads only
    persisted decision fields; it does not pretend that historical Bybit runtime
    guards can be reconstructed from today's market metadata.
    """
    venue_norm = str(venue or "").strip().lower()
    symbol_norm = str(symbol or "").strip().upper()
    bot_type_norm = str(bot_type or "").strip()
    max_rows = max(1, min(int(limit or 500), 2000))

    where = ["venue=?", "symbol=?"]
    params: list[Any] = [venue_norm, symbol_norm]
    if bot_type_norm:
        where.append("bot_type=?")
        params.append(bot_type_norm)
    where_sql = " AND ".join(where)

    count_row = conn.execute(
        f"SELECT COUNT(*) AS c FROM recommendations WHERE {where_sql}",
        params,
    ).fetchone()
    total = int(count_row["c"] or 0) if count_row else 0

    # Read newest rows under the cap, then reverse for a left-to-right timeline.
    cur = conn.execute(
        f"""SELECT rec_id, ts, venue, symbol, bot_type, direction, score,
                   confidence, expected_rr, risk_score, reasons_json, status,
                   ttl_sec, model_version, features_ref_ts,
                   publication_root_rec_id, is_outcome_label_root
              FROM recommendations
             WHERE {where_sql}
             ORDER BY ts DESC, rec_id DESC
             LIMIT ?""",
        [*params, max_rows],
    )
    newest_first = cur.fetchall()
    rows: list[dict[str, Any]] = []
    for r in reversed(newest_first):
        reasons = _json_loads_mapping_or_default(r["reasons_json"], {})
        llm_review = reasons.get("llm_review") if isinstance(reasons.get("llm_review"), dict) else {}
        rows.append({
            "rec_id": r["rec_id"],
            "ts": r["ts"],
            "venue": r["venue"],
            "symbol": r["symbol"],
            "bot_type": r["bot_type"],
            "direction": r["direction"],
            "score": r["score"],
            "confidence": r["confidence"],
            "expected_rr": r["expected_rr"],
            "risk_score": r["risk_score"],
            "status": r["status"],
            "llm_status": str(llm_review.get("status") or "none").strip().lower() or "none",
            "ttl_sec": r["ttl_sec"],
            "model_version": r["model_version"],
            "features_ref_ts": r["features_ref_ts"],
            "publication_root_rec_id": str(r["publication_root_rec_id"] or r["rec_id"]).strip() or r["rec_id"],
            "is_outcome_label_root": bool(int(r["is_outcome_label_root"] or 0)),
        })
    return rows, total

def get_recommendation_by_id(conn: sqlite3.Connection, rec_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
    sql = "SELECT * FROM recommendations WHERE rec_id=?"
    if for_update and getattr(conn, "db_engine", SQLITE) == POSTGRES:
        # В PostgreSQL обычный BEGIN не сериализует read-check-write так же жёстко,
        # как BEGIN IMMEDIATE в SQLite. FOR UPDATE нужен, чтобы concurrent
        # operator-actions не принимали решения по одному и тому же rec на
        # разъехавшемся снимке статуса.
        sql += " FOR UPDATE"
    cur = conn.execute(sql, (rec_id,))
    r = cur.fetchone()
    if not r or not is_supported_bot_type(r["bot_type"]):
        return None
    return {
        "rec_id": r["rec_id"],
        "ts": r["ts"],
        "venue": r["venue"],
        "symbol": r["symbol"],
        "bot_type": r["bot_type"],
        "direction": r["direction"],
        "account_mode": r["account_mode"],
        "margin_mode": r["margin_mode"],
        "score": r["score"],
        "confidence": r["confidence"],
        "expected_rr": r["expected_rr"],
        "risk_score": r["risk_score"],
        "params": _json_loads_mapping_or_default(r["params_json"], {}),
        "reasons": _json_loads_mapping_or_default(r["reasons_json"], {}),
        "blocks": _json_loads_list_of_mappings_or_default(r["blocks_json"], []),
        "status": r["status"],
        "ttl_sec": r["ttl_sec"],
        "model_version": r["model_version"],
        "features_ref_ts": r["features_ref_ts"],
        "publication_root_rec_id": str(r["publication_root_rec_id"] or r["rec_id"]).strip() or r["rec_id"],
        "is_outcome_label_root": bool(int(r["is_outcome_label_root"] or 0)),
    }


def update_recommendation_review(
    conn: sqlite3.Connection,
    rec_id: str,
    *,
    reasons: dict[str, Any],
    status: str | None = None,
) -> bool:
    cur = conn.execute("SELECT status FROM recommendations WHERE rec_id=?", (rec_id,))
    row = cur.fetchone()
    if not row:
        return False
    current = str(row["status"])
    new_status = current
    if status and status in OPERATOR_STATUSES:
        allowed = _ALLOWED_STATUS_TRANSITIONS.get(current, {current})
        if status in allowed:
            new_status = status
    conn.execute(
        "UPDATE recommendations SET reasons_json=?, status=? WHERE rec_id=?",
        (_json_dumps_safe(reasons), new_status, rec_id),
    )
    conn.commit()
    return True



def acquire_runtime_lock(conn: sqlite3.Connection, lock_key: str, owner: str, ttl_sec: int = 90) -> bool:
    """Cross-process best-effort leader lock.

    Для SQLite сериализация достигается через ``BEGIN IMMEDIATE``. Для PostgreSQL
    одного ``BEGIN`` недостаточно: два лидера могут одновременно прочитать
    отсутствие lock-row и оба записать себя владельцем. Поэтому в PostgreSQL
    claim выполняется одной atomic UPSERT-командой с проверкой протухшего
    heartbeat прямо внутри ``ON CONFLICT ... DO UPDATE ... WHERE``.
    """
    now = now_ts()
    if getattr(conn, "db_engine", "sqlite") == POSTGRES:
        expiry_before = now - max(5, int(ttl_sec))

        def _pg_op() -> bool:
            cur = conn.execute(
                """INSERT INTO runtime_locks(lock_key, owner, heartbeat_ts)
                       VALUES(?,?,?)
                    ON CONFLICT (lock_key) DO UPDATE
                          SET owner=EXCLUDED.owner, heartbeat_ts=EXCLUDED.heartbeat_ts
                        WHERE runtime_locks.owner=EXCLUDED.owner
                           OR runtime_locks.heartbeat_ts < ?
                    RETURNING owner""",
                (lock_key, owner, now, expiry_before),
            )
            row = cur.fetchone()
            conn.commit()
            return row is not None

        try:
            return bool(_execute_lock_write_with_retry(_pg_op))
        except OPERATIONAL_ERRORS:
            try:
                conn.rollback()
            except Exception:
                logger.debug("rollback error", exc_info=True)
            return False

    def _op() -> bool:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT owner, heartbeat_ts FROM runtime_locks WHERE lock_key=?",
            (lock_key,),
        ).fetchone()
        should_claim = False
        if row is None:
            should_claim = True
        else:
            row_owner = str(row["owner"] or "")
            heartbeat_ts = int(row["heartbeat_ts"] or 0)
            if row_owner == owner or (now - heartbeat_ts) > max(5, int(ttl_sec)):
                should_claim = True

        if should_claim:
            conn.execute(
                "INSERT OR REPLACE INTO runtime_locks(lock_key, owner, heartbeat_ts) VALUES(?,?,?)",
                (lock_key, owner, now),
            )
            conn.commit()
            return True
        conn.commit()
        return False

    try:
        return bool(_execute_lock_write_with_retry(_op))
    except OPERATIONAL_ERRORS:
        try:
            conn.rollback()
        except Exception:
            logger.debug("rollback error", exc_info=True)
        return False


def release_runtime_lock(conn: sqlite3.Connection, lock_key: str, owner: str) -> None:
    _execute_lock_write_with_retry(
        lambda: conn.execute(
            "DELETE FROM runtime_locks WHERE lock_key=? AND owner=?",
            (lock_key, owner),
        )
    )
    conn.commit()


def heartbeat_runtime_lock(conn: sqlite3.Connection, lock_key: str, owner: str) -> bool:
    cur = _execute_lock_write_with_retry(
        lambda: conn.execute(
            "UPDATE runtime_locks SET heartbeat_ts=? WHERE lock_key=? AND owner=?",
            (now_ts(), lock_key, owner),
        )
    )
    conn.commit()
    return int(cur.rowcount or 0) > 0

def upsert_risk_limits(conn: sqlite3.Connection, version: str, limits: dict[str, Any], is_active: bool = True, *, commit: bool = True) -> None:
    if is_active:
        conn.execute("""UPDATE risk_limits SET is_active=0""")
    conn.execute(
        """INSERT INTO risk_limits(version, limits_json, is_active, created_ts) VALUES(?,?,?,?)""",
        (version, _json_dumps_safe(limits), 1 if is_active else 0, now_ts()),
    )
    if commit:
        conn.commit()

def get_active_risk_limits(conn: sqlite3.Connection) -> dict[str, Any] | None:
    cur = conn.execute("""SELECT limits_json FROM risk_limits WHERE is_active=1 ORDER BY created_ts DESC LIMIT 1""")
    row = cur.fetchone()
    return _json_loads_mapping_or_default(row["limits_json"], None) if row else None

def insert_sentiment_point(
    conn: sqlite3.Connection,
    scope: str,
    key: str,
    ts: int,
    sentiment: float,
    velocity: float,
    volume: int,
    sources: dict,
    tags: list[str],
    *,
    commit: bool = True,
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO sentiment(scope, key, ts, sentiment, velocity, volume, sources_json, tags_json)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            scope,
            key,
            _require_positive_int("ts", ts),
            _require_finite_float("sentiment", sentiment),
            _require_finite_float("velocity", velocity),
            _require_non_negative_int("volume", volume),
            _json_dumps_safe(sources),
            _json_dumps_safe(tags),
        ),
    )
    if commit:
        conn.commit()


def insert_sentiment_points(conn: sqlite3.Connection, rows: list[dict[str, Any]], *, commit: bool = True) -> None:
    if not rows:
        return
    conn.executemany(
        """INSERT OR REPLACE INTO sentiment(scope, key, ts, sentiment, velocity, volume, sources_json, tags_json)
           VALUES(?,?,?,?,?,?,?,?)""",
        [
            (
                row["scope"],
                row["key"],
                _require_positive_int("ts", row["ts"]),
                _require_finite_float("sentiment", row["sentiment"]),
                _require_finite_float("velocity", 0.0 if row.get("velocity") is None else row.get("velocity")),
                _require_non_negative_int("volume", 0 if row.get("volume") is None else row.get("volume")),
                _json_dumps_safe(row.get("sources") or {}),
                _json_dumps_safe(row.get("tags") or []),
            )
            for row in rows
        ],
    )
    if commit:
        conn.commit()

def get_sentiment_series(conn: sqlite3.Connection, scope: str, key: str, limit: int = 120) -> list[dict[str, Any]]:
    cur = conn.execute(
        """SELECT * FROM sentiment WHERE scope=? AND key=? ORDER BY ts DESC LIMIT ?""",
        (scope, key, limit),
    )
    out = []
    for r in cur.fetchall()[::-1]:
        decoded = _decode_sentiment_row(r)
        if decoded is not None:
            out.append(decoded)
    return out

def list_realized_net_events(conn: sqlite3.Connection, *, since_ts: int = 0) -> list[dict[str, Any]]:
    """Return only terminally exchange-reconciled realised PnL events.

    Local signed fills, operator-submitted events and legacy aggregate trades are
    not profitability evidence by themselves.  A bot enters this stream only after
    its stopped/flat ledger matches a later complete Bybit private reconciliation.
    """
    start_ts = _require_non_negative_int("since_ts", since_ts)
    out: list[dict[str, Any]] = []
    cur = conn.execute(
        """SELECT event_id, bot_id, ts, gross_pnl, fee, funding, slippage
             FROM execution_evidence
            WHERE ts>=?
            ORDER BY ts ASC, event_id ASC""",
        (start_ts,),
    )
    finalized_by_bot: dict[str, bool] = {}
    for row in cur.fetchall():
        bot_id = str(row["bot_id"])
        if bot_id not in finalized_by_bot:
            summary = get_bot_execution_summary(conn, bot_id)
            finalized_by_bot[bot_id] = bool(summary.get("total_pnl_finalized"))
        if not finalized_by_bot[bot_id]:
            continue
        gross = _finite_float_or_default(row["gross_pnl"], 0.0)
        fee = _finite_float_or_default(row["fee"], 0.0)
        funding = _finite_float_or_default(row["funding"], 0.0)
        out.append({
            "event_id": row["event_id"],
            "bot_id": bot_id,
            "ts": int(row["ts"]),
            "gross_pnl": gross,
            "fee": fee,
            "funding": funding,
            "net_pnl": gross + funding - fee,
            "source": "exchange_reconciled_evidence",
        })
    return out


def list_risk_net_events(conn: sqlite3.Connection, *, since_ts: int = 0) -> list[dict[str, Any]]:
    """Return a loss-conservative risk stream without crediting unverified profit.

    Reconciled terminal bots contribute their signed net cashflows.  Unreconciled
    execution events and legacy trades can tighten controls through losses, but
    positive values are clamped to zero and cannot manufacture daily profitability,
    drawdown recovery or a favorable cooldown state.
    """
    start_ts = _require_non_negative_int("since_ts", since_ts)
    out: list[dict[str, Any]] = []
    cur = conn.execute(
        """SELECT event_id, bot_id, ts, gross_pnl, fee, funding
             FROM execution_evidence
            WHERE ts>=?
            ORDER BY ts ASC, event_id ASC""",
        (start_ts,),
    )
    finalized_by_bot: dict[str, bool] = {}
    for row in cur.fetchall():
        bot_id = str(row["bot_id"])
        if bot_id not in finalized_by_bot:
            finalized_by_bot[bot_id] = bool(
                get_bot_execution_summary(conn, bot_id).get("total_pnl_finalized")
            )
        raw_net = (
            _finite_float_or_default(row["gross_pnl"], 0.0)
            + _finite_float_or_default(row["funding"], 0.0)
            - _finite_float_or_default(row["fee"], 0.0)
        )
        reconciled = finalized_by_bot[bot_id]
        out.append({
            "event_id": row["event_id"],
            "bot_id": bot_id,
            "ts": int(row["ts"]),
            "net_pnl": raw_net if reconciled else min(0.0, raw_net),
            "source": (
                "exchange_reconciled_evidence"
                if reconciled
                else "unreconciled_execution_loss_only"
            ),
        })

    cur = conn.execute(
        """SELECT t.trade_id, t.bot_id, t.ts, t.pnl, t.fee, t.funding
             FROM trades t
            WHERE t.ts>=?
              AND NOT EXISTS (
                    SELECT 1 FROM execution_evidence e
                     WHERE e.bot_id=t.bot_id AND e.event_type='execution'
              )
            ORDER BY t.ts ASC, t.trade_id ASC""",
        (start_ts,),
    )
    for row in cur.fetchall():
        pnl = _finite_float_or_default(row["pnl"], 0.0)
        fee = _finite_float_or_default(row["fee"], 0.0)
        funding = _finite_float_or_default(row["funding"], 0.0)
        raw_net = pnl + funding - fee
        out.append({
            "event_id": row["trade_id"],
            "bot_id": row["bot_id"],
            "ts": int(row["ts"]),
            "net_pnl": min(0.0, raw_net),
            "source": "legacy_trade_loss_only",
        })
    out.sort(key=lambda item: (int(item["ts"]), str(item["source"]), str(item["event_id"])))
    return out


def sum_daily_gross_pnl(conn: sqlite3.Connection, day_start_ts: int) -> float:
    return sum(
        float(item.get("gross_pnl") or 0.0)
        for item in list_realized_net_events(conn, since_ts=day_start_ts)
    )


def sum_daily_fees(conn: sqlite3.Connection, day_start_ts: int) -> float:
    return sum(
        float(item.get("fee") or 0.0)
        for item in list_realized_net_events(conn, since_ts=day_start_ts)
    )


def sum_daily_pnl(conn: sqlite3.Connection, day_start_ts: int) -> float:
    """Net daily PnL from terminally exchange-reconciled evidence only."""
    return sum(float(item["net_pnl"]) for item in list_realized_net_events(conn, since_ts=day_start_ts))



def get_latest_sentiment(conn: sqlite3.Connection, scope: str, key: str) -> dict[str, Any] | None:
    cur = conn.execute(
        """SELECT * FROM sentiment WHERE scope=? AND key=? ORDER BY ts DESC LIMIT 1""",
        (scope, key),
    )
    return _decode_sentiment_row(cur.fetchone())


def get_outcomes_with_recs(conn: sqlite3.Connection, limit: int = 6000, *, require_llm_verdict: bool = False) -> list[dict[str, Any]]:
    """Returns outcomes joined with rec score/bot_type/direction/reasons in one query.
    Replaces N+1 pattern of get_outcomes_recent + get_recommendation_by_id per row.
    """
    cur = conn.execute(
        """SELECT o.rec_id, o.ts, o.venue, o.symbol, o.bot_type, o.direction,
                  o.horizon_sec, o.label_available_ts, o.entry_close, o.exit_close,
                  o.success, o.ret,
                  r.score, r.status, r.reasons_json, r.model_version, r.publication_root_rec_id, r.is_outcome_label_root
           FROM reco_outcomes o
           JOIN recommendations r ON r.rec_id = o.rec_id
           WHERE COALESCE(r.is_outcome_label_root, 1) = 1
           ORDER BY o.ts DESC LIMIT ?""",
        (limit,),
    )
    out = []
    for row in cur.fetchall():
        if require_llm_verdict and not is_outcome_eligible_under_llm_mode(row["status"], row["reasons_json"]):
            continue
        try:
            reasons = _json_loads_mapping_or_default(row["reasons_json"], {})
        except Exception:
            reasons = {}
        ts = strict_integer(row["ts"])
        horizon_sec = strict_integer(row["horizon_sec"])
        label_available_ts = strict_integer(row["label_available_ts"])
        success = strict_integer(row["success"])
        ret = _finite_float_or_default(row["ret"], float("nan"))
        score = _finite_float_or_default(row["score"], float("nan"))
        root_flag = strict_integer(row["is_outcome_label_root"])
        if (
            ts is None or ts <= 0
            or horizon_sec is None or horizon_sec < 0
            or success not in (0, 1)
            or not math.isfinite(ret)
            or not math.isfinite(score)
            or root_flag not in (0, 1)
        ):
            continue
        out.append({
            "rec_id":    row["rec_id"],
            "ts":        ts,
            "venue":     row["venue"],
            "symbol":    row["symbol"],
            "bot_type":  row["bot_type"],
            "direction": row["direction"],
            "horizon_sec": horizon_sec,
            "label_available_ts": (
                label_available_ts if label_available_ts is not None and label_available_ts > 0 else None
            ),
            "entry_close": _finite_float_or_default(row["entry_close"], float("nan")),
            "exit_close": _finite_float_or_default(row["exit_close"], float("nan")),
            "success":   success,
            "ret":       ret,
            "score":     score,
            "reasons":   reasons,
            "model_version": str(row["model_version"] or ""),
            "publication_root_rec_id": str(row["publication_root_rec_id"] or row["rec_id"]),
            "is_outcome_label_root": bool(root_flag),
        })
    return out


OUTCOME_OBSERVABILITY_STATES: frozenset[str] = frozenset({
    "waiting",
    "censored",
    "labeled",
})
POLICY_LABEL_HORIZONS_SEC: dict[str, int] = {
    "futures_grid": 12 * 3600,
}
POLICY_LABEL_GRACE_SEC = 120


def upsert_outcome_observability(
    conn: sqlite3.Connection,
    *,
    rec_id: str,
    recommendation_ts: int,
    label_due_ts: int | None,
    state: str,
    reason: str,
    details: dict[str, Any] | None = None,
    commit: bool = True,
) -> None:
    rec_id_norm = str(rec_id or "").strip()
    recommendation_ts_int = strict_integer(recommendation_ts)
    label_due_ts_int = strict_integer(label_due_ts) if label_due_ts is not None else None
    state_norm = str(state or "").strip().lower()
    reason_norm = str(reason or "").strip()
    if not rec_id_norm or recommendation_ts_int is None or recommendation_ts_int <= 0:
        raise ValueError("outcome observability requires rec_id and positive recommendation_ts")
    if label_due_ts is not None and (
        label_due_ts_int is None or label_due_ts_int < recommendation_ts_int
    ):
        raise ValueError("outcome observability label_due_ts is invalid")
    if state_norm not in OUTCOME_OBSERVABILITY_STATES:
        raise ValueError("unsupported outcome observability state")
    if not reason_norm:
        raise ValueError("outcome observability reason is required")
    attempt_ts = now_ts()
    conn.execute(
        """INSERT INTO reco_outcome_observability(
                 rec_id, recommendation_ts, label_due_ts, last_attempt_ts,
                 state, reason, details_json
               ) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(rec_id) DO UPDATE SET
                 recommendation_ts=excluded.recommendation_ts,
                 label_due_ts=excluded.label_due_ts,
                 last_attempt_ts=excluded.last_attempt_ts,
                 state=excluded.state,
                 reason=excluded.reason,
                 details_json=excluded.details_json""",
        (
            rec_id_norm,
            recommendation_ts_int,
            label_due_ts_int,
            attempt_ts,
            state_norm,
            reason_norm,
            _json_dumps_safe(details or {}),
        ),
    )
    if commit:
        conn.commit()


def get_policy_outcome_observability(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    policy_fingerprint: str,
    now_ts_value: int | None = None,
    bot_type: str | None = None,
    require_llm_verdict: bool = False,
) -> dict[str, Any]:
    """Count every matured root in an immutable policy cohort.

    Calibration rows are an inner join by construction; this outer-join ledger is
    the independent denominator that exposes censored and unresolved outcomes.
    """
    now_value = strict_integer(now_ts_value if now_ts_value is not None else now_ts())
    if now_value is None or now_value <= 0:
        raise ValueError("now_ts_value must be a positive integer timestamp")
    model_norm = str(model_version or "").strip()
    fingerprint_norm = str(policy_fingerprint or "").strip().lower()
    if not is_sha256_fingerprint(fingerprint_norm):
        raise ValueError("policy_fingerprint must be a sha256 hex digest")
    cur = conn.execute(
        """SELECT r.rec_id, r.ts, r.venue, r.symbol, r.bot_type,
                  r.direction AS recommendation_direction, r.score,
                  r.status, r.reasons_json,
                  r.model_version, r.is_outcome_label_root,
                  o.rec_id AS outcome_rec_id,
                  o.ts AS outcome_ts, o.venue AS outcome_venue,
                  o.symbol AS outcome_symbol, o.bot_type AS outcome_bot_type,
                  o.direction AS outcome_direction, o.horizon_sec,
                  o.label_available_ts, o.entry_close, o.exit_close,
                  o.success, o.ret,
                  obs.state AS observability_state,
                  obs.reason AS observability_reason
             FROM recommendations r
             LEFT JOIN reco_outcomes o ON o.rec_id=r.rec_id
             LEFT JOIN reco_outcome_observability obs ON obs.rec_id=r.rec_id
            WHERE COALESCE(r.is_outcome_label_root, 1)=1
              AND (r.model_version=? OR r.model_version LIKE ?)
            ORDER BY r.ts DESC, r.rec_id DESC""",
        (model_norm, model_norm + "+%"),
    )
    matured_total = 0
    labeled_total = 0
    censored_total = 0
    unresolved_total = 0
    invalid_contract_total = 0
    invalid_labeled_total = 0
    censor_reasons: dict[str, int] = {}

    def _record_invalid_contract(reason: str) -> None:
        nonlocal matured_total, unresolved_total, invalid_contract_total
        matured_total += 1
        unresolved_total += 1
        invalid_contract_total += 1
        censor_reasons[reason] = censor_reasons.get(reason, 0) + 1

    for row in cur.fetchall():
        if bot_type is not None and str(row["bot_type"] or "") != str(bot_type):
            continue
        recommendation_ts = strict_integer(row["ts"])
        horizon_sec = POLICY_LABEL_HORIZONS_SEC.get(str(row["bot_type"] or "").strip())
        canonical_label_due_ts = (
            recommendation_ts + horizon_sec + POLICY_LABEL_GRACE_SEC
            if recommendation_ts is not None
            and recommendation_ts > 0
            and horizon_sec is not None
            else None
        )
        is_matured = bool(
            canonical_label_due_ts is None or canonical_label_due_ts <= now_value
        )
        reasons = _json_loads_mapping_or_default(row["reasons_json"], {})
        outcome_policy = reasons.get("outcome_policy") if isinstance(reasons, dict) else None
        if not isinstance(outcome_policy, dict):
            if is_matured:
                _record_invalid_contract("missing_policy_contract")
            continue
        policy_eligible = outcome_policy.get("policy_evaluation_eligible")
        if policy_eligible is False:
            continue
        if policy_eligible is not True:
            if is_matured:
                _record_invalid_contract("invalid_policy_eligibility")
            continue
        stored_fingerprint = str(
            outcome_policy.get("policy_fingerprint") or ""
        ).strip().lower()
        try:
            contract_fingerprint = canonical_policy_fingerprint(
                outcome_policy.get("policy_contract")
            )
        except Exception:
            contract_fingerprint = ""
        if (
            not is_sha256_fingerprint(stored_fingerprint)
            or contract_fingerprint != stored_fingerprint
        ):
            if is_matured:
                _record_invalid_contract("invalid_policy_contract_fingerprint")
            continue
        if stored_fingerprint != fingerprint_norm:
            continue
        if require_llm_verdict and not is_outcome_eligible_under_llm_mode(
            row["status"], row["reasons_json"]
        ):
            continue
        if canonical_label_due_ts is None:
            _record_invalid_contract("invalid_policy_maturity_contract")
            continue
        if canonical_label_due_ts > now_value:
            continue
        matured_total += 1
        stored_label_due_ts = strict_integer(outcome_policy.get("label_due_ts"))
        if stored_label_due_ts != canonical_label_due_ts:
            unresolved_total += 1
            invalid_contract_total += 1
            censor_reasons["invalid_policy_maturity_contract"] = (
                censor_reasons.get("invalid_policy_maturity_contract", 0) + 1
            )
            continue
        if row["outcome_rec_id"] is not None:
            outcome_ts = strict_integer(row["outcome_ts"])
            outcome_horizon = strict_integer(row["horizon_sec"])
            outcome_available_ts = strict_integer(row["label_available_ts"])
            outcome_success = strict_integer(row["success"])
            entry_close = _finite_float_or_default(row["entry_close"], float("nan"))
            exit_close = _finite_float_or_default(row["exit_close"], float("nan"))
            outcome_return = _finite_float_or_default(row["ret"], float("nan"))
            recommendation_score = _finite_float_or_default(row["score"], float("nan"))
            label_valid = bool(
                outcome_ts == recommendation_ts
                and outcome_horizon == horizon_sec
                and outcome_available_ts is not None
                and outcome_available_ts >= int(recommendation_ts or 0)
                and outcome_available_ts <= now_value
                and str(row["outcome_venue"] or "") == str(row["venue"] or "")
                and str(row["outcome_symbol"] or "") == str(row["symbol"] or "")
                and str(row["outcome_bot_type"] or "") == str(row["bot_type"] or "")
                and str(row["outcome_direction"] or "")
                == str(row["recommendation_direction"] or "")
                and outcome_success in (0, 1)
                and math.isfinite(entry_close)
                and entry_close > 0.0
                and math.isfinite(exit_close)
                and exit_close > 0.0
                and math.isfinite(outcome_return)
                and math.isfinite(recommendation_score)
            )
            if label_valid:
                labeled_total += 1
            else:
                unresolved_total += 1
                invalid_labeled_total += 1
                censor_reasons["invalid_labeled_outcome"] = (
                    censor_reasons.get("invalid_labeled_outcome", 0) + 1
                )
            continue
        state = str(row["observability_state"] or "").strip().lower()
        if state == "censored":
            censored_total += 1
            reason = str(row["observability_reason"] or "unknown").strip() or "unknown"
            censor_reasons[reason] = censor_reasons.get(reason, 0) + 1
        else:
            unresolved_total += 1
    return {
        "policy_fingerprint": fingerprint_norm,
        "matured_total": matured_total,
        "labeled_total": labeled_total,
        "censored_total": censored_total,
        "unresolved_total": unresolved_total,
        "invalid_contract_total": invalid_contract_total,
        "invalid_labeled_total": invalid_labeled_total,
        "censor_reasons": dict(sorted(censor_reasons.items())),
    }


def insert_outcome(conn: sqlite3.Connection, o: dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO reco_outcomes(
            rec_id, ts, venue, symbol, bot_type, direction, horizon_sec, label_available_ts,
            entry_close, exit_close, ret, success
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            o["rec_id"], o["ts"], o["venue"], o["symbol"], o["bot_type"], o["direction"], o["horizon_sec"],
            o.get("label_available_ts"), o["entry_close"], o["exit_close"], o["ret"], o["success"]
        ),
    )
    upsert_outcome_observability(
        conn,
        rec_id=str(o["rec_id"]),
        recommendation_ts=int(o["ts"]),
        label_due_ts=o.get("label_available_ts"),
        state="labeled",
        reason="outcome_inserted",
        details={"bot_type": o.get("bot_type")},
        commit=False,
    )
    conn.commit()

def outcome_exists(conn: sqlite3.Connection, rec_id: str) -> bool:
    cur = conn.execute("""SELECT 1 FROM reco_outcomes WHERE rec_id=? LIMIT 1""", (rec_id,))
    return cur.fetchone() is not None

def get_outcomes_recent(conn: sqlite3.Connection, limit: int = 2000) -> list[dict[str, Any]]:
    cur = conn.execute("""SELECT * FROM reco_outcomes ORDER BY ts DESC LIMIT ?""", (limit,))
    out = []
    for r in cur.fetchall():
        out.append({k: r[k] for k in r.keys()})
    return out


def get_latest_reco_ts(conn: sqlite3.Connection, venue: str | None = None) -> int | None:
    _supported_sql, _supported_params = sql_in_clause("bot_type")
    if venue:
        cur = conn.execute(f"""SELECT MAX(ts) AS m FROM recommendations WHERE venue=? AND {_supported_sql}""", [venue, *_supported_params])
    else:
        cur = conn.execute(f"""SELECT MAX(ts) AS m FROM recommendations WHERE {_supported_sql}""", _supported_params)
    r = cur.fetchone()
    return int(r["m"]) if r and r["m"] is not None else None


def list_recent_reco_snapshot_ts(conn: sqlite3.Connection, venue: str | None = None, limit: int = 50) -> list[int]:
    _supported_sql, _supported_params = sql_in_clause("bot_type")
    q = f"""SELECT DISTINCT ts FROM recommendations WHERE {_supported_sql}"""
    params: list[Any] = [*_supported_params]
    if venue:
        q += " AND venue=?"
        params.append(venue)
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(max(1, int(limit)))
    cur = conn.execute(q, params)
    return [int(r["ts"]) for r in cur.fetchall() if r["ts"] is not None]


def get_recent_llm_review_candidates(
    conn: sqlite3.Connection,
    *,
    venue: str | None = None,
    recent_sec: int = 3600,
    limit: int = 60,
    snapshot_ts: int | None = None,
) -> list[dict[str, Any]]:
    cutoff_ts = max(0, now_ts() - max(60, int(recent_sec)))
    _supported_sql, _supported_params = sql_in_clause("bot_type")
    _eligible_status_params = ["recommended", "active", "pending"]
    _eligible_status_sql = "status IN (?,?,?)"
    if snapshot_ts is not None:
        q = f"""SELECT * FROM recommendations
            WHERE ts = ? AND {_eligible_status_sql} AND {_supported_sql}"""
        params: list[Any] = [int(snapshot_ts), *_eligible_status_params, *_supported_params]
    else:
        q = f"""SELECT * FROM recommendations
            WHERE ts >= ? AND {_eligible_status_sql} AND {_supported_sql}"""
        params = [cutoff_ts, *_eligible_status_params, *_supported_params]
    if venue:
        q += " AND venue=?"
        params.append(venue)
    q += " ORDER BY ts DESC, confidence DESC, score DESC LIMIT ?"
    params.append(max(1, int(limit)))
    cur = conn.execute(q, params)
    out: list[dict[str, Any]] = []
    for r in cur.fetchall():
        out.append({
            "rec_id": r["rec_id"],
            "ts": r["ts"],
            "venue": r["venue"],
            "symbol": r["symbol"],
            "bot_type": r["bot_type"],
            "direction": r["direction"],
            "account_mode": r["account_mode"],
            "margin_mode": r["margin_mode"],
            "score": r["score"],
            "confidence": r["confidence"],
            "expected_rr": r["expected_rr"],
            "risk_score": r["risk_score"],
            "params": _json_loads_mapping_or_default(r["params_json"], {}),
            "reasons": _json_loads_mapping_or_default(r["reasons_json"], {}),
            "blocks": _json_loads_list_of_mappings_or_default(r["blocks_json"], []),
            "status": r["status"],
            "ttl_sec": r["ttl_sec"],
            "model_version": r["model_version"],
            "features_ref_ts": r["features_ref_ts"],
            "publication_root_rec_id": str(r["publication_root_rec_id"] or r["rec_id"]).strip() or r["rec_id"],
            "is_outcome_label_root": bool(int(r["is_outcome_label_root"] or 0)),
        })
    return out




def get_llm_status_counts(
    conn: sqlite3.Connection,
    *,
    venue: str | None,
    min_conf: float,
    statuses: list[str] | None = None,
    snapshot_ts: int | None = None,
    strict_min_conf: bool = False,
) -> dict[str, int]:
    _supported_sql, _supported_params = sql_in_clause("bot_type")
    if snapshot_ts is not None:
        q = f"""SELECT status, confidence, reasons_json FROM recommendations WHERE ts = ? AND {_supported_sql}"""
        params: list[Any] = [snapshot_ts, *_supported_params]
    else:
        q = f"""SELECT status, confidence, reasons_json FROM recommendations WHERE ts > ? AND {_supported_sql}"""
        params = [now_ts() - 86400, *_supported_params]
    if venue:
        q += " AND venue=?"
        params.append(venue)
    if statuses is not None:
        if not statuses:
            return {"ok": 0, "pending": 0, "error": 0, "skipped": 0, "none": 0, "other": 0}
        placeholders = ",".join("?" for _ in statuses)
        q += f" AND status IN ({placeholders})"
        params.extend(statuses)

    counts = {"ok": 0, "pending": 0, "error": 0, "skipped": 0, "none": 0, "other": 0}
    cur = conn.execute(q, params)
    for row in cur.fetchall():
        if not _recommended_row_passes_conf_filter(row, min_conf=min_conf, strict_min_conf=strict_min_conf):
            continue
        review = _extract_llm_review_snapshot(row["reasons_json"])
        llm_status = str((review or {}).get("status") or "none").strip().lower()
        if llm_status not in counts:
            llm_status = "other"
        counts[llm_status] += 1
    return counts

def get_recommendation_status_counts(
    conn: sqlite3.Connection,
    venue: str | None = None,
    snapshot_ts: int | None = None,
) -> dict[str, int]:
    _supported_sql, _supported_params = sql_in_clause("bot_type")
    if snapshot_ts is not None:
        q = f"""SELECT status, COUNT(*) AS c FROM recommendations WHERE ts = ? AND {_supported_sql}"""
        params: list[Any] = [snapshot_ts, *_supported_params]
    else:
        q = f"""SELECT status, COUNT(*) AS c FROM recommendations WHERE ts > ? AND {_supported_sql}"""
        params = [now_ts() - 86400, *_supported_params]
    if venue:
        q += " AND venue=?"
        params.append(venue)
    q += " GROUP BY status"
    cur = conn.execute(q, params)
    counts = {k: 0 for k in ("recommended", "active", "pending", "blocked", "no_trade", "suppressed", "expired", "executed", "ignored")}
    for row in cur.fetchall():
        counts[str(row["status"])] = int(row["c"] or 0)
    return counts



def get_recommender_warmup_status(
    conn: sqlite3.Connection,
    symbols_linear: list[str],
    legacy_symbols_linear: list[str] | None = None,
    *,
    stale_sec: int = 300,
    min_rows_per_tf: int = 80,
    required_tfs: Iterable[int] | None = None,
    active_venues: list[str] | None = None,
) -> dict[str, Any]:
    """Warm-up/readiness summary for recommendation publishing.

    A symbol is considered ready when:
      - ticker snapshot is fresh enough;
      - latest closed 1m candle is fresh enough;
      - each required timeframe has at least `min_rows_per_tf` valid candles.

    Slow timeframes are checked for history depth only, not freshness, because a
    closed daily candle can be <24h old and still be fully valid.
    """
    if isinstance(legacy_symbols_linear, list) and legacy_symbols_linear:
        symbols_linear = [str(item) for item in legacy_symbols_linear]

    now = now_ts()
    min_rows_per_tf = max(1, int(min_rows_per_tf or 1))
    tf_list = tuple(dict.fromkeys(int(tf) for tf in (required_tfs or (60, 900, 1800, 3600, 14400, 86400)) if int(tf) > 0))
    active = {str(v or '').strip().lower() for v in (active_venues or ['linear']) if str(v or '').strip()}

    def _iter_symbols(items: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for raw in items:
            sym = str(raw or '').strip().upper()
            if not sym or sym in seen:
                continue
            out.append(sym)
            seen.add(sym)
        return out

    per_venue: list[dict[str, Any]] = []
    for venue, raw_symbols in (("linear", symbols_linear),):
        if venue not in active:
            continue
        symbols = _iter_symbols(raw_symbols)
        reasons_count: dict[str, int] = {}
        ready_symbols = 0
        samples: list[dict[str, Any]] = []
        for sym in symbols:
            reasons: list[str] = []
            ticker_ts = get_latest_ticker_ts(conn, venue, sym)
            ticker_age_sec = None if ticker_ts is None else max(0, now - int(ticker_ts))
            if ticker_age_sec is None:
                reasons.append('ticker_missing')
            elif ticker_age_sec > stale_sec:
                reasons.append('ticker_stale')

            rows_1m = get_latest_ohlcv(conn, venue, sym, 60, limit=min_rows_per_tf)
            last_1m_ts = int(rows_1m[0]['ts']) if rows_1m else None
            candle_age_sec = None if last_1m_ts is None else max(0, now - last_1m_ts)
            if candle_age_sec is None:
                reasons.append('candle_missing')
            elif candle_age_sec > stale_sec:
                reasons.append('candle_stale')
            if len(rows_1m) < min_rows_per_tf:
                reasons.append('tf_60_short')

            for tf_sec in tf_list:
                if tf_sec == 60:
                    continue
                rows = get_latest_ohlcv(conn, venue, sym, tf_sec, limit=min_rows_per_tf)
                if len(rows) < min_rows_per_tf:
                    reasons.append(f'tf_{int(tf_sec)}_short')

            if not reasons:
                ready_symbols += 1
            else:
                for reason in reasons:
                    reasons_count[reason] = reasons_count.get(reason, 0) + 1
                if len(samples) < 8:
                    samples.append({
                        'symbol': sym,
                        'reasons': reasons,
                        'ticker_age_sec': ticker_age_sec,
                        'candle_age_sec': candle_age_sec,
                    })

        total_symbols = len(symbols)
        ready_ratio = float(ready_symbols / total_symbols) if total_symbols else 1.0
        per_venue.append({
            'venue': venue,
            'symbols_total': total_symbols,
            'ready_symbols': ready_symbols,
            'ready_ratio': round(ready_ratio, 4),
            'not_ready_symbols': max(0, total_symbols - ready_symbols),
            'reason_counts': reasons_count,
            'sample_not_ready': samples,
            'required_tfs': list(tf_list),
            'min_rows_per_tf': int(min_rows_per_tf),
            'stale_sec': int(stale_sec),
        })

    overall_total = sum(int(item['symbols_total']) for item in per_venue)
    overall_ready = sum(int(item['ready_symbols']) for item in per_venue)
    overall_ratio = float(overall_ready / overall_total) if overall_total else 1.0
    return {
        'ts': now,
        'venues': per_venue,
        'symbols_total': overall_total,
        'ready_symbols': overall_ready,
        'ready_ratio': round(overall_ratio, 4),
        'required_tfs': list(tf_list),
        'min_rows_per_tf': int(min_rows_per_tf),
        'stale_sec': int(stale_sec),
    }


# ── funding rate ──────────────────────────────────────────────────────────────

def _normalize_funding_row(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = dict(row)
    raw_ts = payload.get("ts")
    raw_funding_rate = payload.get("funding_rate")
    if isinstance(raw_funding_rate, bool):
        return None
    ts = strict_integer(raw_ts)
    if ts is None:
        return None
    try:
        funding_rate = float(raw_funding_rate)
    except Exception:
        return None
    if (not _is_plausible_market_ts(ts)) or (not math.isfinite(funding_rate)):
        return None
    next_funding_ts_raw = payload.get("next_funding_ts")
    next_funding_ts = (
        strict_integer(next_funding_ts_raw)
        if next_funding_ts_raw not in (None, "")
        else None
    )
    if next_funding_ts is not None and next_funding_ts <= 0:
        next_funding_ts = None
    funding_interval_min = None
    raw_interval = payload.get("funding_interval_min")
    if raw_interval not in (None, ""):
        interval = strict_integer(raw_interval)
        if interval is not None and interval > 0:
            funding_interval_min = int(interval)
    return {
        "symbol": str(payload.get("symbol") or ""),
        "ts": ts,
        "funding_rate": funding_rate,
        "next_funding_ts": next_funding_ts,
        "funding_interval_min": funding_interval_min,
    }


def upsert_funding_rate(conn: sqlite3.Connection, rows: list[dict], *, commit: bool = True) -> None:
    valid_rows = []
    for row in rows:
        normalized = _normalize_funding_row(row)
        if normalized is not None and normalized["symbol"]:
            valid_rows.append(normalized)
    if not valid_rows:
        return
    conn.executemany(
        """INSERT OR REPLACE INTO funding_rate(symbol, ts, funding_rate, next_funding_ts, funding_interval_min)
           VALUES(?,?,?,?,?)""",
        [
            (r["symbol"], r["ts"], r["funding_rate"], r.get("next_funding_ts"), r.get("funding_interval_min"))
            for r in valid_rows
        ],
    )
    if commit:
        conn.commit()

def get_latest_funding_rate(conn: sqlite3.Connection, symbol: str) -> dict | None:
    cur = conn.execute(
        """SELECT * FROM funding_rate WHERE symbol=? ORDER BY ts DESC LIMIT 64""",
        (symbol,),
    )
    for row in cur.fetchall():
        normalized = _normalize_funding_row(row)
        if normalized is not None:
            return normalized
    return None


def _normalize_funding_settlement_row(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = dict(row)
    symbol = str(payload.get("symbol") or "").strip().upper()
    ts = strict_integer(payload.get("ts"))
    raw_rate = payload.get("funding_rate")
    if not symbol or ts is None or isinstance(raw_rate, bool):
        return None
    try:
        rate = float(raw_rate)
    except Exception:
        return None
    if not _is_plausible_market_ts(ts) or not math.isfinite(rate):
        return None
    return {"symbol": symbol, "ts": int(ts), "funding_rate": float(rate)}


def upsert_funding_settlements(conn: sqlite3.Connection, rows: list[dict], *, commit: bool = True) -> None:
    valid_rows: list[dict[str, Any]] = []
    for row in rows:
        normalized = _normalize_funding_settlement_row(row)
        if normalized is not None:
            valid_rows.append(normalized)
    if not valid_rows:
        return
    conn.executemany(
        """INSERT OR REPLACE INTO funding_settlement(symbol, ts, funding_rate)
           VALUES(?,?,?)""",
        [(row["symbol"], row["ts"], row["funding_rate"]) for row in valid_rows],
    )
    if commit:
        conn.commit()


def get_funding_settlements(
    conn: sqlite3.Connection,
    symbol: str,
    ts_start: int,
    ts_end: int,
) -> list[dict[str, Any]]:
    start = strict_integer(ts_start)
    end = strict_integer(ts_end)
    target = str(symbol or "").strip().upper()
    if not target or start is None or end is None or start <= 0 or end < start:
        return []
    cur = conn.execute(
        """SELECT symbol, ts, funding_rate
           FROM funding_settlement
           WHERE symbol=? AND ts>=? AND ts<=?
           ORDER BY ts ASC""",
        (target, int(start), int(end)),
    )
    out: list[dict[str, Any]] = []
    for row in cur.fetchall():
        normalized = _normalize_funding_settlement_row(row)
        if normalized is not None:
            out.append(normalized)
    return out


def get_latest_funding_settlement_ts(conn: sqlite3.Connection, symbol: str) -> int | None:
    target = str(symbol or "").strip().upper()
    if not target:
        return None
    cur = conn.execute(
        "SELECT ts FROM funding_settlement WHERE symbol=? ORDER BY ts DESC LIMIT 1",
        (target,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    ts = strict_integer(row["ts"])
    return int(ts) if ts is not None and ts > 0 else None

# ── open interest ─────────────────────────────────────────────────────────────

def _normalize_open_interest_row(symbol: str, row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = dict(row)
    raw_ts = payload.get("ts")
    raw_oi = payload.get("oi")
    if isinstance(raw_oi, bool):
        return None
    ts = strict_integer(raw_ts)
    if ts is None:
        return None
    try:
        oi = float(raw_oi)
    except Exception:
        return None
    if (not _is_plausible_market_ts(ts)) or (not math.isfinite(oi)) or oi < 0:
        return None
    return {"symbol": str(symbol or payload.get("symbol") or ""), "ts": ts, "oi": oi}


def upsert_open_interest(conn: sqlite3.Connection, symbol: str, rows: list[dict], *, commit: bool = True) -> None:
    valid_rows = []
    for row in rows:
        normalized = _normalize_open_interest_row(symbol, row)
        if normalized is not None and normalized["symbol"]:
            valid_rows.append(normalized)
    if not valid_rows:
        return
    conn.executemany(
        """INSERT OR REPLACE INTO open_interest(symbol, ts, oi) VALUES(?,?,?)""",
        [(r["symbol"], r["ts"], r["oi"]) for r in valid_rows],
    )
    if commit:
        conn.commit()

def get_oi_series(conn: sqlite3.Connection, symbol: str, limit: int = 48) -> list[dict]:
    safe_limit = max(1, int(limit))
    fetch_limit = max(safe_limit, min(5000, safe_limit * 4))
    cur = conn.execute(
        """SELECT ts, oi FROM open_interest WHERE symbol=? ORDER BY ts DESC LIMIT ?""",
        (symbol, fetch_limit),
    )
    out: list[dict[str, Any]] = []
    for row in cur.fetchall():
        normalized = _normalize_open_interest_row(symbol, row)
        if normalized is None:
            continue
        out.append({"ts": normalized["ts"], "oi": normalized["oi"]})
        if len(out) >= safe_limit:
            break
    return out


def get_latest_open_interest_ts(conn: sqlite3.Connection, symbol: str) -> int | None:
    cur = conn.execute(
        """SELECT ts, oi FROM open_interest WHERE symbol=? ORDER BY ts DESC LIMIT ?""",
        (symbol, LATEST_ROW_SCAN_LIMIT),
    )
    for row in cur.fetchall():
        normalized = _normalize_open_interest_row(symbol, row)
        if normalized is not None:
            return int(normalized["ts"])
    return None


# ── Operator actions ──────────────────────────────────────────────────────────

OPERATOR_STATUSES = {"executed", "ignored", "recommended", "active", "pending", "blocked", "no_trade", "suppressed", "expired"}
TERMINAL_RECOMMENDATION_STATUSES = {"executed", "ignored", "expired", "blocked", "no_trade", "suppressed"}
_ALLOWED_STATUS_TRANSITIONS = {
    # recommendation engine / async reviewers may still downgrade a fresh idea
    # before an operator explicitly acts on it.
    "recommended": {"recommended", "active", "pending", "executed", "ignored", "blocked", "no_trade", "suppressed", "expired"},
    "active": {"active", "pending", "executed", "ignored", "blocked", "no_trade", "suppressed", "expired"},
    "pending": {"pending", "recommended", "active", "ignored", "blocked", "no_trade", "suppressed", "expired"},
    "executed": {"executed"},
    "ignored": {"ignored"},
    "expired": {"expired"},
    "blocked": {"blocked"},
    "no_trade": {"no_trade"},
    "suppressed": {"suppressed"},
}


def update_recommendation_status(
    conn: sqlite3.Connection,
    rec_id: str,
    status: str,
    operator: str | None = None,
    *,
    commit: bool = True,
) -> bool:
    if status not in OPERATOR_STATUSES:
        return False
    cur = conn.execute("SELECT status FROM recommendations WHERE rec_id=?", (rec_id,))
    row = cur.fetchone()
    if not row:
        return False
    current = str(row["status"])
    allowed = _ALLOWED_STATUS_TRANSITIONS.get(current, {current})
    if status not in allowed:
        return False
    if status == current:
        return True
    cur = conn.execute(
        """UPDATE recommendations SET status=? WHERE rec_id=?""",
        (status, rec_id),
    )
    if cur.rowcount == 0:
        return False
    log_decision(conn, "STATUS_UPDATE", rec_id, operator, {"old_status": current, "new_status": status}, commit=False)
    if commit:
        conn.commit()
    return True


# ── TTL expiry ────────────────────────────────────────────────────────────────

def _recommendation_chain_first_ts(conn: sqlite3.Connection, root_rec_id: str) -> int | None:
    root = str(root_rec_id or "").strip()
    if not root:
        return None
    row = conn.execute(
        """SELECT MIN(ts) AS first_ts
             FROM recommendations
            WHERE publication_root_rec_id = ? OR rec_id = ?""",
        (root, root),
    ).fetchone()
    if not row or row["first_ts"] is None:
        return None
    try:
        return int(row["first_ts"])
    except Exception:
        return None


def recommendation_chain_expiry_context(
    conn: sqlite3.Connection,
    *,
    rec_id: str,
    publication_root_rec_id: str | None,
    row_ts: int | None,
    ttl_sec: int | None,
    ts_now: int | None = None,
) -> dict[str, Any]:
    """Return row-vs-publication-chain TTL state for a recommendation.

    A recommendation can be republished many times under the same
    ``publication_root_rec_id``. The executable market idea must expire from the
    first root signal, not from the most recent replacement row. Otherwise a
    stale idea can stay visually active forever by receiving tiny updates.
    """
    now = int(ts_now if ts_now is not None else now_ts())
    rec_id_norm = str(rec_id or "").strip()
    root = str(publication_root_rec_id or rec_id_norm).strip() or rec_id_norm
    try:
        ttl = int(ttl_sec) if ttl_sec is not None else 0
    except Exception:
        ttl = 0
    try:
        row_ts_int = int(row_ts) if row_ts is not None else None
    except Exception:
        row_ts_int = None

    chain_first_ts = _recommendation_chain_first_ts(conn, root) if root else None
    if chain_first_ts is None:
        chain_first_ts = row_ts_int

    row_age_sec = None if row_ts_int is None else max(0, now - int(row_ts_int))
    chain_age_sec = None if chain_first_ts is None else max(0, now - int(chain_first_ts))
    row_expires_in_sec = None if row_ts_int is None or ttl <= 0 else int(row_ts_int) + ttl - now
    chain_expires_in_sec = None if chain_first_ts is None or ttl <= 0 else int(chain_first_ts) + ttl - now
    return {
        "publication_root_rec_id": root,
        "recommendation_row_age_sec": row_age_sec,
        "publication_chain_started_ts": chain_first_ts,
        "publication_chain_age_sec": chain_age_sec,
        "recommendation_row_expires_in_sec": row_expires_in_sec,
        "publication_chain_expires_in_sec": chain_expires_in_sec,
        "is_recommendation_row_expired": bool(row_expires_in_sec is not None and row_expires_in_sec <= 0),
        "is_publication_chain_expired": bool(chain_expires_in_sec is not None and chain_expires_in_sec <= 0),
    }


def expire_stale_recommendations(conn: sqlite3.Connection) -> int:
    """Mark transient recs as expired when either the row or its root chain exceeds TTL.

    Operator-set statuses are preserved. Expiring by row ``ts + ttl`` alone is
    unsafe for republished signals: a stale idea can keep a fresh child row and
    remain actionable. This function therefore expires the whole transient
    publication chain from the earliest root timestamp.
    """
    ts_now = now_ts()
    placeholders = ",".join("?" for _ in EXPIRABLE_RECOMMENDATION_STATUSES)
    rows = conn.execute(
        f"""SELECT rec_id, ts, ttl_sec, publication_root_rec_id
               FROM recommendations
              WHERE status IN ({placeholders})""",
        [*EXPIRABLE_RECOMMENDATION_STATUSES],
    ).fetchall()

    expired_ids: list[str] = []
    row_expired_count = 0
    chain_expired_count = 0
    root_first_ts_cache: dict[str, int | None] = {}
    for row in rows:
        rec_id = str(row["rec_id"] or "").strip()
        if not rec_id:
            continue
        try:
            ttl_sec = int(row["ttl_sec"] or 0)
        except Exception:
            ttl_sec = 0
        if ttl_sec <= 0:
            continue
        try:
            row_ts = int(row["ts"] or 0)
        except Exception:
            row_ts = 0
        root = str(row["publication_root_rec_id"] or rec_id).strip() or rec_id
        if root not in root_first_ts_cache:
            root_first_ts_cache[root] = _recommendation_chain_first_ts(conn, root)
        chain_first_ts = root_first_ts_cache.get(root) or row_ts
        row_expired = bool(row_ts > 0 and row_ts + ttl_sec <= ts_now)
        chain_expired = bool(chain_first_ts > 0 and chain_first_ts + ttl_sec <= ts_now)
        if row_expired or chain_expired:
            expired_ids.append(rec_id)
            row_expired_count += int(row_expired)
            chain_expired_count += int(chain_expired)

    if not expired_ids:
        conn.commit()
        return 0

    update_placeholders = ",".join("?" for _ in expired_ids)
    cur = conn.execute(
        f"UPDATE recommendations SET status='expired' WHERE rec_id IN ({update_placeholders})",
        expired_ids,
    )
    conn.commit()
    expired = int(cur.rowcount or 0)
    if expired > 0:
        log_decision(
            conn,
            "TTL_EXPIRED",
            None,
            None,
            {
                "count": expired,
                "ts": ts_now,
                "row_expired_count": int(row_expired_count),
                "chain_expired_count": int(chain_expired_count),
                "mode": "row_or_publication_chain",
            },
        )
    return expired


# ── Outcomes stats ────────────────────────────────────────────────────────────

def _normalize_direction(direction: Any, fallback: str = "neutral") -> str:
    value = str(direction or fallback).strip().lower()
    return value if value in ("long", "short", "neutral") else fallback


def _parse_reasons_json(reasons_json: str | None) -> dict[str, Any]:
    try:
        reasons = _json_loads_mapping_or_default(reasons_json, {})
    except Exception:
        reasons = {}
    return reasons if isinstance(reasons, dict) else {}



def _extract_llm_review_snapshot(reasons_json: str | None) -> dict[str, Any] | None:
    reasons = _parse_reasons_json(reasons_json)
    llm_review = reasons.get("llm_review") if isinstance(reasons.get("llm_review"), dict) else None
    if not isinstance(llm_review, dict):
        return None

    agree = llm_review.get("agree_with_engine")
    if isinstance(agree, str):
        agree = agree.strip().lower() in {"1", "true", "yes", "y"}
    elif not isinstance(agree, bool):
        agree = None

    confidence_raw = llm_review.get("confidence")
    try:
        confidence = float(confidence_raw) if confidence_raw is not None else None
    except Exception:
        confidence = None

    risk_flags_raw = llm_review.get("risk_flags")
    if isinstance(risk_flags_raw, list):
        risk_flags = [str(x) for x in risk_flags_raw[:8]]
    elif isinstance(risk_flags_raw, str) and risk_flags_raw.strip():
        risk_flags = [risk_flags_raw.strip()]
    else:
        risk_flags = []

    review_ts_raw = llm_review.get("review_ts")
    try:
        review_ts = int(review_ts_raw) if review_ts_raw is not None else None
    except Exception:
        review_ts = None

    cache_age_raw = llm_review.get("cache_age_sec")
    try:
        cache_age_sec = int(cache_age_raw) if cache_age_raw is not None else None
    except Exception:
        cache_age_sec = None

    return {
        "status": str(llm_review.get("status") or "unknown"),
        "provider": llm_review.get("provider"),
        "model": llm_review.get("model"),
        "mode": llm_review.get("mode"),
        "gate_decision": llm_review.get("gate_decision"),
        "agree_with_engine": agree,
        "confidence": confidence,
        "thesis_direction": _normalize_direction(llm_review.get("thesis_direction"), fallback="neutral"),
        "execution_direction": _normalize_direction(llm_review.get("execution_direction"), fallback="neutral"),
        "regime_view": str(llm_review.get("regime_view") or "unknown"),
        "summary": llm_review.get("summary"),
        "error": llm_review.get("error"),
        "risk_flags": risk_flags,
        "cached": bool(llm_review.get("cached")),
        "cache_age_sec": cache_age_sec,
        "source": llm_review.get("source"),
        "review_ts": review_ts,
    }


def is_outcome_eligible_under_llm_mode(status: Any, reasons_json: str | None) -> bool:
    """Accept only rows that already have a completed LLM verdict.

    Async LLM mode temporarily parks actionable recommendations in `pending`. Those
    rows — and rows whose LLM review never completed successfully — must not enter
    outcome labeling, UI summaries or calibration datasets.
    """
    status_norm = str(status or "").strip().lower()
    if status_norm in {"pending", "blocked", "no_trade", "suppressed"}:
        return False
    llm_review = _extract_llm_review_snapshot(reasons_json)
    if not isinstance(llm_review, dict):
        return False
    llm_status = str(llm_review.get("status") or "").strip().lower()
    return llm_status in LLM_OUTCOME_READY_STATUSES



def _extract_outcome_directions(outcome_direction: Any, reco_direction: Any, reasons_json: str | None) -> dict[str, Any]:
    raw_direction = None
    execution_direction = None

    reasons = _parse_reasons_json(reasons_json)

    execution_constraints = reasons.get("execution_constraints") if isinstance(reasons.get("execution_constraints"), dict) else {}
    direction_agg = reasons.get("direction_agg") if isinstance(reasons.get("direction_agg"), dict) else {}

    raw_direction = (
        execution_constraints.get("raw_direction")
        or direction_agg.get("raw_direction")
        or direction_agg.get("direction_before_execution")
        or reco_direction
        or outcome_direction
    )
    execution_direction = (
        execution_constraints.get("executable_direction")
        or execution_constraints.get("execution_direction")
        or reco_direction
        or outcome_direction
    )

    raw_direction = _normalize_direction(raw_direction, fallback=_normalize_direction(outcome_direction))
    execution_direction = _normalize_direction(execution_direction, fallback=_normalize_direction(outcome_direction))

    if execution_direction == "neutral" and raw_direction == "short":
        neutral_source = "futures_neutral"
    elif execution_direction == "neutral" and raw_direction == "neutral":
        neutral_source = "true_neutral"
    elif execution_direction == "neutral":
        neutral_source = "other_neutralized"
    else:
        neutral_source = None

    return {
        "raw_direction": raw_direction,
        "execution_direction": execution_direction,
        "neutral_source": neutral_source,
    }


def _accumulate_stat(bucket: dict[str, Any], success: Any, ret: Any) -> None:
    bucket["total"] += 1
    bucket["wins"] += int(success or 0)
    bucket["ret_sum"] += float(ret or 0.0)
    bucket["abs_ret_sum"] += abs(float(ret or 0.0))



def _materialize_stat_rows(grouped: dict[tuple[Any, ...], dict[str, Any]], key_names: list[str], *, sort_key) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, stat in grouped.items():
        total = int(stat["total"])
        wins = int(stat["wins"])
        row = {name: key[idx] for idx, name in enumerate(key_names)}
        row.update({
            "total": total,
            "wins": wins,
            "losses": max(0, total - wins),
            "win_rate": round(wins / total, 3) if total else 0.0,
            "avg_ret": round((float(stat["ret_sum"]) / total) * 100.0, 3) if total else 0.0,
            "avg_abs_ret": round((float(stat["abs_ret_sum"]) / total) * 100.0, 3) if total else 0.0,
        })
        rows.append(row)
    return sorted(rows, key=sort_key)



def get_outcomes_recent_enriched(conn: sqlite3.Connection, limit: int = 200, *, require_llm_verdict: bool = False) -> list[dict[str, Any]]:
    _supported_sql, _supported_params = sql_in_clause("o.bot_type")
    cur = conn.execute(
        f"""SELECT o.rec_id, o.ts, o.venue, o.symbol, o.bot_type, o.direction,
                     o.horizon_sec, o.entry_close, o.exit_close, o.ret, o.success,
                     r.direction AS reco_direction, r.status AS reco_status,
                     r.score, r.confidence, r.expected_rr, r.reasons_json
              FROM reco_outcomes o
              LEFT JOIN recommendations r ON r.rec_id = o.rec_id
              WHERE {_supported_sql} AND COALESCE(r.is_outcome_label_root, 1) = 1
              ORDER BY o.ts DESC
              LIMIT ?""",
        [*_supported_params, int(limit)],
    )
    out: list[dict[str, Any]] = []
    for row in cur.fetchall():
        if require_llm_verdict and not is_outcome_eligible_under_llm_mode(row["reco_status"], row["reasons_json"]):
            continue
        dirs = _extract_outcome_directions(row["direction"], row["reco_direction"], row["reasons_json"])
        llm_review = _extract_llm_review_snapshot(row["reasons_json"])
        out.append({
            "rec_id": row["rec_id"],
            "ts": int(row["ts"]),
            "venue": row["venue"],
            "symbol": row["symbol"],
            "bot_type": row["bot_type"],
            "direction": _normalize_direction(row["direction"]),
            "raw_direction": dirs["raw_direction"],
            "execution_direction": dirs["execution_direction"],
            "neutral_source": dirs["neutral_source"],
            "horizon_sec": int(row["horizon_sec"]),
            "entry_close": float(row["entry_close"]),
            "exit_close": float(row["exit_close"]),
            "ret": float(row["ret"]),
            "success": int(row["success"]),
            "reco_status": row["reco_status"],
            "score": float(row["score"]) if row["score"] is not None else None,
            "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
            "expected_rr": float(row["expected_rr"]) if row["expected_rr"] is not None else None,
            "llm_review": llm_review,
        })
    return out



def get_outcomes_stats(conn: sqlite3.Connection, *, require_llm_verdict: bool = False) -> dict:
    """Aggregate win-rate / return proxies and expose raw vs execution direction splits.

    Neutral execution can hide two very different realities:
    true neutral thesis and linear short neutralisation (raw short -> execution neutral).
    The UI needs both axes to avoid mixing them together.
    """
    _supported_sql, _supported_params = sql_in_clause("o.bot_type")
    cur = conn.execute(
        f"""SELECT o.rec_id, o.ts, o.venue, o.symbol, o.bot_type, o.direction,
                     o.ret, o.success,
                     r.direction AS reco_direction, r.status AS reco_status,
                     r.reasons_json, r.is_outcome_label_root
              FROM reco_outcomes o
              LEFT JOIN recommendations r ON r.rec_id = o.rec_id
              WHERE {_supported_sql}
              ORDER BY o.ts DESC""",
        _supported_params,
    )

    raw_total = 0
    summary_bucket = {"total": 0, "wins": 0, "ret_sum": 0.0, "abs_ret_sum": 0.0}
    cohort_buckets = {
        "actionable": {"total": 0, "wins": 0, "ret_sum": 0.0, "abs_ret_sum": 0.0},
        "shadow_no_trade": {"total": 0, "wins": 0, "ret_sum": 0.0, "abs_ret_sum": 0.0},
    }
    by_bot_bucket: dict[tuple[Any, ...], dict[str, Any]] = {}
    by_symbol_bucket: dict[tuple[Any, ...], dict[str, Any]] = {}
    by_raw_bucket: dict[tuple[Any, ...], dict[str, Any]] = {}
    by_execution_bucket: dict[tuple[Any, ...], dict[str, Any]] = {}
    by_pair_bucket: dict[tuple[Any, ...], dict[str, Any]] = {}
    by_neutral_bucket: dict[tuple[Any, ...], dict[str, Any]] = {}
    by_llm_bucket: dict[tuple[Any, ...], dict[str, Any]] = {}
    by_llm_engine_bucket: dict[tuple[Any, ...], dict[str, Any]] = {}
    by_llm_matrix_bucket: dict[tuple[Any, ...], dict[str, Any]] = {}

    true_neutral_total = 0
    futures_neutral_total = 0
    shadow_no_trade_total = 0
    actionable_total = 0
    executed_audit_total = 0
    llm_summary = {
        "present_total": 0,
        "ok_total": 0,
        "agree_total": 0,
        "disagree_total": 0,
        "error_total": 0,
        "skipped_total": 0,
        "veto_total": 0,
    }

    rows = cur.fetchall()
    for row in rows:
        if require_llm_verdict and not is_outcome_eligible_under_llm_mode(row["reco_status"], row["reasons_json"]):
            continue
        raw_total += 1
        if row["is_outcome_label_root"] is not None and not bool(int(row["is_outcome_label_root"] or 0)):
            continue
        dirs = _extract_outcome_directions(row["direction"], row["reco_direction"], row["reasons_json"])
        raw_direction = dirs["raw_direction"]
        execution_direction = dirs["execution_direction"]
        neutral_source = dirs["neutral_source"]
        success = int(row["success"] or 0)
        ret = float(row["ret"] or 0.0)
        reco_status = str(row["reco_status"] or "").strip().lower()
        try:
            reasons_mapping = _json_loads_mapping_or_default(row["reasons_json"], {})
        except Exception:
            reasons_mapping = {}
        outcome_policy = reasons_mapping.get("outcome_policy") if isinstance(reasons_mapping, dict) else None
        sample_role = str(outcome_policy.get("sample_role") or "") if isinstance(outcome_policy, dict) else ""
        if sample_role == "shadow_no_trade" or (not sample_role and reco_status == "no_trade"):
            cohort_name = "shadow_no_trade"
            shadow_no_trade_total += 1
        else:
            cohort_name = "actionable"
            actionable_total += 1
        _accumulate_stat(cohort_buckets[cohort_name], success, ret)
        if reco_status == "executed":
            executed_audit_total += 1
        llm_review = _extract_llm_review_snapshot(row["reasons_json"])

        if neutral_source == "true_neutral":
            true_neutral_total += 1
        elif neutral_source == "futures_neutral":
            futures_neutral_total += 1

        if llm_review:
            llm_summary["present_total"] += 1
            llm_status = str(llm_review.get("status") or "unknown")
            llm_agree = llm_review.get("agree_with_engine")
            llm_gate = str(llm_review.get("gate_decision") or "")
            if llm_status == "ok":
                llm_summary["ok_total"] += 1
                if llm_agree is True:
                    llm_summary["agree_total"] += 1
                elif llm_agree is False:
                    llm_summary["disagree_total"] += 1
            elif llm_status == "error":
                llm_summary["error_total"] += 1
            elif llm_status == "skipped":
                llm_summary["skipped_total"] += 1
            if llm_gate == "veto":
                llm_summary["veto_total"] += 1

            llm_execution_direction = _normalize_direction(llm_review.get("execution_direction"), fallback="neutral")
            llm_alignment = "agree" if llm_agree is True else "disagree" if llm_agree is False else "unknown"
            llm_gate_decision = llm_gate or "pass"

            llm_bucket_key = (
                llm_status,
                llm_execution_direction,
                llm_alignment,
                llm_gate_decision,
            )
            stat = by_llm_bucket.setdefault(llm_bucket_key, {"total": 0, "wins": 0, "ret_sum": 0.0, "abs_ret_sum": 0.0})
            _accumulate_stat(stat, success, ret)

            llm_engine_key = (
                execution_direction,
                llm_status,
                llm_alignment,
                llm_gate_decision,
            )
            stat = by_llm_engine_bucket.setdefault(llm_engine_key, {"total": 0, "wins": 0, "ret_sum": 0.0, "abs_ret_sum": 0.0})
            _accumulate_stat(stat, success, ret)

            llm_matrix_key = (
                execution_direction,
                llm_execution_direction,
                llm_alignment,
                llm_gate_decision,
                llm_status,
                neutral_source or "",
            )
            stat = by_llm_matrix_bucket.setdefault(llm_matrix_key, {"total": 0, "wins": 0, "ret_sum": 0.0, "abs_ret_sum": 0.0})
            _accumulate_stat(stat, success, ret)

        _accumulate_stat(summary_bucket, success, ret)

        for bucket, key in (
            (by_bot_bucket, (row["bot_type"], raw_direction, execution_direction)),
            (by_symbol_bucket, (row["symbol"], row["bot_type"], raw_direction, execution_direction)),
            (by_raw_bucket, (raw_direction,)),
            (by_execution_bucket, (execution_direction,)),
            (by_pair_bucket, (raw_direction, execution_direction, neutral_source or "")),
        ):
            stat = bucket.setdefault(key, {"total": 0, "wins": 0, "ret_sum": 0.0, "abs_ret_sum": 0.0})
            _accumulate_stat(stat, success, ret)

        neutral_key = (neutral_source or "directional", raw_direction, execution_direction)
        stat = by_neutral_bucket.setdefault(neutral_key, {"total": 0, "wins": 0, "ret_sum": 0.0, "abs_ret_sum": 0.0})
        _accumulate_stat(stat, success, ret)

    def _cohort_summary(bucket: dict[str, Any]) -> dict[str, Any]:
        cohort_total = int(bucket["total"])
        cohort_wins = int(bucket["wins"])
        return {
            "total": cohort_total,
            "wins": cohort_wins,
            "losses": max(0, cohort_total - cohort_wins),
            "win_rate": round(cohort_wins / cohort_total, 3) if cohort_total else None,
            "avg_ret": round((float(bucket["ret_sum"]) / cohort_total) * 100.0, 3) if cohort_total else 0.0,
            "avg_abs_ret": round((float(bucket["abs_ret_sum"]) / cohort_total) * 100.0, 3) if cohort_total else 0.0,
        }

    total = int(summary_bucket["total"])
    wins = int(summary_bucket["wins"])
    summary = {
        "total": total,
        "raw_total": int(raw_total),
        "deduped_duplicates": max(0, int(raw_total) - total),
        "wins": wins,
        "losses": max(0, total - wins),
        "win_rate": round(wins / total, 3) if total else None,
        "avg_ret": round((float(summary_bucket["ret_sum"]) / total) * 100.0, 3) if total else 0.0,
        "avg_abs_ret": round((float(summary_bucket["abs_ret_sum"]) / total) * 100.0, 3) if total else 0.0,
        "true_neutral_total": int(true_neutral_total),
        "futures_neutral_total": int(futures_neutral_total),
        "shadow_no_trade_total": int(shadow_no_trade_total),
        "actionable_total": int(actionable_total),
        "executed_audit_total": int(executed_audit_total),
    }

    by_bot = _materialize_stat_rows(
        by_bot_bucket,
        ["bot_type", "raw_direction", "execution_direction"],
        sort_key=lambda row: (-row["total"], row["bot_type"], row["raw_direction"], row["execution_direction"]),
    )
    by_symbol = _materialize_stat_rows(
        by_symbol_bucket,
        ["symbol", "bot_type", "raw_direction", "execution_direction"],
        sort_key=lambda row: (-row["total"], row["symbol"], row["bot_type"], row["raw_direction"], row["execution_direction"]),
    )
    by_raw_direction = _materialize_stat_rows(
        by_raw_bucket,
        ["raw_direction"],
        sort_key=lambda row: (-row["total"], row["raw_direction"]),
    )
    by_execution_direction = _materialize_stat_rows(
        by_execution_bucket,
        ["execution_direction"],
        sort_key=lambda row: (-row["total"], row["execution_direction"]),
    )
    direction_pairs = _materialize_stat_rows(
        by_pair_bucket,
        ["raw_direction", "execution_direction", "neutral_source"],
        sort_key=lambda row: (-row["total"], row["raw_direction"], row["execution_direction"], row.get("neutral_source") or ""),
    )
    neutral_breakdown = _materialize_stat_rows(
        by_neutral_bucket,
        ["neutral_source", "raw_direction", "execution_direction"],
        sort_key=lambda row: (
            row["neutral_source"] != "true_neutral",
            row["neutral_source"] != "futures_neutral",
            row["neutral_source"] != "other_neutralized",
            row["neutral_source"] != "directional",
            -row["total"],
            row["raw_direction"],
            row["execution_direction"],
        ),
    )
    llm_alignment = _materialize_stat_rows(
        by_llm_bucket,
        ["llm_status", "llm_execution_direction", "llm_alignment", "llm_gate_decision"],
        sort_key=lambda row: (-row["total"], row["llm_status"], row["llm_execution_direction"], row["llm_alignment"], row["llm_gate_decision"]),
    )
    llm_engine_alignment = _materialize_stat_rows(
        by_llm_engine_bucket,
        ["engine_execution_direction", "llm_status", "llm_alignment", "llm_gate_decision"],
        sort_key=lambda row: (-row["total"], row["engine_execution_direction"], row["llm_status"], row["llm_alignment"], row["llm_gate_decision"]),
    )
    llm_engine_matrix = _materialize_stat_rows(
        by_llm_matrix_bucket,
        ["engine_execution_direction", "llm_execution_direction", "llm_alignment", "llm_gate_decision", "llm_status", "neutral_source"],
        sort_key=lambda row: (
            -row["total"],
            row["engine_execution_direction"],
            row["llm_execution_direction"],
            row["llm_alignment"],
            row["llm_gate_decision"],
            row["llm_status"],
            row.get("neutral_source") or "",
        ),
    )

    return {
        "summary": summary,
        "cohorts": {
            "all_roots": _cohort_summary(summary_bucket),
            "actionable": _cohort_summary(cohort_buckets["actionable"]),
            "shadow_no_trade": _cohort_summary(cohort_buckets["shadow_no_trade"]),
        },
        "llm_summary": llm_summary,
        "by_bot": by_bot,
        "by_symbol": by_symbol,
        "by_raw_direction": by_raw_direction,
        "by_execution_direction": by_execution_direction,
        "direction_pairs": direction_pairs,
        "neutral_breakdown": neutral_breakdown,
        "llm_alignment": llm_alignment,
        "llm_engine_alignment": llm_engine_alignment,
        "llm_engine_matrix": llm_engine_matrix,
        "recent": get_outcomes_recent_enriched(conn, limit=120, require_llm_verdict=require_llm_verdict),
    }


# ── Symbol health ─────────────────────────────────────────────────────────────

def get_symbol_health(
    conn: sqlite3.Connection,
    symbols_linear: list[str],
    legacy_symbols_or_stale_sec: list[str] | int = 300,
    stale_sec: int = 300,
    *,
    active_venues: list[str] | None = None,
) -> list[dict]:
    """
    Returns health status for each configured symbol on active venues:
      last_candle_ts: newest 1m candle timestamp
      last_ticker_ts: newest ticker timestamp
      age_sec:        seconds since last closed 1m candle
      ticker_age_sec: seconds since last ticker snapshot
      data_age_sec:   worst age across candle/ticker freshness gates
      status:         'ok' | 'stale' | 'missing' | 'disabled'
      error_count_10m: COLLECT_ERRORs for this symbol in last 10 min
      disabled:       True if SYMBOL_DISABLED is still inside retry window
    """
    if isinstance(legacy_symbols_or_stale_sec, list):
        # Compatibility with the former two-symbol-list signature
        # get_symbol_health(conn, symbols_linear, ...). The product
        # now has only linear symbols, so prefer the second positional list when
        # it is provided and otherwise keep the first positional list.
        if legacy_symbols_or_stale_sec:
            symbols_linear = [str(item) for item in legacy_symbols_or_stale_sec]
    else:
        stale_sec = int(legacy_symbols_or_stale_sec or stale_sec)

    now = now_ts()
    result: list[dict] = []

    # collect recent errors per symbol
    cur = conn.execute(
        """SELECT details_json FROM decision_log
           WHERE action='COLLECT_ERROR' AND ts >= ?""",
        (now - 600,),
    )
    error_counts: dict[tuple[str, str], int] = {}
    for row in cur.fetchall():
        try:
            d = _json_loads_mapping_or_default(row["details_json"], {})
            venue = str(d.get("venue") or "")
            sym = str(d.get("symbol") or "UNKNOWN")
            key = (venue, sym)
            error_counts[key] = error_counts.get(key, 0) + 1
        except Exception:
            pass

    # currently disabled symbols. Use retry_at when available instead of treating any
    # historic disable event in the last 24h as still active.
    cur = conn.execute(
        """SELECT ts, details_json FROM decision_log
           WHERE action='SYMBOL_DISABLED' AND ts >= ?
           ORDER BY ts DESC""",
        (now - 86400,),
    )
    disabled_until: dict[tuple[str, str], int] = {}
    for row in cur.fetchall():
        try:
            d = _json_loads_mapping_or_default(row["details_json"], {})
            venue = str(d.get("venue") or "")
            sym = str(d.get("symbol") or "")
            if not venue or not sym:
                continue
            key = (venue, sym)
            if key in disabled_until:
                continue
            retry_at_raw = d.get("retry_at")
            retry_after_raw = d.get("retry_after_sec")
            retry_at = None
            try:
                if retry_at_raw not in (None, ""):
                    retry_at = int(retry_at_raw)
            except Exception:
                retry_at = None
            if retry_at is None:
                try:
                    retry_after_sec = int(retry_after_raw or 0)
                except Exception:
                    retry_after_sec = 0
                if retry_after_sec > 0:
                    retry_at = int(row["ts"] or 0) + retry_after_sec
                else:
                    retry_at = int(row["ts"] or 0) + 86400
            disabled_until[key] = max(0, int(retry_at or 0))
        except Exception:
            logger.debug("health: disabled parse error", exc_info=True)

    # stale skip counts per symbol in last hour
    cur = conn.execute(
        """SELECT details_json FROM decision_log
           WHERE action='STALE_DATA_SKIP' AND ts >= ?""",
        (now - 3600,),
    )
    stale_counts: dict[tuple[str, str], int] = {}
    for row in cur.fetchall():
        try:
            d = _json_loads_mapping_or_default(row["details_json"], {})
            venue = str(d.get("venue") or "")
            sym = str(d.get("symbol") or "UNKNOWN")
            key = (venue, sym)
            stale_counts[key] = stale_counts.get(key, 0) + 1
        except Exception:
            logger.debug("health: disabled_syms parse error", exc_info=True)

    active_set = {str(v or "").strip().lower() for v in (active_venues or ["linear"])}
    venue_symbols: list[tuple[str, list[str]]] = []
    if "linear" in active_set:
        venue_symbols.append(("linear", symbols_linear))

    for venue, symbols in venue_symbols:
        for raw_sym in symbols:
            sym = str(raw_sym or "").strip().upper()
            if not sym:
                continue
            # Require both recent 1m candles and recent ticker snapshots. The collector
            # can keep candles fresh while ticker collection is degraded; in that state
            # execution/cost assumptions are stale even though the chart looks healthy.
            last_ts = get_latest_ohlcv_ts(conn, venue, sym, 60)
            last_ticker_ts = get_latest_ticker_ts(conn, venue, sym)
            age_sec = (now - last_ts) if last_ts else None
            ticker_age_sec = (now - last_ticker_ts) if last_ticker_ts else None
            is_disabled = int(disabled_until.get((venue, sym), 0) or 0) > now
            candle_missing = last_ts is None
            ticker_missing = last_ticker_ts is None
            candle_stale = age_sec is not None and age_sec > stale_sec
            ticker_stale = ticker_age_sec is not None and ticker_age_sec > stale_sec
            data_age_sec = max(
                age_sec if age_sec is not None else -1,
                ticker_age_sec if ticker_age_sec is not None else -1,
            )
            data_age_sec = None if data_age_sec < 0 else int(data_age_sec)

            if is_disabled and (candle_missing or ticker_missing or candle_stale or ticker_stale):
                status = "disabled"
            elif candle_missing or ticker_missing:
                status = "missing"
            elif candle_stale or ticker_stale:
                status = "stale"
            else:
                status = "ok"

            result.append({
                "venue":           venue,
                "symbol":          sym,
                "last_candle_ts":  last_ts,
                "last_ticker_ts":  last_ticker_ts,
                "age_sec":         age_sec,
                "ticker_age_sec":  ticker_age_sec,
                "data_age_sec":    data_age_sec,
                "status":          status,
                "error_count_10m": error_counts.get((venue, sym), 0),
                "stale_skips_1h":  stale_counts.get((venue, sym), 0),
                "disabled":        is_disabled,
            })

    status_rank = {"disabled": 0, "missing": 1, "stale": 2, "ok": 3}
    return sorted(result, key=lambda x: (status_rank.get(str(x.get("status") or ""), 9), x["symbol"]))

def prune_old_data(conn: sqlite3.Connection, retain_days: int = 7) -> dict[str, int]:
    """Prune old rows from high-growth tables. Call periodically (e.g. once per hour).
    Retains retain_days of data. Returns count of deleted rows per table.
    """
    cutoff = now_ts() - retain_days * 86400
    cutoff_14d = now_ts() - 14 * 86400  # sentiment: keep 14 days for EWMA
    deleted = {}

    # features: keep 1 day (used only for current cycle reference)
    cur = conn.execute("DELETE FROM features WHERE ts < ?", (now_ts() - 86400,))
    deleted["features"] = cur.rowcount

    # market_regime: keep 7 days
    cur = conn.execute("DELETE FROM market_regime WHERE ts < ?", (cutoff,))
    deleted["market_regime"] = cur.rowcount

    # decision_log: keep 7 days
    cur = conn.execute("DELETE FROM decision_log WHERE ts < ?", (cutoff,))
    deleted["decision_log"] = cur.rowcount

    # sentiment: keep 14 days (EWMA uses 7d window with margin)
    cur = conn.execute("DELETE FROM sentiment WHERE ts < ?", (cutoff_14d,))
    deleted["sentiment"] = cur.rowcount

    # recommendations: keep 14 days — MUST match outcomes retention.
    # get_outcomes_with_recs() uses an INNER JOIN on rec_id.
    # If recs are pruned at 7d but outcomes live 14d, the JOIN silently drops
    # all outcomes whose rec was already pruned → calibrator loses half its training data.
    cur = conn.execute("DELETE FROM recommendations WHERE ts < ? AND status NOT IN ('executed','ignored')", (cutoff_14d,))
    deleted["recommendations"] = cur.rowcount

    # reco_outcomes: keep 14 days (calibrator uses up to 6000 recent outcomes)
    cur = conn.execute("DELETE FROM reco_outcomes WHERE ts < ?", (cutoff_14d,))
    deleted["reco_outcomes"] = cur.rowcount

    # Keep the independent denominator for exactly the same retained window as
    # recommendations/outcomes.  Otherwise censored roots disappear earlier and
    # can make the monetary gate fail open.
    cur = conn.execute(
        "DELETE FROM reco_outcome_observability WHERE recommendation_ts < ?",
        (cutoff_14d,),
    )
    deleted["reco_outcome_observability"] = cur.rowcount

    # ohlcv: prune by timeframe, not with one flat horizon.
    # Recommender requires up to 80 daily candles for the 1d vote. A single 30d cutoff
    # silently kills the 1d branch after the system has been running for a month, because
    # tf=86400 can never accumulate enough history again. Keep short TFs compact, but
    # retain enough slow-TF history for the actual inference contract.
    now = now_ts()
    cutoff_ohlcv_1m = now - 30 * 86400
    cutoff_ohlcv_15_30m = now - 90 * 86400
    cutoff_ohlcv_1h_4h = now - 180 * 86400
    cutoff_ohlcv_1d = now - 400 * 86400
    cur = conn.execute(
        """DELETE FROM ohlcv
           WHERE (tf_sec = 60 AND ts < ?)
              OR (tf_sec IN (900, 1800) AND ts < ?)
              OR (tf_sec IN (3600, 14400) AND ts < ?)
              OR (tf_sec >= 86400 AND ts < ?)""",
        (cutoff_ohlcv_1m, cutoff_ohlcv_15_30m, cutoff_ohlcv_1h_4h, cutoff_ohlcv_1d),
    )
    deleted["ohlcv"] = cur.rowcount

    # ticker_snap: keep 2 days (only latest snapshot is used at inference time)
    cur = conn.execute("DELETE FROM ticker_snap WHERE ts < ?", (now_ts() - 2 * 86400,))
    deleted["ticker_snap"] = cur.rowcount

    # funding_rate: keep 7 days (only current value used; history not queried)
    cur = conn.execute("DELETE FROM funding_rate WHERE ts < ?", (cutoff,))
    deleted["funding_rate"] = cur.rowcount

    # runtime_locks: drop stale leader locks so dead workers do not block takeover
    cur = conn.execute("DELETE FROM runtime_locks WHERE heartbeat_ts < ?", (now_ts() - 2 * 86400,))
    deleted["runtime_locks"] = cur.rowcount

    # open_interest: keep 7 days (oi_trend uses last 48 1h candles = 2 days)
    cur = conn.execute("DELETE FROM open_interest WHERE ts < ?", (cutoff,))
    deleted["open_interest"] = cur.rowcount

    conn.commit()
    return deleted



def count_recommendations_for_statuses(
    conn: sqlite3.Connection,
    venue: str | None,
    min_conf: float,
    statuses: list[str],
    snapshot_ts: int | None = None,
    strict_min_conf: bool = False,
) -> int:
    if not statuses:
        return 0
    _supported_sql, _supported_params = sql_in_clause("bot_type")
    placeholders = ",".join("?" for _ in statuses)
    if snapshot_ts is not None:
        q = f"""SELECT status, confidence, reasons_json FROM recommendations WHERE ts = ? AND {_supported_sql} AND status IN ({placeholders})"""
        params: list[Any] = [snapshot_ts, *_supported_params, *statuses]
    else:
        q = f"""SELECT status, confidence, reasons_json FROM recommendations WHERE ts > ? AND {_supported_sql} AND status IN ({placeholders})"""
        params = [now_ts() - 86400, *_supported_params, *statuses]
    if venue:
        q += " AND venue=?"
        params.append(venue)
    cur = conn.execute(q, params)
    count = 0
    for r in cur.fetchall():
        if not _recommended_row_passes_conf_filter(r, min_conf=min_conf, strict_min_conf=strict_min_conf):
            continue
        count += 1
    return count


def count_visible_recommendations(
    conn: sqlite3.Connection,
    venue: str | None,
    min_conf: float,
    snapshot_ts: int | None = None,
    strict_min_conf: bool = False,
) -> int:
    return count_recommendations_for_statuses(
        conn,
        venue,
        min_conf,
        list(ACTIONABLE_RECOMMENDATION_STATUSES),
        snapshot_ts=snapshot_ts,
        strict_min_conf=strict_min_conf,
    )
