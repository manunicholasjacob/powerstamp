"""Sampling statistics, the uncertainty budget, and the refusal rule.

Everything in this module is deliberately explicit about where each number
comes from, because the whole point of the tool is that the error bar is not
decoration.

The uncertainty budget follows the structure of JCGM 100:2008 (the GUM): a
Type A component evaluated from the observed scatter of the samples, and Type B
components evaluated from bounds on effects that the samples cannot show. A
Type B bound of half width ``a`` drawn from a rectangular distribution
contributes ``a / sqrt(3)`` as a standard uncertainty. The components are
combined in quadrature and reported both at k = 1 and at k = 2.

What the budget does NOT include, stated plainly because omitting it silently
would be the same failure the tool exists to prevent:

* the absolute accuracy of the underlying counter or ADC, which no vendor in
  the backend matrix publishes for its energy path;
* any error from the measured region not being the only load on the machine;
* for a battery side reading, the share of the energy that belongs to the
  display, the modem or the radio.

So the reported figure is a repeatability and discretisation budget, not a
calibration budget. The README says the same thing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# The refusal rule.
# ---------------------------------------------------------------------------

#: Minimum number of complete samples for a confident figure.
#:
#: Chosen at 30 for two reasons that are both checkable:
#:
#: 1. The Type A term uses a sample standard deviation, and the relative
#:    standard error of a standard deviation is approximately
#:    ``1 / sqrt(2 * (n - 1))``. At n = 30 that is about 13 percent, so the
#:    error bar is loose but still worth more than its own noise. At n = 10 it
#:    is about 24 percent, at which point the error bar is less trustworthy
#:    than the number it is supposed to qualify.
#: 2. It forces the duration floor into the open. The floor is
#:    ``MIN_SAMPLES / achieved_hz``, so on hardware whose achieved rate is
#:    21.2 Hz the shortest run that can earn a confident verdict is about
#:    1.42 s. A tool that accepts a 50 ms run on that hardware is reporting
#:    one or two samples and an error bar it invented.
MIN_SAMPLES = 30

#: Below this count the figure is reported with a warning rather than refused.
#: The relative standard error of the standard deviation falls to about
#: 7 percent at n = 100, which is where the Type A term stops being the
#: loosest part of the budget on the backends in the matrix.
MARGINAL_SAMPLES = 100

#: Fraction of the requested rate below which the shortfall is called out.
#: A sampler that delivers less than 80 percent of what was asked for is not
#: running at the rate the caller believes.
RATE_SHORTFALL_FRACTION = 0.8

#: Relative standard deviation of the sample interval above which the timing
#: term is called out as dominating. Rectangular-bound reasoning assumes the
#: interval is roughly regular; at this much jitter it is not.
JITTER_RSD_WARN = 0.25

#: Coverage factor reported alongside the expanded uncertainty.
COVERAGE_FACTOR = 2.0


def relative_se_of_stdev(n: int) -> Optional[float]:
    """Relative standard error of a sample standard deviation, 1/sqrt(2(n-1))."""
    if n < 2:
        return None
    return 1.0 / math.sqrt(2.0 * (n - 1))


def min_duration_s(achieved_hz: float, min_samples: int = MIN_SAMPLES) -> Optional[float]:
    """Shortest run that can reach ``min_samples`` at the achieved rate."""
    if achieved_hz <= 0.0:
        return None
    return min_samples / achieved_hz


@dataclass
class RateReport:
    """What the sampling loop actually achieved, as opposed to what was asked."""

    requested_hz: Optional[float]
    achieved_hz: float
    sample_count: int
    interval_count: int
    span_s: float
    mean_interval_s: Optional[float]
    stdev_interval_s: Optional[float]
    min_interval_s: Optional[float]
    max_interval_s: Optional[float]
    jitter_rsd: Optional[float]

    def to_dict(self) -> Dict[str, object]:
        return {
            "requested_hz": self.requested_hz,
            "achieved_hz": self.achieved_hz,
            "sample_count": self.sample_count,
            "interval_count": self.interval_count,
            "span_s": self.span_s,
            "mean_interval_s": self.mean_interval_s,
            "stdev_interval_s": self.stdev_interval_s,
            "min_interval_s": self.min_interval_s,
            "max_interval_s": self.max_interval_s,
            "jitter_relative_stdev": self.jitter_rsd,
        }


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / float(len(xs))


def _stdev(xs: Sequence[float]) -> Optional[float]:
    """Sample standard deviation with Bessel's correction, or None below n = 2."""
    n = len(xs)
    if n < 2:
        return None
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / float(n - 1)
    return math.sqrt(var)


