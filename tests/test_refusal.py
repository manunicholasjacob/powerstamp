"""The refusal rule, and the achieved rate.

A run too short for the achieved rate must not produce a confident figure. The
rule is stated in ``powerstamp.uncertainty``: at least MIN_SAMPLES complete
samples, with a warning band up to MARGINAL_SAMPLES.
"""

from __future__ import annotations

import math

import pytest

from conftest import FakeConstantPowerBackend
from powerstamp import MARGINAL_SAMPLES, MIN_SAMPLES, Powerstamp
from powerstamp.backends.base import Availability
from powerstamp.measure import _build_result
from powerstamp.uncertainty import (
    JITTER_RSD_WARN,
    RATE_SHORTFALL_FRACTION,
    energy_uncertainty,
    min_duration_s,
    rate_report,
    relative_se_of_stdev,
)


def _synthetic_result(n_samples, hz, watts=10.0, requested_hz=None, rails=2):
    """Build a result from an exactly specified sample series."""
    backend = FakeConstantPowerBackend(watts=watts, rails=rails)
    avail = Availability(available=True, detail="synthetic")
    samples = []
    for i in range(n_samples):
        s = backend.read()
        s.t_monotonic = i / hz
        samples.append(s)
    duration = (n_samples - 1) / hz if n_samples > 1 else 0.0
    return _build_result(
        backend=backend,
        availability=avail,
        rejected=[],
        samples=samples,
        duration_s=duration,
        read_errors=[],
        had_closing_read=False,
        requested_hz=requested_hz if requested_hz is not None else hz,
        baseline_w=None,
        baseline_source="none",
        label="synthetic",
        metadata={},
        tool_version="0.1.0",
    )


# ---------------------------------------------------------------------------
# The documented rule
# ---------------------------------------------------------------------------


def test_min_samples_is_thirty_and_the_rationale_holds():
    assert MIN_SAMPLES == 30
    assert MARGINAL_SAMPLES == 100
    # The stated justification: the relative standard error of a standard
    # deviation is about 13 percent at the minimum and about 24 percent at 10.
    assert relative_se_of_stdev(30) == pytest.approx(0.1313, abs=5e-4)
    assert relative_se_of_stdev(10) == pytest.approx(0.2357, abs=5e-4)
    assert relative_se_of_stdev(100) == pytest.approx(0.0710, abs=5e-4)
    assert relative_se_of_stdev(1) is None


def test_min_duration_at_the_pi5_measured_rate():
    # 21.2 Hz is the rate the author's Raspberry Pi 5 energy work measured.
    # The duration floor the rule implies on that hardware is about 1.42 s.
    assert min_duration_s(21.2) == pytest.approx(1.4151, abs=1e-3)
    assert min_duration_s(0.0) is None


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------


def test_one_sample_is_refused_as_no_samples():
    result = _synthetic_result(n_samples=1, hz=20.0)
    assert result.verdict == "INVALID"
    assert "NO_SAMPLES" in result.reason_codes()
    assert result.rate is not None
    assert result.rate.achieved_hz == 0.0


def test_a_run_too_short_for_the_rate_is_refused():
    result = _synthetic_result(n_samples=5, hz=20.0)
    assert result.verdict == "INVALID"
    codes = result.reason_codes()
    assert "SAMPLE_COUNT_TOO_LOW" in codes
    detail = [r.detail for r in result.reasons if r.code == "SAMPLE_COUNT_TOO_LOW"][0]
    # The refusal states the rule and the duration floor it implies.
    assert "minimum of 30" in detail
    assert "shortest run" in detail
    assert "1.5 s" in detail or "1.5" in detail


def test_exactly_at_the_minimum_is_not_refused():
    result = _synthetic_result(n_samples=MIN_SAMPLES, hz=20.0)
    assert result.verdict == "PASS"
    assert "SAMPLE_COUNT_TOO_LOW" not in result.reason_codes()
    assert "SAMPLE_COUNT_MARGINAL" in result.reason_codes()


def test_one_below_the_minimum_is_refused():
    result = _synthetic_result(n_samples=MIN_SAMPLES - 1, hz=20.0)
    assert result.verdict == "INVALID"
    assert "SAMPLE_COUNT_TOO_LOW" in result.reason_codes()


def test_above_the_marginal_band_has_no_sample_count_warning():
    result = _synthetic_result(n_samples=MARGINAL_SAMPLES + 10, hz=100.0)
    assert result.verdict == "PASS"
    codes = result.reason_codes()
    assert "SAMPLE_COUNT_TOO_LOW" not in codes
    assert "SAMPLE_COUNT_MARGINAL" not in codes


