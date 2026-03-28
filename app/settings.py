from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from dotenv import load_dotenv

from .llm_review import parse_tf_secs


_DOTENV_LOADED = False


def _maybe_load_dotenv() -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules:
        return
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
    _DOTENV_LOADED = True


def _env(key: str, default: str | None = None) -> str:
    v = os.getenv(key, default)
    if v is None:
        raise RuntimeError(f"Missing required env var: {key}")
    return v


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

    require_conf_gate: bool = False
    llm_reviewer_enabled: bool = False
    llm_reviewer_mode: str = "advisory"
    llm_reviewer_provider: str = "ollama"
    llm_reviewer_url: str = "http://127.0.0.1:11434"
    llm_reviewer_model: str = "qwen3:8b"
    llm_reviewer_timeout_sec: int = 60
    llm_reviewer_tf_secs: list[int] = field(default_factory=lambda: [15 * 60, 60 * 60, 4 * 60 * 60])
    llm_reviewer_candles_per_tf: int = 32
    llm_reviewer_max_candidates: int = 2
    llm_reviewer_min_confidence: float = 0.65
    llm_reviewer_cadence_sec: int = 300
    reco_ttl_sec: int | None = None
    outcomes_interval_sec: int = 60
    outcomes_max_to_process: int = 200


def load_settings() -> Settings:
    _maybe_load_dotenv()
    venues = [v.strip() for v in _env("VENUES", "spot,linear").split(",") if v.strip()]
    symbols_spot = [s.strip().upper() for s in _env("SYMBOLS_SPOT", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
    symbols_linear = [s.strip().upper() for s in _env("SYMBOLS_LINEAR", "BTCUSDT,ETHUSDT").split(",") if s.strip()]

    risk_limits_json = _env(
        "RISK_LIMITS_JSON",
        '{"max_concurrent_bots":4,"max_daily_dd_usdt":200.0,"cooldown_after_loss_min":30,"max_symbol_bots":1}',
    )
    risk_limits = json.loads(risk_limits_json)

    master_key = os.getenv("MASTER_KEY", "") or None
    admin_api_key = os.getenv("ADMIN_API_KEY", "") or None

    outcome_horizon_fallback_sec = int(os.getenv("OUTCOME_HORIZON_FALLBACK_SEC", os.getenv("OUTCOME_HORIZON_SEC", "900")))
    calib_min_samples = max(80, int(_env("CALIB_MIN_SAMPLES", "80")))
    require_conf_gate = _env("REQUIRE_CONF_GATE", "1").strip().lower() in ("1", "true", "yes", "y")

    llm_reviewer_enabled = _env("LLM_REVIEWER_ENABLED", "0").strip().lower() in ("1", "true", "yes", "y")
    llm_reviewer_mode = _env("LLM_REVIEWER_MODE", "advisory").strip().lower()
    if llm_reviewer_mode not in {"advisory", "gate"}:
        llm_reviewer_mode = "advisory"
    llm_reviewer_provider = _env("LLM_REVIEWER_PROVIDER", "ollama").strip().lower() or "ollama"
    llm_reviewer_url = _env("LLM_REVIEWER_URL", "http://127.0.0.1:11434").strip()
    llm_reviewer_model = _env("LLM_REVIEWER_MODEL", "qwen3:8b").strip()
    llm_reviewer_timeout_sec = max(5, int(_env("LLM_REVIEWER_TIMEOUT_SEC", "60")))
    llm_reviewer_tf_secs = parse_tf_secs(_env("LLM_REVIEWER_TFS", "15m,1h,4h"))
    llm_reviewer_candles_per_tf = max(16, min(96, int(_env("LLM_REVIEWER_CANDLES_PER_TF", "32"))))
    llm_reviewer_max_candidates = max(1, min(100, int(_env("LLM_REVIEWER_MAX_CANDIDATES", "2"))))
    llm_reviewer_min_confidence = max(0.0, min(1.0, float(_env("LLM_REVIEWER_MIN_CONFIDENCE", "0.65"))))
    llm_reviewer_cadence_sec = max(60, int(_env("LLM_REVIEWER_CADENCE_SEC", "300")))
    reco_ttl_raw = os.getenv("RECO_TTL_SEC")
    reco_ttl_sec = None if reco_ttl_raw in (None, "") else max(180, int(reco_ttl_raw))
    outcomes_interval_sec = max(20, int(_env("OUTCOMES_INTERVAL_SEC", "60")))
    outcomes_max_to_process = max(10, min(2000, int(_env("OUTCOMES_MAX_TO_PROCESS", "200"))))

    return Settings(
        require_conf_gate=require_conf_gate,
        db_path=_env("DB_PATH", "./data/app.db"),
        bybit_base_url=_env("BYBIT_BASE_URL", "https://api.bybit.com"),
        collect_interval_sec=int(_env("COLLECT_INTERVAL_SEC", "20")),
        stale_data_max_sec=int(_env("STALE_DATA_MAX_SEC", "300")),
        reco_interval_sec=int(_env("RECO_INTERVAL_SEC", "20")),
        top_n=int(_env("TOP_N", "20")),
        venues=venues,
        symbols_spot=symbols_spot,
        symbols_linear=symbols_linear,
        risk_limits=risk_limits,
        min_score_to_recommend=float(_env("MIN_SCORE_TO_RECOMMEND", "0.08")),
        min_conf_to_recommend=float(_env("MIN_CONF_TO_RECOMMEND", "0.52")),
        taker_fee_bps_spot=float(_env("TAKER_FEE_BPS_SPOT", "10")),
        taker_fee_bps_linear=float(_env("TAKER_FEE_BPS_LINEAR", "6")),
        master_key=master_key,
        admin_api_key=admin_api_key,
        sentiment_interval_sec=int(_env("SENTIMENT_INTERVAL_SEC", "60")),
        futures_collect_interval_sec=int(_env("FUTURES_COLLECT_INTERVAL_SEC", "900")),
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
        llm_reviewer_min_confidence=llm_reviewer_min_confidence,
        llm_reviewer_cadence_sec=llm_reviewer_cadence_sec,
        reco_ttl_sec=reco_ttl_sec,
        outcomes_interval_sec=outcomes_interval_sec,
        outcomes_max_to_process=outcomes_max_to_process,
    )
