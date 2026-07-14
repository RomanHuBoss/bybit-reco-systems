from app import calibration, recommender


def _positive_model() -> calibration.LogRegScaler:
    return calibration.LogRegScaler(
        fitted=True,
        coef=[0.1] * calibration.N_FEATURES,
        intercept=0.0,
        platt=calibration.PlattScaler(fitted=True),
        expectancy_status="positive",
        return_samples=100,
        weighted_mean_return=0.02,
        weighted_expected_shortfall=-0.03,
        weighted_return_std=0.01,
        weighted_effective_return_samples=100.0,
        weighted_mean_return_lower_bound=0.01,
        weighted_temporal_mean_return_lower_bound=0.008,
    )


def test_one_censored_root_uses_adverse_sensitivity_without_destroying_model() -> None:
    model = recommender._apply_outcome_observability_gate(
        _positive_model(),
        {
            "matured_total": 101,
            "labeled_total": 100,
            "censored_total": 1,
            "unresolved_total": 0,
            "invalid_labeled_total": 0,
        },
    )
    assert model.fitted is True
    assert model.expectancy_status == "positive"
    assert model.censoring_sensitivity_status == "passed"
    assert model.censoring_assumed_return == -0.03
    assert model.censoring_adjusted_mean_return is not None
    assert model.censoring_adjusted_mean_return > 0.0


def test_excessive_censoring_still_fails_closed() -> None:
    model = recommender._apply_outcome_observability_gate(
        _positive_model(),
        {
            "matured_total": 110,
            "labeled_total": 100,
            "censored_total": 10,
            "unresolved_total": 0,
            "invalid_labeled_total": 0,
        },
    )
    assert model.fitted is False
    assert model.expectancy_status == "censored"
    assert model.censoring_sensitivity_status == "failed"


def test_unresolved_root_remains_hard_block() -> None:
    model = recommender._apply_outcome_observability_gate(
        _positive_model(),
        {
            "matured_total": 101,
            "labeled_total": 100,
            "censored_total": 0,
            "unresolved_total": 1,
            "invalid_labeled_total": 0,
        },
    )
    assert model.fitted is False
    assert model.expectancy_status == "censored"
    assert model.censoring_sensitivity_status == "hard_block"


def test_adverse_imputation_can_overturn_weak_positive_edge() -> None:
    model = _positive_model()
    model.weighted_mean_return = 0.0001
    model.weighted_mean_return_lower_bound = 0.00001
    model.weighted_temporal_mean_return_lower_bound = 0.00001
    gated = recommender._apply_outcome_observability_gate(
        model,
        {
            "matured_total": 101,
            "labeled_total": 100,
            "censored_total": 1,
            "unresolved_total": 0,
            "invalid_labeled_total": 0,
        },
    )
    assert gated.fitted is False
    assert gated.censoring_sensitivity_status == "failed"
    assert gated.censoring_adjusted_mean_return is not None
    assert gated.censoring_adjusted_mean_return < 0.0
