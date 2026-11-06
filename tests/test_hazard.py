"""pipeline/hazard.py is pure math (no I/O) — see docs/HAZARD_METHODOLOGY.md for the
weights/thresholds these tests exercise. Every assertion here should also make sense
read against that doc; the two are meant to be kept in lockstep."""

from __future__ import annotations

from datetime import date

import pytest

from pipeline.hazard import (
    CONFIDENCE_METHOD,
    FRESH_WITHIN_DAYS,
    STALE_AFTER_DAYS,
    WEIGHTS,
    LakeStaticInputs,
    compute_hazard_score,
    growth_rate,
    input_coverage,
    observation_freshness,
    score_confidence,
    seasonal_anomaly,
    tier_for_score,
)


def test_weights_sum_to_one():
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_growth_rate_computes_fractional_change_between_nearest_observations():
    obs = [(date(2026, 6, 1), 1.0), (date(2026, 7, 1), 1.5)]
    rate = growth_rate(obs, as_of=date(2026, 7, 1), window_days=30)
    assert rate == pytest.approx(0.5)


def test_growth_rate_is_none_without_an_observation_near_the_window_start():
    obs = [(date(2026, 7, 1), 1.5)]
    assert growth_rate(obs, as_of=date(2026, 7, 1), window_days=30) is None


def test_growth_rate_is_none_without_a_latest_observation_on_or_before_as_of():
    obs = [(date(2026, 8, 1), 1.5)]  # only observation is in the future relative to as_of
    assert growth_rate(obs, as_of=date(2026, 7, 1), window_days=30) is None


def test_seasonal_anomaly_compares_against_other_years_same_month():
    obs = [
        (date(2024, 7, 15), 1.0),
        (date(2025, 7, 15), 1.0),
        (date(2026, 7, 15), 1.4),
    ]
    anomaly = seasonal_anomaly(obs, as_of=date(2026, 7, 15))
    assert anomaly == pytest.approx(0.4)  # 1.4 vs baseline mean(1.0, 1.0) = 1.0


def test_seasonal_anomaly_is_none_without_another_years_data_for_the_month():
    obs = [(date(2026, 7, 15), 1.4)]
    assert seasonal_anomaly(obs, as_of=date(2026, 7, 15)) is None


def test_tier_for_score_picks_the_highest_threshold_met():
    assert tier_for_score(0.0) == "normal"
    assert tier_for_score(0.24) == "normal"
    assert tier_for_score(0.25) == "watch"
    assert tier_for_score(0.44) == "watch"
    assert tier_for_score(0.45) == "high"
    assert tier_for_score(0.64) == "high"
    assert tier_for_score(0.65) == "critical"
    assert tier_for_score(1.0) == "critical"


def test_compute_hazard_score_worst_case_inputs_reach_critical():
    # Rapid growth, moraine dam, glacier contact, steep slope, real GLOF history —
    # every component maxed out should land at or above the critical threshold.
    obs = [(date(2026, 4, 1), 1.0), (date(2026, 7, 1), 2.0)]  # +100% over ~90d
    static = LakeStaticInputs(dam_type="moraine", glacier_contact=True, historical_glof=True)
    result = compute_hazard_score(obs, static, slope_deg=40.0, as_of=date(2026, 7, 1))
    assert result.tier == "critical"
    assert result.score >= 0.65


def test_compute_hazard_score_best_case_inputs_stay_normal():
    # No growth signal available, bedrock dam, no glacier contact, flat terrain, no
    # GLOF history — nothing here should push the score into an elevated tier.
    obs = [(date(2026, 7, 1), 1.0)]
    static = LakeStaticInputs(dam_type="bedrock", glacier_contact=False, historical_glof=False)
    result = compute_hazard_score(obs, static, slope_deg=0.0, as_of=date(2026, 7, 1))
    assert result.tier == "normal"


def test_compute_hazard_score_components_are_self_contained_for_reproducibility():
    obs = [(date(2026, 6, 1), 1.0), (date(2026, 7, 1), 1.2)]
    static = LakeStaticInputs(dam_type="ice", glacier_contact=True, historical_glof=False)
    result = compute_hazard_score(obs, static, slope_deg=20.0, as_of=date(2026, 7, 1))

    assert result.components["weights"] == WEIGHTS
    assert "tier_thresholds" in result.components
    assert result.components["raw_inputs"]["dam_type"] == "ice"
    assert result.components["raw_inputs"]["slope_deg"] == 20.0
    assert set(result.components["normalized_risks"]) == set(WEIGHTS)


