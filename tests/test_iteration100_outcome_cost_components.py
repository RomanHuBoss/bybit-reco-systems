from __future__ import annotations

import pytest

from app.outcomes import _extract_cost_components


def test_extract_cost_components_backsolves_execution_cost_from_net_cost_and_positive_funding() -> None:
    """Legacy payload с одним net_cost_bps не должен дважды штрафовать funding."""
    execution_bps, funding_bps = _extract_cost_components(
        {
            "cost_model": {
                "net_cost_bps": 11.0,
                "expected_funding_bps": 5.0,
            }
        },
        fallback_execution_bps=15.0,
    )

    assert execution_bps == pytest.approx(6.0)
    assert funding_bps == pytest.approx(5.0)


def test_extract_cost_components_backsolves_execution_cost_from_net_cost_and_negative_funding() -> None:
    """Отрицательный funding (rebate) тоже должен корректно отделяться от execution friction."""
    execution_bps, funding_bps = _extract_cost_components(
        {
            "trade_plan": {
                "cost_model": {
                    "net_cost_bps": 4.0,
                    "expected_funding_bps": -2.0,
                }
            }
        },
        fallback_execution_bps=15.0,
    )

    assert execution_bps == pytest.approx(6.0)
    assert funding_bps == pytest.approx(-2.0)