def test_the_refusal_rule_is_published_in_the_envelope():
    result = _synthetic_result(n_samples=50, hz=20.0)
    rule = result.to_dict()["measurement"]["refusal_rule"]
    assert rule["min_samples"] == MIN_SAMPLES
    assert rule["marginal_samples"] == MARGINAL_SAMPLES
    assert rule["min_duration_s_at_achieved_rate"] == pytest.approx(30 / 20.0)
    assert rule["relative_se_of_stdev"] == pytest.approx(relative_se_of_stdev(50))


# ---------------------------------------------------------------------------
# Achieved rate, not requested rate
# ---------------------------------------------------------------------------


def test_the_achieved_rate_is_reported_not_the_requested_one():
    result = _synthetic_result(n_samples=60, hz=10.0, requested_hz=100.0)
    assert result.rate is not None
    assert result.rate.requested_hz == 100.0
    assert result.rate.achieved_hz == pytest.approx(10.0)
    assert "RATE_SHORTFALL" in result.reason_codes()
    detail = [r.detail for r in result.reasons if r.code == "RATE_SHORTFALL"][0]
    assert "was not attained" in detail


def test_meeting_the_requested_rate_raises_no_shortfall():
    result = _synthetic_result(n_samples=60, hz=100.0, requested_hz=100.0)
    assert "RATE_SHORTFALL" not in result.reason_codes()


def test_shortfall_threshold_boundary():
    # Just inside the threshold raises nothing; just outside raises a warning.
    inside = _synthetic_result(n_samples=60, hz=85.0, requested_hz=100.0)
    outside = _synthetic_result(n_samples=60, hz=70.0, requested_hz=100.0)
    assert RATE_SHORTFALL_FRACTION == 0.8
    assert "RATE_SHORTFALL" not in inside.reason_codes()
    assert "RATE_SHORTFALL" in outside.reason_codes()


def test_rate_report_counts_intervals_not_samples():
    # Ten samples span nine intervals. 9 / 0.9 s is 10 Hz, not 11.1 Hz.
    ts = [i * 0.1 for i in range(10)]
    rep = rate_report(ts, requested_hz=10.0)
    assert rep.sample_count == 10
    assert rep.interval_count == 9
    assert rep.span_s == pytest.approx(0.9)
    assert rep.achieved_hz == pytest.approx(10.0)


def test_rate_report_with_one_sample_reports_zero_not_infinity():
    rep = rate_report([1.0], requested_hz=10.0)
    assert rep.achieved_hz == 0.0
    assert rep.interval_count == 0
    assert rep.mean_interval_s is None


def test_high_jitter_is_flagged():
    backend = FakeConstantPowerBackend(watts=10.0)
    avail = Availability(available=True, detail="synthetic")
    # Alternating 10 ms and 100 ms intervals: heavy jitter.
    ts = []
    t = 0.0
    for i in range(60):
        ts.append(t)
        t += 0.01 if i % 2 == 0 else 0.1
    samples = []
    for value in ts:
        s = backend.read()
        s.t_monotonic = value
        samples.append(s)
    result = _build_result(
        backend=backend,
        availability=avail,
        rejected=[],
        samples=samples,
        duration_s=ts[-1],
        read_errors=[],
        had_closing_read=False,
        requested_hz=20.0,
        baseline_w=None,
        baseline_source="none",
        label="jitter",
        metadata={},
        tool_version="0.1.0",
    )
    assert result.rate is not None
    assert result.rate.jitter_rsd is not None
    assert result.rate.jitter_rsd > JITTER_RSD_WARN
    assert "SAMPLER_JITTER_HIGH" in result.reason_codes()


# ---------------------------------------------------------------------------
# The engine end to end, with real threads and a real clock
# ---------------------------------------------------------------------------


def test_engine_measures_a_real_region():
    import time

    backend = FakeConstantPowerBackend(watts=10.0, rails=2)
    with Powerstamp(sample_hz=200, candidates=[backend], label="sleep") as ps:
        time.sleep(1.0)

    result = ps.result
    assert result is not None
    assert result.rate is not None
    # At least the documented minimum arrived, on any reasonable machine.
    assert result.rate.sample_count >= MIN_SAMPLES
    assert result.verdict == "PASS"
    assert result.joules is not None
    # 10 W held for about a second.
    assert result.joules == pytest.approx(10.0, rel=0.2)
    assert result.watts_mean == pytest.approx(10.0, rel=0.2)
    # The achieved rate is measured, so it is a real number and not the request.
    assert result.rate.achieved_hz > 0
    assert result.duration_s == pytest.approx(1.0, abs=0.3)