def test_compute_hazard_score_includes_exposure_without_affecting_tier():
    obs = [(date(2026, 7, 1), 1.0)]
    static = LakeStaticInputs(dam_type="bedrock", glacier_contact=False, historical_glof=False)
    exposure = {"population_within_buffer": 12345.6}

    without = compute_hazard_score(obs, static, slope_deg=0.0, as_of=date(2026, 7, 1))
    with_exposure = compute_hazard_score(
        obs, static, slope_deg=0.0, as_of=date(2026, 7, 1), exposure=exposure
    )

    assert with_exposure.score == without.score
    assert with_exposure.tier == without.tier
    assert with_exposure.components["exposure"] == exposure


# --- Score confidence (heuristic data quality; informational, never affects the tier) ----


def test_input_coverage_is_one_when_every_signal_has_data():
    assert input_coverage(0.1, 0.2, 0.05, "moraine") == pytest.approx(1.0)


def test_input_coverage_drops_by_each_missing_signals_weight():
    # no seasonal anomaly: loses its full weight
    assert input_coverage(0.1, 0.2, None, "moraine") == pytest.approx(1.0 - WEIGHTS["seasonal_anomaly"])
    # area_growth is half 30-day, half 90-day: missing one loses half of that weight
    assert input_coverage(None, 0.2, 0.05, "moraine") == pytest.approx(1.0 - WEIGHTS["area_growth"] / 2)
    # an "unknown" dam type counts as no data
    assert input_coverage(0.1, 0.2, 0.05, "unknown") == pytest.approx(1.0 - WEIGHTS["dam_type"])


def test_input_coverage_with_no_observation_signals_keeps_only_the_static_weights():
    static_share = WEIGHTS["glacier_contact"] + WEIGHTS["slope"] + WEIGHTS["historical_glof"]
    assert input_coverage(None, None, None, "unknown") == pytest.approx(static_share)


def test_observation_freshness_is_full_then_falls_linearly_then_zero():
    as_of = date(2026, 7, 1)
    assert observation_freshness([(date(2026, 7, 1), 1.0)], as_of) == (1.0, 0)
    assert observation_freshness([(date(2026, 6, 16), 1.0)], as_of) == (1.0, FRESH_WITHIN_DAYS)
    mid = (FRESH_WITHIN_DAYS + STALE_AFTER_DAYS) // 2
    newest = date.fromordinal(as_of.toordinal() - mid)
    freshness, age = observation_freshness([(newest, 1.0)], as_of)
    assert age == mid
    assert 0.0 < freshness < 1.0
    assert observation_freshness([(date(2026, 4, 1), 1.0)], as_of)[0] == 0.0  # 91 days old


def test_observation_freshness_without_any_scene_is_zero_and_age_unknown():
    assert observation_freshness([], date(2026, 7, 1)) == (0.0, None)
    # a scene after as_of does not count
    assert observation_freshness([(date(2026, 8, 1), 1.0)], date(2026, 7, 1)) == (0.0, None)


def test_score_confidence_multiplies_coverage_and_freshness_and_records_both():
    conf = score_confidence([(date(2026, 7, 1), 1.0)], date(2026, 7, 1), 0.1, 0.2, 0.05, "moraine")
    assert conf["value"] == 1.0
    assert conf["method"] == CONFIDENCE_METHOD
    assert conf["input_coverage"] == 1.0
    assert conf["freshness"] == 1.0
    assert conf["latest_observation_age_days"] == 0
    assert "not a statistical confidence" in conf["note"]


def test_score_confidence_is_low_when_signals_are_missing_or_the_scene_is_old():
    # one fresh scene only: growth and seasonal signals missing, dam type unknown
    sparse = score_confidence([(date(2026, 7, 1), 1.0)], date(2026, 7, 1), None, None, None, "unknown")
    static_share = WEIGHTS["glacier_contact"] + WEIGHTS["slope"] + WEIGHTS["historical_glof"]
    assert sparse["value"] == pytest.approx(static_share, abs=0.01)  # only the static weights
    # full signals but the newest scene is 91 days old
    stale = score_confidence([(date(2026, 4, 1), 1.0)], date(2026, 7, 1), 0.1, 0.2, 0.05, "moraine")
    assert stale["value"] == 0.0
    assert stale["latest_observation_age_days"] == 91


def test_compute_hazard_score_stores_confidence_without_changing_score_or_tier():
    obs = [(date(2026, 6, 1), 1.0), (date(2026, 7, 1), 1.2)]
    static = LakeStaticInputs(dam_type="ice", glacier_contact=True, historical_glof=False)
    result = compute_hazard_score(obs, static, slope_deg=20.0, as_of=date(2026, 7, 1))

    conf = result.components["confidence"]
    assert 0.0 <= conf["value"] <= 1.0
    assert conf["method"] == CONFIDENCE_METHOD
    # confidence is derived from the same stored inputs, so a stored score can re-derive it
    raw = result.components["raw_inputs"]
    again = score_confidence(
        obs,
        date(2026, 7, 1),
        raw["growth_30d_pct"],
        raw["growth_90d_pct"],
        raw["seasonal_anomaly_pct"],
        raw["dam_type"],
    )
    assert again == conf
