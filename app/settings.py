from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from dotenv import load_dotenv

from .llm_review import parse_tf_secs


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
    try:
        loaded = json.loads(raw)
    except Exception:
        loaded = json.loads(default_json)
    return loaded if isinstance(loaded, dict) else json.loads(default_json)


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
    suffix = base.suffix if base.suffix else ".db"
    return str(base.with_name(f"{base.stem}.locks{suffix}"))


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
    if minimum is not None:
        value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return float(value)


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
    symbols_spot: list[str]
    symbols_linear: list[str]

    risk_limits: dict
    min_score_to_recommend: float
    min_conf_to_recommend: float

    taker_fee_bps_spot: float
    taker_fee_bps_linear: float

    master_key: str | None
    admin_api_key: str | None
    sentiment_interval_sec: int
    futures_collect_interval_sec: int
    telegram_token: str | None
    telegram_chat_id: str | None
    runtime_lock_db_path: str = ""

    require_conf_gate: bool = False
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
    llm_reviewer_keep_alive: str = "90s"
    reco_ttl_sec: int | None = None
    outcomes_interval_sec: int = 60
    outcomes_max_to_process: int = 200
    collector_max_workers: int = 8
    futures_collect_max_workers: int = 8
    reco_warmup_min_ready_ratio: float = 0.85
    reco_warmup_min_ready_symbols: int = 1
    reco_warmup_log_cooldown_sec: int = 120


def load_settings() -> Settings:
    _maybe_load_dotenv()
    supported_venues = {"spot", "linear"}
    venues: list[str] = []
    for raw_venue in _env("VENUES", "spot,linear").split(","):
        venue = str(raw_venue or "").strip().lower()
        if not venue or venue not in supported_venues or venue in venues:
            continue
        venues.append(venue)
    if not venues:
        venues = ["spot", "linear"]

    symbols_spot_default = "BTCUSDT,ETHUSDT" if "spot" in venues else ""
    symbols_linear_default = "BTCUSDT,ETHUSDT" if "linear" in venues else ""
    symbols_spot = [s.strip().upper() for s in _env("SYMBOLS_SPOT", symbols_spot_default).split(",") if s.strip()] if "spot" in venues else []
    symbols_linear = [s.strip().upper() for s in _env("SYMBOLS_LINEAR", symbols_linear_default).split(",") if s.strip()] if "linear" in venues else []

    risk_limits = _env_json_dict(
        "RISK_LIMITS_JSON",
        '{"max_concurrent_bots":4,"max_daily_dd_usdt":200.0,"cooldown_after_loss_min":30,"max_symbol_bots":1}',
    )

    master_key = os.getenv("MASTER_KEY", "") or None
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
    llm_reviewer_keep_alive = _env("LLM_REVIEWER_KEEP_ALIVE", "90s").strip() or "90s"
    reco_ttl_raw = os.getenv("RECO_TTL_SEC")
    reco_ttl_sec = None if reco_ttl_raw in (None, "") else _env_int("RECO_TTL_SEC", 180, minimum=180, maximum=7 * 24 * 3600)
    outcomes_interval_sec = _env_int("OUTCOMES_INTERVAL_SEC", 60, minimum=20, maximum=3600)
    outcomes_max_to_process = _env_int("OUTCOMES_MAX_TO_PROCESS", 200, minimum=10, maximum=2000)
    reco_warmup_min_ready_ratio = _env_float("RECO_WARMUP_MIN_READY_RATIO", 0.85, minimum=0.1, maximum=1.0)
    reco_warmup_min_ready_symbols = _env_int("RECO_WARMUP_MIN_READY_SYMBOLS", 1, minimum=1, maximum=10_000)
    reco_warmup_log_cooldown_sec = _env_int("RECO_WARMUP_LOG_COOLDOWN_SEC", 120, minimum=10, maximum=3600)

    db_path = _resolve_project_path(_env("DB_PATH", "./data/app.db"))
    runtime_lock_db_path = _resolve_project_path(os.getenv("RUNTIME_LOCK_DB_PATH") or _default_runtime_lock_db_path(db_path))

    return Settings(
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
        symbols_spot=symbols_spot,
        symbols_linear=symbols_linear,
        risk_limits=risk_limits,
        min_score_to_recommend=_env_float("MIN_SCORE_TO_RECOMMEND", 0.08, minimum=-1.0, maximum=1.0),
        min_conf_to_recommend=_env_float("MIN_CONF_TO_RECOMMEND", 0.52, minimum=0.0, maximum=1.0),
        taker_fee_bps_spot=_env_float("TAKER_FEE_BPS_SPOT", 10.0, minimum=0.0, maximum=500.0),
        taker_fee_bps_linear=_env_float("TAKER_FEE_BPS_LINEAR", 6.0, minimum=0.0, maximum=500.0),
        master_key=master_key,
        admin_api_key=admin_api_key,
        sentiment_interval_sec=_env_int("SENTIMENT_INTERVAL_SEC", 60, minimum=10, maximum=3600),
        futures_collect_interval_sec=_env_int("FUTURES_COLLECT_INTERVAL_SEC", 900, minimum=60, maximum=24 * 3600),
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
        llm_reviewer_keep_alive=llm_reviewer_keep_alive,
        reco_ttl_sec=reco_ttl_sec,
        outcomes_interval_sec=outcomes_interval_sec,
        outcomes_max_to_process=outcomes_max_to_process,
        reco_warmup_min_ready_ratio=reco_warmup_min_ready_ratio,
        reco_warmup_min_ready_symbols=reco_warmup_min_ready_symbols,
        reco_warmup_log_cooldown_sec=reco_warmup_log_cooldown_sec,
    )
