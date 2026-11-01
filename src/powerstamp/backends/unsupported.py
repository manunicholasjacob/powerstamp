"""The UNSUPPORTED path, and the opt-in ESTIMATE path.

``UnsupportedBackend`` is what runs when no real backend is available. It
reports no joule figure at all. It does not derive one from CPU utilization, a
thermal design power figure or a core count, because a number derived that way
is not a measurement and presenting it in a joules field would be the exact
dishonesty this tool was written against.

``ModelEstimateBackend`` exists so that a caller who genuinely wants a rough
figure can ask for one explicitly, and it is off unless ``allow_estimate`` is
passed. Its output sets ``measurement.kind`` to ``ESTIMATE`` as a field in the
data structure, carries ``ESTIMATE_NOT_MEASUREMENT`` in ``reasons``, and can
never reach a PASS verdict: the verdict rule caps an estimate at UNKNOWN. The
label lives in the record, not only in the documentation, so a consumer that
reads the JSON and never reads the README still cannot mistake it for a
measurement.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Sequence

from .base import REDUCTION_SAMPLER, Availability, Backend, Reduction, Sample, integrate_power


class UnsupportedBackend(Backend):
    name = "unsupported"
    reduction_kind = REDUCTION_SAMPLER
    coverage = "nothing; no energy interface was found"

    def __init__(self, detail: str = "", code: str = "NO_BACKEND") -> None:
        self.detail = detail or "no energy measurement backend is available on this platform"
        # NO_BACKEND when nothing was available, BACKEND_UNAVAILABLE when the
        # caller named a backend that cannot run here. The distinction matters
        # to a consumer deciding whether the platform or the request was wrong.
        self.code = code

    def detect(self) -> Availability:
        return Availability(available=False, detail=self.detail)

    def read(self) -> Sample:
        return Sample(reads=0)

    def reduce(self, samples: Sequence[Sample]) -> Reduction:
        return Reduction(
            joules=0.0,
            interval_power_w=[],
            per_domain_j={},
            quantization_j=0.0,
            extra={
                "coverage_note": self.coverage,
                "why_no_estimate": (
                    "An energy figure derived from CPU utilization or a thermal design "
                    "power number is not a measurement. powerstamp reports UNSUPPORTED "
                    "instead. Pass allow_estimate to opt into a figure labelled ESTIMATE."
                ),
            },
            reasons=[
                (
                    self.code,
                    "invalidating",
                    self.detail,
                )
            ],
            unreadable=["all energy domains (no backend)"],
        )


class ModelEstimateBackend(Backend):
    """An explicitly labelled model based estimate. Never a measurement.

    The model is deliberately crude and its crudeness is reported: process CPU
    time times an assumed watts-per-busy-core figure. It exists to be honest
    about being an estimate, not to be accurate. The assumed figure has no
    hardware basis and is recorded in the output as an assumption.
    """

    name = "model_estimate"
    reduction_kind = REDUCTION_SAMPLER
    coverage = "no hardware; a model over process CPU time"

    #: Assumed watts drawn by one fully busy core. There is no hardware basis
    #: for this value and the output says so.
    ASSUMED_W_PER_BUSY_CORE = 6.0

    def __init__(self, assumed_w_per_busy_core: Optional[float] = None) -> None:
        self.assumed_w = (
            assumed_w_per_busy_core
            if assumed_w_per_busy_core is not None
            else self.ASSUMED_W_PER_BUSY_CORE
        )
        self._cpu_count = os.cpu_count() or 1

    def detect(self) -> Availability:
        return Availability(
            available=True,
            detail="model based estimate, not a measurement",
            detected_hardware="none",
        )

    def read(self) -> Sample:
        times = os.times()
        busy = times[0] + times[1] + times[2] + times[3]
        return Sample(counters={"cpu_seconds_milli": int(busy * 1000)}, reads=1)

    def reduce(self, samples: Sequence[Sample]) -> Reduction:
        joules = 0.0
        interval_power: List[float] = []
        for i in range(len(samples) - 1):
            a = samples[i].counters.get("cpu_seconds_milli")
            b = samples[i + 1].counters.get("cpu_seconds_milli")
            dt = samples[i + 1].t_monotonic - samples[i].t_monotonic
            if a is None or b is None or dt <= 0:
                continue
            cpu_s = max(0.0, (b - a) / 1000.0)
            e = cpu_s * self.assumed_w
            joules += e
            interval_power.append(e / dt)

        return Reduction(
            joules=joules,
            interval_power_w=interval_power,
            per_domain_j={"modelled_cpu": joules},
            quantization_j=0.0,
            extra={
                "model": "process cpu seconds times an assumed watts per busy core",
                "assumed_w_per_busy_core": self.assumed_w,
                "assumption_has_no_hardware_basis": True,
                "logical_cpus": self._cpu_count,
                "coverage_note": self.coverage,
            },
            reasons=[
                (
                    "ESTIMATE_NOT_MEASUREMENT",
                    "warning",
                    "This figure is a model over process CPU time multiplied by an "
                    "assumed %.1f W per busy core. It is not a measurement, it has no "
                    "hardware basis, and it cannot earn a PASS verdict." % self.assumed_w,
                )
            ],
            unreadable=["all energy domains (no hardware interface; modelled)"],
        )
