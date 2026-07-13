from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from dotenv import load_dotenv

from .llm_review import parse_tf_secs
from .security import KeyStore
from .db_backend import POSTGRES, SQLITE


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DOTENV_LOADED = False


def _maybe_load_dotenv() -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules:
        return
    env_path = _PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
    _DOTENV_LOADED = True


def _env(key: str, default: str | None = None) -> str:
    v = os.getenv(key, default)
    if v is None:
        raise RuntimeError(f"Missing required env var: {key}")
    return v


def _env_json_dict(key: str, default_json: str) -> dict:
    raw = _env(key, default_json)

    # Python stdlib по умолчанию принимает ``NaN``/``Infinity`` как будто это
    # валидный JSON. Для risk-конфига это опасно: не-finite значения могут
    # отключить отдельные лимиты через ``nan``-сравнения или уронить дальнейшую
    # нормализацию. Здесь принимаем только strict JSON и при любом отклонении
    # откатываемся к безопасному default.
    def _strict_json_loads(payload: str) -> dict:
        def _reject_non_finite(token: str):
            raise ValueError(f"non-finite JSON token is not allowed: {token}")

        loaded = json.loads(payload, parse_constant=_reject_non_finite)
        if not isinstance(loaded, dict):
            raise ValueError("json payload must be an object")
        return loaded

    try:
        loaded = _strict_json_loads(raw)
    except Exception:
        loaded = _strict_json_loads(default_json)
    return loaded


def _resolve_project_path(raw: str) -> str:
    value = str(raw or '').strip()
    if not value:
        return str((_PROJECT_ROOT / 'data' / 'app.db').resolve())
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = (_PROJECT_ROOT / candidate).resolve()
    return str(candidate)


def _default_runtime_lock_db_path(db_path: str) -> str:
    base = Path(str(db_path)).expanduser()
    return str(base.with_name(f"{base.stem}.runtime_locks.sqlite"))


