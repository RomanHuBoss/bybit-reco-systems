from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))



def safe_linear_grid_params(params: dict | None = None, *, reference: float = 100.5, lower: float = 95.0, upper: float = 105.0) -> dict:
    """Build an execution-safe Bybit Linear USDT futures grid payload for API tests.

    Mutating endpoints now fail closed without a full trade_plan, so legacy API tests
    that exercise lifecycle/rollback behavior should seed complete executable plans
    instead of relying on incomplete historical params fixtures.
    """
    payload = dict(params or {})
    grid_count = int(payload.get("grid_count") or payload.get("grid_levels") or 8)
    payload.setdefault("grid_count", grid_count)
    payload.setdefault("grid_levels", grid_count)
    payload.setdefault("grid_type", "arithmetic")
    payload.setdefault("leverage", 1)
    payload.setdefault("margin_mode", "isolated")
    payload.setdefault("price_range_lower", lower)
    payload.setdefault("price_range_upper", upper)
    step_abs = max(0.1, round((float(upper) - float(lower)) / max(1, grid_count + 2), 1))
    step_pct = step_abs / float(reference) * 100.0
    qty = 0.053 if float(reference) < 1000 else 0.001
    payload.setdefault(
        "trade_plan",
        {
            "reference_price": float(reference),
            "grid_type": "arithmetic",
            "levels": {
                "range": {"lower": float(lower), "upper": float(upper)},
                "kill_switch": {"lower": float(lower) - step_abs, "upper": float(upper) + step_abs},
                "grid_step": {"step_abs": step_abs, "step_pct": step_pct},
                "tp_per_leg": {"abs": step_abs, "pct": step_pct},
            },
            "sizing": {
                "qty_per_order": qty,
                "order_notional_usdt": qty * float(reference),
                "estimated_total_order_notional_usdt": qty * float(reference) * grid_count,
                "estimated_margin_required_usdt": qty * float(reference) * grid_count,
            },
        },
    )
    payload.setdefault(
        "economics",
        {
            "liquidation_buffer_pct": 100.0,
            "net_profit_per_grid_pct": max(0.1, step_pct - 0.1),
            "estimated_fee_per_grid_pct": 0.04,
            "estimated_funding_impact_pct": 0.0,
        },
    )
    return payload
