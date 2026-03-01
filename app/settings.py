from __future__ import annotations

import json
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

def _env(key: str, default: str | None = None) -> str:
    v = os.getenv(key, default)
    if v is None:
        raise RuntimeError(f"Missing required env var: {key}")
    return v

@dataclass(frozen=True)
class Settings:
    require_conf_gate: bool

    outcome_horizon_sec: int
    calib_min_samples: int

    db_path: str
    bybit_base_url: str

    collect_interval_sec: int
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

def load_settings() -> Settings:
    venues = [v.strip() for v in _env("VENUES", "spot,linear").split(",") if v.strip()]
    symbols_spot = [s.strip().upper() for s in _env("SYMBOLS_SPOT", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
    symbols_linear = [s.strip().upper() for s in _env("SYMBOLS_LINEAR", "BTCUSDT,ETHUSDT").split(",") if s.strip()]

    risk_limits_json = _env("RISK_LIMITS_JSON", '{"max_concurrent_bots":4,"max_daily_dd_usdt":200.0,"cooldown_after_loss_min":30,"max_symbol_bots":1}')
    risk_limits = json.loads(risk_limits_json)

    master_key = os.getenv("MASTER_KEY", "") or None

    outcome_horizon_sec = int(_env("OUTCOME_HORIZON_SEC", "1800"))
    calib_min_samples = int(_env("CALIB_MIN_SAMPLES", "80"))

    return Settings(
        db_path=_env("DB_PATH", "./data/app.db"),
        bybit_base_url=_env("BYBIT_BASE_URL", "https://api.bybit.com"),
        collect_interval_sec=int(_env("COLLECT_INTERVAL_SEC", "20")),
        reco_interval_sec=int(_env("RECO_INTERVAL_SEC", "20")),
        top_n=int(_env("TOP_N", "20")),
        venues=venues,
        symbols_spot=symbols_spot,
        symbols_linear=symbols_linear,
        risk_limits=risk_limits,
        min_score_to_recommend=float(_env("MIN_SCORE_TO_RECOMMEND", "0.0")),
        min_conf_to_recommend=float(_env("MIN_CONF_TO_RECOMMEND", "0.30")),
        taker_fee_bps_spot=float(_env("TAKER_FEE_BPS_SPOT", "10")),
        taker_fee_bps_linear=float(_env("TAKER_FEE_BPS_LINEAR", "6")),
        master_key=master_key,
        outcome_horizon_sec=outcome_horizon_sec,
        calib_min_samples=calib_min_samples,
    )