def _env_int(key: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.getenv(key)
    try:
        value = int(str(raw if raw not in (None, '') else default).strip())
    except Exception:
        value = int(default)
    if minimum is not None:
        value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return int(value)


def _env_float(key: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    raw = os.getenv(key)
    try:
        value = float(str(raw if raw not in (None, '') else default).strip())
    except Exception:
        value = float(default)
    if not math.isfinite(value):
        value = float(default)
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return float(value)


def _csv_symbols_unique(raw: str) -> list[str]:
    """Нормализует CSV-список символов и убирает дубли с сохранением порядка.

    Для торгового контура это не косметика: дубликаты в env приводят к
    повторному сбору market data по одному и тому же инструменту, раздувают
    нагрузку на публичный API и потенциально создают несколько конкурирующих
    рекомендаций для фактически одного symbol/venue. На bootstrap выгоднее
    нормализовать конфиг один раз, чем надеяться, что все downstream-циклы
    сами будут дедуплицировать вход.
    """
    out: list[str] = []
    seen: set[str] = set()
    for chunk in str(raw or '').split(','):
        symbol = chunk.strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
    return out


def _is_exact_linear_usdt_symbol(symbol: str) -> bool:
    base = str(symbol or "").strip().upper()[:-4] if str(symbol or "").strip().upper().endswith("USDT") else ""
    normalized = str(symbol or "").strip().upper()
    return bool(base and normalized.endswith("USDT") and normalized.isalnum())


def _linear_usdt_symbols_unique(raw: str) -> list[str]:
    """Return only exact Bybit Linear USDT perpetual symbols from operator config.

    Bybit's ``linear`` API category also contains non-USDT linear contracts on
    some endpoints. Product scope for this service is stricter: only exact USDT
    perpetual futures symbols such as ``BTCUSDT``. Filtering here prevents
    accidental collection/scoring of malformed values like ``BTC/USDT`` or
    non-USDT instruments before later execution preflight gets a chance to block
    them.
    """
    return [symbol for symbol in _csv_symbols_unique(raw) if _is_exact_linear_usdt_symbol(symbol)]


@dataclass(frozen=True)
class Settings:
    outcome_horizon_fallback_sec: int
    calib_min_samples: int

    db_path: str
    bybit_base_url: str

    collect_interval_sec: int
    stale_data_max_sec: int
    reco_interval_sec: int
    top_n: int

    venues: list[str]
    symbols_linear: list[str]

    risk_limits: dict
    min_score_to_recommend: float
    min_conf_to_recommend: float

    taker_fee_bps_linear: float

    master_key: str | None
    admin_api_key: str | None
    sentiment_interval_sec: int
    futures_collect_interval_sec: int
    backfill_full_sweep_on_warmup: bool = True
    backfill_per_tf_budget: int = 0
    futures_meta_during_warmup: bool = False
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    runtime_lock_db_path: str = ""
    db_engine: str = SQLITE

    require_conf_gate: bool = False
    mean_reversion_min_score: float = 0.25
    llm_reviewer_enabled: bool = False
    llm_reviewer_mode: str = "advisory"
    llm_reviewer_provider: str = "ollama"
    llm_reviewer_url: str = "http://127.0.0.1:11434"
    llm_reviewer_model: str = "qwen3:8b"
    llm_reviewer_timeout_sec: int = 60
    llm_reviewer_tf_secs: list[int] = field(default_factory=lambda: [15 * 60, 60 * 60, 4 * 60 * 60])
    llm_reviewer_candles_per_tf: int = 32
    llm_reviewer_max_candidates: int = 24
    llm_reviewer_max_workers: int = 2
    llm_reviewer_min_confidence: float = 0.65
    llm_reviewer_cadence_sec: int = 300
    llm_reviewer_pending_timeout_sec: int = 900
    llm_reviewer_ttl_sec: int | None = None
    llm_reviewer_keep_alive: str = "90s"
    reco_ttl_sec: int | None = None
    outcomes_interval_sec: int = 60
    outcomes_max_to_process: int = 200
    reco_republish_cooldown_sec: int = 3600
    collector_max_workers: int = 8
    futures_collect_max_workers: int = 8
    reco_warmup_min_ready_ratio: float = 0.85
    reco_warmup_min_ready_symbols: int = 1
    reco_warmup_log_cooldown_sec: int = 120


def load_settings() -> Settings:
    _maybe_load_dotenv()
    supported_venues = {"linear"}
    venues: list[str] = []
    for raw_venue in _env("VENUES", "linear").split(","):
        venue = str(raw_venue or "").strip().lower()
        if not venue or venue not in supported_venues or venue in venues:
            continue
        venues.append(venue)
    if not venues:
        venues = ["linear"]

    symbols_linear_default = "BTCUSDT,ETHUSDT" if "linear" in venues else ""
    symbols_linear = _linear_usdt_symbols_unique(_env("SYMBOLS_LINEAR", symbols_linear_default)) if "linear" in venues else []

    risk_limits = _env_json_dict(
        "RISK_LIMITS_JSON",
        '{"max_concurrent_bots":1,"max_daily_dd_usdt":10.0,"cooldown_after_loss_min":90,"max_symbol_bots":1,"min_leverage":3,"max_leverage":5,"max_position_notional_usdt":500.0,"max_margin_per_bot_usdt":100.0}',
    )

    master_key = os.getenv("MASTER_KEY", "") or None
    if master_key:
        # Fail fast: шифровальный ключ должен быть валиден ещё на bootstrap,
        # а не только в момент первой операции с секретом.
        KeyStore.from_env(master_key)
    admin_api_key = os.getenv("ADMIN_API_KEY", "") or None

    outcome_horizon_fallback_sec = _env_int(
        "OUTCOME_HORIZON_FALLBACK_SEC",
        _env_int("OUTCOME_HORIZON_SEC", 900, minimum=300),
        minimum=300,
        maximum=7 * 24 * 3600,
    )
    calib_min_samples = _env_int("CALIB_MIN_SAMPLES", 80, minimum=80, maximum=200_000)
    require_conf_gate = _env("REQUIRE_CONF_GATE", "1").strip().lower() in ("1", "true", "yes", "y")

    llm_reviewer_enabled = _env("LLM_REVIEWER_ENABLED", "0").strip().lower() in ("1", "true", "yes", "y")
    llm_reviewer_mode = _env("LLM_REVIEWER_MODE", "advisory").strip().lower()
    if llm_reviewer_mode not in {"advisory", "gate"}:
        llm_reviewer_mode = "advisory"
    llm_reviewer_provider = _env("LLM_REVIEWER_PROVIDER", "ollama").strip().lower() or "ollama"
    llm_reviewer_url = _env("LLM_REVIEWER_URL", "http://127.0.0.1:11434").strip()
    llm_reviewer_model = _env("LLM_REVIEWER_MODEL", "qwen3:8b").strip()
    llm_reviewer_timeout_sec = _env_int("LLM_REVIEWER_TIMEOUT_SEC", 60, minimum=5, maximum=600)
    llm_reviewer_tf_secs = parse_tf_secs(_env("LLM_REVIEWER_TFS", "15m,1h,4h"))
    llm_reviewer_candles_per_tf = _env_int("LLM_REVIEWER_CANDLES_PER_TF", 32, minimum=16, maximum=96)
    llm_reviewer_max_candidates = _env_int("LLM_REVIEWER_MAX_CANDIDATES", 24, minimum=1, maximum=100)
    llm_reviewer_max_workers = _env_int("LLM_REVIEWER_MAX_WORKERS", 2, minimum=1, maximum=32)
    llm_reviewer_min_confidence = _env_float("LLM_REVIEWER_MIN_CONFIDENCE", 0.65, minimum=0.0, maximum=1.0)
    llm_reviewer_cadence_sec = _env_int("LLM_REVIEWER_CADENCE_SEC", 300, minimum=5, maximum=3600)
    llm_reviewer_pending_timeout_sec = _env_int("LLM_REVIEWER_PENDING_TIMEOUT_SEC", 900, minimum=60, maximum=24 * 3600)
    llm_reviewer_ttl_raw = os.getenv("LLM_REVIEWER_TTL_SEC")
    llm_reviewer_ttl_sec = None if llm_reviewer_ttl_raw in (None, "") else _env_int("LLM_REVIEWER_TTL_SEC", 900, minimum=60, maximum=7 * 24 * 3600)
    llm_reviewer_keep_alive = _env("LLM_REVIEWER_KEEP_ALIVE", "90s").strip() or "90s"
    backfill_full_sweep_on_warmup = _env("BACKFILL_FULL_SWEEP_ON_WARMUP", "1").strip().lower() in ("1", "true", "yes", "y")
    backfill_per_tf_budget = _env_int("BACKFILL_PER_TF_BUDGET", 0, minimum=0, maximum=10000)
    futures_meta_during_warmup = _env("FUTURES_META_DURING_WARMUP", "0").strip().lower() in ("1", "true", "yes", "y")
    reco_ttl_raw = os.getenv("RECO_TTL_SEC")
    reco_ttl_sec = None if reco_ttl_raw in (None, "") else _env_int("RECO_TTL_SEC", 180, minimum=180, maximum=7 * 24 * 3600)
    outcomes_interval_sec = _env_int("OUTCOMES_INTERVAL_SEC", 60, minimum=20, maximum=3600)
    outcomes_max_to_process = _env_int("OUTCOMES_MAX_TO_PROCESS", 200, minimum=10, maximum=2000)
    reco_republish_cooldown_sec = _env_int("RECO_REPUBLISH_COOLDOWN_SEC", 3600, minimum=0, maximum=24 * 3600)
    reco_warmup_min_ready_ratio = _env_float("RECO_WARMUP_MIN_READY_RATIO", 0.85, minimum=0.1, maximum=1.0)
    reco_warmup_min_ready_symbols = _env_int("RECO_WARMUP_MIN_READY_SYMBOLS", 1, minimum=1, maximum=10_000)
    reco_warmup_log_cooldown_sec = _env_int("RECO_WARMUP_LOG_COOLDOWN_SEC", 120, minimum=10, maximum=3600)

    db_engine_raw = _env("DB_ENGINE", SQLITE).strip().lower()
    db_engine = POSTGRES if db_engine_raw in {"postgres", "postgresql"} else SQLITE

    if db_engine == POSTGRES:
        db_path = str(os.getenv("DATABASE_URL") or "").strip()
        if not db_path:
            raise RuntimeError("DATABASE_URL is required when DB_ENGINE=postgresql")
        runtime_lock_db_path = str(os.getenv("RUNTIME_LOCK_DATABASE_URL") or db_path).strip() or db_path
    else:
        db_path = _resolve_project_path(_env("DB_PATH", "./data/app.db"))
        runtime_lock_db_path = _resolve_project_path(os.getenv("RUNTIME_LOCK_DB_PATH") or _default_runtime_lock_db_path(db_path))
        if Path(runtime_lock_db_path) == Path(db_path):
            raise RuntimeError("RUNTIME_LOCK_DB_PATH must differ from DB_PATH")

    return Settings(
        db_engine=db_engine,
        require_conf_gate=require_conf_gate,
        db_path=db_path,
        runtime_lock_db_path=runtime_lock_db_path,
        bybit_base_url=_env("BYBIT_BASE_URL", "https://api.bybit.com"),
        collect_interval_sec=_env_int("COLLECT_INTERVAL_SEC", 20, minimum=5, maximum=3600),
        stale_data_max_sec=_env_int("STALE_DATA_MAX_SEC", 300, minimum=60, maximum=24 * 3600),
        reco_interval_sec=_env_int("RECO_INTERVAL_SEC", 20, minimum=5, maximum=3600),
        collector_max_workers=_env_int("COLLECTOR_MAX_WORKERS", 8, minimum=1, maximum=16),
        futures_collect_max_workers=_env_int("FUTURES_COLLECT_MAX_WORKERS", 8, minimum=1, maximum=16),
        top_n=_env_int("TOP_N", 20, minimum=1, maximum=500),
        venues=venues,
        symbols_linear=symbols_linear,
        risk_limits=risk_limits,
        min_score_to_recommend=_env_float("MIN_SCORE_TO_RECOMMEND", 0.08, minimum=-1.0, maximum=1.0),
        min_conf_to_recommend=_env_float("MIN_CONF_TO_RECOMMEND", 0.52, minimum=0.0, maximum=1.0),
        mean_reversion_min_score=_env_float("MEAN_REVERSION_MIN_SCORE", 0.25, minimum=0.0, maximum=1.0),
        taker_fee_bps_linear=_env_float("TAKER_FEE_BPS_LINEAR", 6.0, minimum=0.0, maximum=500.0),
        master_key=master_key,
        admin_api_key=admin_api_key,
        sentiment_interval_sec=_env_int("SENTIMENT_INTERVAL_SEC", 60, minimum=10, maximum=3600),
        futures_collect_interval_sec=_env_int("FUTURES_COLLECT_INTERVAL_SEC", 900, minimum=60, maximum=24 * 3600),
        backfill_full_sweep_on_warmup=backfill_full_sweep_on_warmup,
        backfill_per_tf_budget=backfill_per_tf_budget,
        futures_meta_during_warmup=futures_meta_during_warmup,
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
        outcome_horizon_fallback_sec=outcome_horizon_fallback_sec,
        calib_min_samples=calib_min_samples,
        llm_reviewer_enabled=llm_reviewer_enabled,
        llm_reviewer_mode=llm_reviewer_mode,
        llm_reviewer_provider=llm_reviewer_provider,
        llm_reviewer_url=llm_reviewer_url,
        llm_reviewer_model=llm_reviewer_model,
        llm_reviewer_timeout_sec=llm_reviewer_timeout_sec,
        llm_reviewer_tf_secs=llm_reviewer_tf_secs,
        llm_reviewer_candles_per_tf=llm_reviewer_candles_per_tf,
        llm_reviewer_max_candidates=llm_reviewer_max_candidates,
        llm_reviewer_max_workers=llm_reviewer_max_workers,
        llm_reviewer_min_confidence=llm_reviewer_min_confidence,
        llm_reviewer_cadence_sec=llm_reviewer_cadence_sec,
        llm_reviewer_pending_timeout_sec=llm_reviewer_pending_timeout_sec,
        llm_reviewer_ttl_sec=llm_reviewer_ttl_sec,
        llm_reviewer_keep_alive=llm_reviewer_keep_alive,
        reco_ttl_sec=reco_ttl_sec,
        outcomes_interval_sec=outcomes_interval_sec,
        outcomes_max_to_process=outcomes_max_to_process,
        reco_republish_cooldown_sec=reco_republish_cooldown_sec,
        reco_warmup_min_ready_ratio=reco_warmup_min_ready_ratio,
        reco_warmup_min_ready_symbols=reco_warmup_min_ready_symbols,
        reco_warmup_log_cooldown_sec=reco_warmup_log_cooldown_sec,
    )