def test_engine_refuses_a_region_shorter_than_one_sample_interval():
    backend = FakeConstantPowerBackend(watts=10.0)
    # One sample per 2 s requested, and a region that returns immediately.
    with Powerstamp(sample_hz=0.5, candidates=[backend], label="instant") as ps:
        pass
    result = ps.result
    assert result is not None
    assert result.verdict == "INVALID"
    codes = result.reason_codes()
    assert "NO_SAMPLES" in codes or "SAMPLE_COUNT_TOO_LOW" in codes


def test_closing_read_is_excluded_from_the_rate():
    import time

    backend = FakeConstantPowerBackend(watts=5.0)
    with Powerstamp(sample_hz=100, candidates=[backend], label="closing") as ps:
        time.sleep(0.5)
    result = ps.result
    assert result is not None
    assert result.closing_interval_s is not None
    # The closing read happens right after the region ends, so its interval is
    # shorter than the cadence. Counting it would inflate the reported rate.
    assert result.rate is not None
    assert result.rate.mean_interval_s is not None


# ---------------------------------------------------------------------------
# The uncertainty budget
# ---------------------------------------------------------------------------


def test_type_a_term_is_zero_and_flagged_absent_with_one_sample():
    budget = energy_uncertainty(
        joules=1.0,
        duration_s=1.0,
        power_samples=[1.0],
        mean_interval_s=0.05,
        quantization_j=1e-6,
    )
    assert budget.type_a_j == 0.0
    assert any("absent rather than small" in n for n in budget.notes)


def test_timing_term_scales_with_the_sampling_interval():
    slow = energy_uncertainty(
        joules=100.0, duration_s=10.0, power_samples=[10.0] * 50,
        mean_interval_s=0.2, quantization_j=1e-6,
    )
    fast = energy_uncertainty(
        joules=100.0, duration_s=10.0, power_samples=[10.0] * 50,
        mean_interval_s=0.01, quantization_j=1e-6,
    )
    assert slow.timing_j > fast.timing_j
    assert slow.timing_j / fast.timing_j == pytest.approx(20.0)


def test_timing_term_matches_the_documented_formula():
    # P * dt * sqrt(2) / (2 * sqrt(3)) for two independent boundaries.
    budget = energy_uncertainty(
        joules=100.0, duration_s=10.0, power_samples=[10.0] * 50,
        mean_interval_s=0.2, quantization_j=0.0,
    )
    expected = 10.0 * 0.2 * math.sqrt(2) / (2 * math.sqrt(3))
    assert budget.timing_j == pytest.approx(expected)


def test_quantization_term_matches_the_documented_formula():
    budget = energy_uncertainty(
        joules=100.0, duration_s=10.0, power_samples=[10.0] * 50,
        mean_interval_s=0.0, quantization_j=1e-3,
    )
    expected = 1e-3 * math.sqrt(2) / math.sqrt(12)
    assert budget.quantization_j == pytest.approx(expected)


def test_components_combine_in_quadrature():
    budget = energy_uncertainty(
        joules=100.0, duration_s=10.0,
        power_samples=[9.0, 10.0, 11.0] * 20,
        mean_interval_s=0.1, quantization_j=1e-3,
    )
    expected = math.sqrt(
        budget.type_a_j**2 + budget.timing_j**2 + budget.quantization_j**2
    )
    assert budget.standard_j == pytest.approx(expected)
    assert budget.expanded_j == pytest.approx(2.0 * budget.standard_j)
    assert budget.coverage_factor == 2.0


def test_the_budget_names_what_it_excludes():
    budget = energy_uncertainty(
        joules=100.0, duration_s=10.0, power_samples=[10.0] * 50,
        mean_interval_s=0.1, quantization_j=1e-6,
    )
    block = budget.to_dict()
    joined = " ".join(block["excludes"])
    assert "absolute accuracy" in joined
    assert "outside the measured region" in joined


def test_a_passing_result_carries_a_nonzero_expanded_uncertainty():
    result = _synthetic_result(n_samples=200, hz=100.0, watts=10.0)
    assert result.verdict == "PASS"
    assert result.expanded_uncertainty_j is not None
    assert result.expanded_uncertainty_j > 0
    block = result.to_dict()["measurement"]["uncertainty"]
    assert block["coverage_factor"] == 2.0
    assert block["relative_expanded"] > 0
