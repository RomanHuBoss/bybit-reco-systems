from __future__ import annotations

import pytest

from app.outcomes import _extract_cost_components, _funding_cost_bps_for_outcome_label


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


def test_outcome_label_does_not_credit_funding_receipt_as_edge() -> None:
    assert _funding_cost_bps_for_outcome_label(5.0) == pytest.approx(5.0)
    assert _funding_cost_bps_for_outcome_label(0.0) == pytest.approx(0.0)
    assert _funding_cost_bps_for_outcome_label(-2.0) == pytest.approx(0.0)