def rate_report(timestamps: Sequence[float], requested_hz: Optional[float]) -> RateReport:
    """Measure the achieved sampling rate from monotonic sample timestamps.

    The achieved rate is ``intervals / span``, not ``samples / span``, and it
    counts complete samples, never the number of individual reads that went
    into a sample. A per-read count on hardware that reads a voltage channel
    and a current channel separately reports roughly twice the real rate, and
    that artefact is the specific error this tool was built to make impossible.
    """
    n = len(timestamps)
    if n < 2:
        return RateReport(
            requested_hz=requested_hz,
            achieved_hz=0.0,
            sample_count=n,
            interval_count=0,
            span_s=0.0,
            mean_interval_s=None,
            stdev_interval_s=None,
            min_interval_s=None,
            max_interval_s=None,
            jitter_rsd=None,
        )

    intervals = [timestamps[i + 1] - timestamps[i] for i in range(n - 1)]
    span = timestamps[-1] - timestamps[0]
    achieved = (len(intervals) / span) if span > 0 else 0.0
    mean_iv = _mean(intervals)
    sd_iv = _stdev(intervals)
    rsd = (sd_iv / mean_iv) if (sd_iv is not None and mean_iv > 0) else None

    return RateReport(
        requested_hz=requested_hz,
        achieved_hz=achieved,
        sample_count=n,
        interval_count=len(intervals),
        span_s=span,
        mean_interval_s=mean_iv,
        stdev_interval_s=sd_iv,
        min_interval_s=min(intervals),
        max_interval_s=max(intervals),
        jitter_rsd=rsd,
    )


@dataclass
class UncertaintyBudget:
    """The combined standard uncertainty on an energy figure, in joules."""

    type_a_j: float
    timing_j: float
    quantization_j: float
    standard_j: float
    expanded_j: float
    coverage_factor: float = COVERAGE_FACTOR
    relative_expanded: Optional[float] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "standard_uncertainty_j": self.standard_j,
            "expanded_uncertainty_j": self.expanded_j,
            "coverage_factor": self.coverage_factor,
            "relative_expanded": self.relative_expanded,
            "components_j": {
                "type_a_sample_scatter": self.type_a_j,
                "type_b_region_boundary_timing": self.timing_j,
                "type_b_counter_quantization": self.quantization_j,
            },
            "excludes": [
                "absolute accuracy of the counter or ADC, which the vendors do not publish",
                "contamination from load outside the measured region",
                "attribution of a battery side reading to the measured region",
            ],
            "notes": list(self.notes),
        }


def energy_uncertainty(
    joules: float,
    duration_s: float,
    power_samples: Sequence[float],
    mean_interval_s: Optional[float],
    quantization_j: float,
    boundary_reads: int = 2,
) -> UncertaintyBudget:
    """Build the uncertainty budget for an energy figure.

    Parameters
    ----------
    joules:
        The energy figure the budget qualifies.
    duration_s:
        Wall clock duration of the measured region.
    power_samples:
        Per interval mean power in watts. For a counter backend these are the
        counter deltas divided by their interval; for a sampling backend they
        are the sampled power values. Either way the scatter of this series is
        what the Type A term is evaluated from.
    mean_interval_s:
        Mean sampling interval, used for the region boundary term.
    quantization_j:
        Resolution of one counter or ADC read, expressed in joules.
    boundary_reads:
        How many reads define the region. Two for a start and an end read.

    Notes on each term
    ------------------
    Type A: the standard error of the mean power is ``s / sqrt(n)``, and the
    energy is the mean power times the duration, so the Type A term on the
    energy is ``duration * s / sqrt(n)``. With fewer than two samples there is
    no scatter to evaluate and the term is reported as zero, which is why the
    refusal rule exists: a zero Type A term on a one sample run is not a small
    uncertainty, it is an absent one.

    Type B, region boundary: the start and the end of the measured region each
    fall somewhere inside one sampling interval, so each boundary carries a
    rectangular error of half width ``P * dt / 2``, contributing
    ``P * dt / (2 * sqrt(3))``. Independent boundaries combine in quadrature,
    giving ``P * dt * sqrt(boundary_reads) / (2 * sqrt(3))``. This term is the
    direct cost of a low sampling rate and it is the reason the achieved rate
    belongs in the output next to the joule figure.

    Type B, quantization: one read of an integer counter carries a rectangular
    error of half width ``q / 2``, contributing ``q / sqrt(12)``. The region
    needs ``boundary_reads`` of them, so the term is
    ``q * sqrt(boundary_reads) / sqrt(12)``.
    """
    n = len(power_samples)
    mean_power = (joules / duration_s) if duration_s > 0 else 0.0
    notes: List[str] = []

    sd_power = _stdev(power_samples)
    if sd_power is None:
        type_a = 0.0
        notes.append(
            "Type A term is zero because fewer than two power samples exist, "
            "which means it is absent rather than small."
        )
    else:
        type_a = duration_s * sd_power / math.sqrt(n)

    if mean_interval_s is None or mean_interval_s <= 0.0:
        timing = 0.0
        notes.append(
            "Region boundary term is zero because no sampling interval was observed."
        )
    else:
        timing = (
            mean_power
            * mean_interval_s
            * math.sqrt(boundary_reads)
            / (2.0 * math.sqrt(3.0))
        )

    quant = quantization_j * math.sqrt(boundary_reads) / math.sqrt(12.0)

    standard = math.sqrt(type_a**2 + timing**2 + quant**2)
    expanded = COVERAGE_FACTOR * standard
    rel = (expanded / joules) if joules > 0 else None

    if sd_power is not None and timing > type_a and timing > 0:
        notes.append(
            "The region boundary term dominates, so the figure is limited by the "
            "achieved sampling rate rather than by workload variability."
        )

    return UncertaintyBudget(
        type_a_j=type_a,
        timing_j=timing,
        quantization_j=quant,
        standard_j=standard,
        expanded_j=expanded,
        relative_expanded=rel,
        notes=notes,
    )
