from app.risk import (
    BYBIT_FUTURES_GRID_MAX_CONCURRENT_BOTS,
    BYBIT_FUTURES_GRID_MAX_SYMBOL_BOTS,
    RiskStatus,
    gate_candidate,
    normalize_risk_limits,
)


def test_risk_limits_clamp_to_bybit_futures_grid_bot_product_caps() -> None:
    limits = normalize_risk_limits(
        {
            "max_concurrent_bots": 5000,
            "max_daily_dd_usdt": 200.0,
            "cooldown_after_loss_min": 30,
            "max_symbol_bots": 5000,
        },
        {
            "max_concurrent_bots": 4,
            "max_daily_dd_usdt": 200.0,
            "cooldown_after_loss_min": 30,
            "max_symbol_bots": 1,
        },
    )

    assert limits["max_concurrent_bots"] == BYBIT_FUTURES_GRID_MAX_CONCURRENT_BOTS
    assert limits["max_symbol_bots"] == BYBIT_FUTURES_GRID_MAX_SYMBOL_BOTS


def test_gate_candidate_uses_clamped_futures_grid_bot_caps() -> None:
    raw_limits = {
        "max_concurrent_bots": 5000,
        "max_daily_dd_usdt": 200.0,
        "cooldown_after_loss_min": 30,
        "max_symbol_bots": 5000,
    }
    status = RiskStatus(
        limits=normalize_risk_limits(raw_limits, raw_limits),
        active_bots=BYBIT_FUTURES_GRID_MAX_CONCURRENT_BOTS,
        daily_pnl=0.0,
        daily_dd=0.0,
        cooldown_active=False,
        symbol_bot_counts={"linear:BTCUSDT": BYBIT_FUTURES_GRID_MAX_SYMBOL_BOTS},
    )

    blocks = gate_candidate(None, "linear", "BTCUSDT", raw_limits, cached_status=status)
    codes = {block["code"] for block in blocks}

    assert "MAX_CONCURRENT_BOTS" in codes
    assert "MAX_SYMBOL_BOTS" in codes
