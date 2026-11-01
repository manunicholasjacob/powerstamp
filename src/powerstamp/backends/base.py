"""Backend contract.

The engine owns timing. Backends own reading and reduction. That split is what
makes the achieved sampling rate a measured quantity rather than a constant
copied from the caller's request: the engine timestamps every completed sample
with ``time.monotonic`` and derives the rate from those timestamps, and no
backend is in a position to report a rate it did not achieve.

A backend reports one of two reduction kinds:

``counter``
    The hardware exposes a cumulative energy counter. The engine still samples
    it periodically, because a per interval series is what detects a rollover
    and what the Type A uncertainty term is evaluated from, but the energy
    comes from counter deltas.

``sampler``
    The hardware exposes instantaneous power or the voltage and current needed
    to compute it. Energy is the integral of sampled power over time, which is
    a different measurement from a hardware energy counter and is labelled as
    such in the output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

REDUCTION_COUNTER = "counter"
REDUCTION_SAMPLER = "sampler"


@dataclass
class Availability:
    """Whether a backend can run here, and why not when it cannot."""

    available: bool
    detail: str
    detected_hardware: Optional[str] = None


@dataclass
class Sample:
    """One complete read of every channel the backend covers.

    ``t_monotonic`` is set by the engine, not by the backend.
    """

    t_monotonic: float = 0.0
    #: Cumulative counter readings, in the backend's native integer units,
    #: keyed by domain name. Used by counter backends.
    counters: Dict[str, int] = field(default_factory=dict)
    #: Instantaneous power in watts, keyed by rail name. Used by samplers.
    power_w: Dict[str, float] = field(default_factory=dict)
    #: How many individual hardware reads this one sample required. Reported so
    #: that a consumer can never mistake a read count for a sample count.
    reads: int = 1


@dataclass
class Reduction:
    """A backend's energy figure plus everything needed to qualify it."""

    joules: float
    #: Per interval mean power in watts, one entry per interval between
    #: consecutive samples. Feeds the Type A uncertainty term.
    interval_power_w: List[float]
    #: Per domain or per rail joules, so a package only figure is visible as
    #: a package only figure.
    per_domain_j: Dict[str, float]
    #: Resolution of one read expressed in joules, for the quantization term.
    quantization_j: float
    #: Backend specific facts for the ``measurement`` block.
    extra: Dict[str, object] = field(default_factory=dict)
    #: Reason codes the backend wants recorded, as (code, severity, detail).
    reasons: List[tuple] = field(default_factory=list)
    #: Domains or rails that could not be read.
    unreadable: List[str] = field(default_factory=list)


class Backend:
    """Base class. Subclasses override ``detect``, ``open``, ``read``, ``reduce``."""

    #: Short stable identifier recorded in the output.
    name = "base"
    #: ``counter`` or ``sampler``.
    reduction_kind = REDUCTION_SAMPLER
    #: Human readable description of what the backend physically covers.
    coverage = "unspecified"

    def detect(self) -> Availability:  # pragma: no cover - overridden
        raise NotImplementedError

    def open(self) -> None:
        """Prepare for sampling. Called once before the first read."""
        return None

    def close(self) -> None:
        """Release anything ``open`` acquired."""
        return None

    def read(self) -> Sample:  # pragma: no cover - overridden
        """Take one complete sample of every channel."""
        raise NotImplementedError

    def reduce(self, samples: Sequence[Sample]) -> Reduction:  # pragma: no cover
        raise NotImplementedError


def integrate_power(samples: Sequence[Sample]) -> tuple:
    """Trapezoidal integral of total sampled power.

    Returns ``(joules, interval_mean_power_w, per_rail_joules)``. Rails present
    in only some samples contribute only across the intervals where both
    endpoints have them, which keeps a rail that disappeared mid run from being
    silently treated as zero.
    """
    if len(samples) < 2:
        return 0.0, [], {}

    totals = []
    for s in samples:
        totals.append(sum(s.power_w.values()))

    joules = 0.0
    interval_power: List[float] = []
    per_rail: Dict[str, float] = {}

    for i in range(len(samples) - 1):
        dt = samples[i + 1].t_monotonic - samples[i].t_monotonic
        if dt <= 0:
            continue
        mean_p = 0.5 * (totals[i] + totals[i + 1])
        joules += mean_p * dt
        interval_power.append(mean_p)
        shared = set(samples[i].power_w) & set(samples[i + 1].power_w)
        for rail in shared:
            mp = 0.5 * (samples[i].power_w[rail] + samples[i + 1].power_w[rail])
            per_rail[rail] = per_rail.get(rail, 0.0) + mp * dt

    return joules, interval_power, per_rail


def counter_deltas(
    samples: Sequence[Sample],
    max_range: Dict[str, int],
) -> tuple:
    """Sum per interval counter deltas, correcting a single rollover per interval.

    Returns ``(per_domain_units, interval_total_units, wrapped_domains,
    ambiguous_domains)``.

    A cumulative counter that rolls over at ``max_range`` produces a negative
    delta. Adding ``max_range`` back recovers the true delta as long as the
    counter wrapped at most once inside that interval. If a corrected interval
    delta exceeds half of ``max_range`` then a second rollover inside the same
    interval would have been indistinguishable from no rollover at all, so the
    domain is flagged ambiguous rather than reported as a corrected number.
    That is the honest boundary of what sysfs rollover handling can recover.
    """
    per_domain: Dict[str, float] = {}
    wrapped: List[str] = []
    ambiguous: List[str] = []
    interval_totals: List[float] = []

    for i in range(len(samples) - 1):
        a = samples[i].counters
        b = samples[i + 1].counters
        total = 0.0
        for domain in set(a) & set(b):
            delta = b[domain] - a[domain]
            rng = max_range.get(domain)
            if delta < 0:
                if rng is None or rng <= 0:
                    if domain not in ambiguous:
                        ambiguous.append(domain)
                    continue
                delta += rng
                if domain not in wrapped:
                    wrapped.append(domain)
            if rng is not None and rng > 0 and delta > rng / 2.0:
                if domain not in ambiguous:
                    ambiguous.append(domain)
            per_domain[domain] = per_domain.get(domain, 0.0) + delta
            total += delta
        interval_totals.append(total)

    return per_domain, interval_totals, wrapped, ambiguous
