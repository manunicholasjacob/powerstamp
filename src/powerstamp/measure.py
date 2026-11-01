"""The measurement engine, the context manager, the decorator and the result.

The engine owns the clock. It samples the selected backend on a background
thread at the requested interval, timestamps every completed sample with
``time.monotonic``, and derives the achieved sampling rate from those
timestamps. No backend reports a rate, so no backend can report a rate it did
not achieve.

The engine also takes a closing read at the moment the measured region ends, so
that a counter backend's region is properly bounded. That closing read is
excluded from the rate calculation, because the interval between the last
cadence sample and the closing read is a fragment of an interval and counting
it would inflate the reported rate on a short run.
"""

from __future__ import annotations

import functools
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import schema as _schema
from .backends import (
    REDUCTION_COUNTER,
    Availability,
    Backend,
    Reduction,
    Sample,
    select_backend,
)
from .schema import (
    KIND_ESTIMATE,
    KIND_MEASUREMENT,
    KIND_NONE,
    Envelope,
    Reason,
    reason,
)
from .uncertainty import (
    COVERAGE_FACTOR,
    JITTER_RSD_WARN,
    MARGINAL_SAMPLES,
    MIN_SAMPLES,
    RATE_SHORTFALL_FRACTION,
    RateReport,
    UncertaintyBudget,
    energy_uncertainty,
    min_duration_s,
    rate_report,
    relative_se_of_stdev,
)

DEFAULT_SAMPLE_HZ = 20.0


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class EnergyResult:
    """What a measurement produced. ``to_dict`` emits the shared envelope."""

    tool_version: str
    duration_s: float
    backend: str
    kind: str
    joules: Optional[float]
    watts_mean: Optional[float]
    rate: Optional[RateReport]
    uncertainty: Optional[UncertaintyBudget]
    reasons: List[Reason] = field(default_factory=list)
    unreadable: List[str] = field(default_factory=list)
    per_domain_j: Dict[str, float] = field(default_factory=dict)
    backend_detail: str = ""
    backend_coverage: str = ""
    reduction: str = ""
    baseline_subtracted: bool = False
    baseline_w: Optional[float] = None
    baseline_source: str = "none"
    joules_before_baseline: Optional[float] = None
    channel_read_hz: Optional[float] = None
    mean_reads_per_sample: Optional[float] = None
    closing_interval_s: Optional[float] = None
    backends_rejected: List[Dict[str, str]] = field(default_factory=list)
    label: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)
    taken_at_utc: Optional[str] = None
    supported: bool = True

    # -- derived ----------------------------------------------------------

    @property
    def verdict(self) -> str:
        return _schema.decide_verdict(self.reasons, self.kind, self.supported)

    @property
    def is_measurement(self) -> bool:
        return self.kind == KIND_MEASUREMENT

    @property
    def achieved_hz(self) -> Optional[float]:
        return self.rate.achieved_hz if self.rate else None

    @property
    def sample_count(self) -> int:
        return self.rate.sample_count if self.rate else 0

    @property
    def expanded_uncertainty_j(self) -> Optional[float]:
        return self.uncertainty.expanded_j if self.uncertainty else None

    def reason_codes(self) -> List[str]:
        return [r.code for r in self.reasons]

    # -- serialisation ----------------------------------------------------

    def measurement_block(self) -> Dict[str, Any]:
        block: Dict[str, Any] = {
            "kind": self.kind,
            "is_measurement": self.kind == KIND_MEASUREMENT,
            "backend": self.backend,
            "backend_detail": self.backend_detail,
            "backend_coverage": self.backend_coverage,
            "reduction": self.reduction,
            "joules": self.joules,
            "watts_mean": self.watts_mean,
            "rate": self.rate.to_dict() if self.rate else None,
            "channel_read_hz": self.channel_read_hz,
            "mean_reads_per_sample": self.mean_reads_per_sample,
            "closing_interval_s": self.closing_interval_s,
            "uncertainty": self.uncertainty.to_dict() if self.uncertainty else None,
            "refusal_rule": {
                "min_samples": MIN_SAMPLES,
                "marginal_samples": MARGINAL_SAMPLES,
                "min_duration_s_at_achieved_rate": (
                    min_duration_s(self.rate.achieved_hz) if self.rate else None
                ),
                "relative_se_of_stdev": (
                    relative_se_of_stdev(self.rate.sample_count) if self.rate else None
                ),
                "rate_shortfall_fraction": RATE_SHORTFALL_FRACTION,
            },
            "per_domain_j": dict(self.per_domain_j),
            "baseline": {
                "subtracted": self.baseline_subtracted,
                "baseline_w": self.baseline_w,
                "source": self.baseline_source,
                "joules_before_baseline": self.joules_before_baseline,
            },
            "backends_rejected": list(self.backends_rejected),
            "label": self.label,
        }
        if self.metadata:
            block["metadata"] = dict(self.metadata)
        block.update(self.extra)
        return block

    def to_envelope(self) -> Envelope:
        return Envelope(
            tool_version=self.tool_version,
            duration_s=self.duration_s,
            verdict=self.verdict,
            reasons=list(self.reasons),
            unreadable=list(self.unreadable),
            measurement=self.measurement_block(),
            taken_at_utc=self.taken_at_utc,
        )

    def to_dict(self) -> Dict[str, Any]:
        return self.to_envelope().to_dict()

    def to_json(self, indent: Optional[int] = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=False)

    def summary(self) -> str:
        """One line a human can read without opening the JSON."""
        if self.joules is None:
            return "%s: %s (%s)" % (self.verdict, self.backend, self.backend_detail)
        u = self.expanded_uncertainty_j
        rate = self.achieved_hz or 0.0
        tail = " +/- %.4g J (k=%g)" % (u, COVERAGE_FACTOR) if u is not None else ""
        return "%s %s: %.6g J%s over %.4g s, %.6g W mean, %.4g Hz achieved from %d samples [%s]" % (
            self.verdict,
            self.kind,
            self.joules,
            tail,
            self.duration_s,
            self.watts_mean if self.watts_mean is not None else float("nan"),
            rate,
            self.sample_count,
            self.backend,
        )


# ---------------------------------------------------------------------------
# Sampling loop
# ---------------------------------------------------------------------------


class _SamplerThread(threading.Thread):
    def __init__(self, backend: Backend, interval_s: float) -> None:
        super().__init__(daemon=True, name="powerstamp-sampler")
        self._backend = backend
        self._interval = interval_s
        self._stop = threading.Event()
        self.samples: List[Sample] = []
        self.errors: List[str] = []

    def run(self) -> None:
        next_due = time.monotonic()
        while not self._stop.is_set():
            try:
                sample = self._backend.read()
            except Exception as exc:  # a read failure is recorded, not fatal
                self.errors.append(str(exc))
                sample = None
            if sample is not None:
                sample.t_monotonic = time.monotonic()
                self.samples.append(sample)
            next_due += self._interval
            wait = next_due - time.monotonic()
            if wait > 0:
                self._stop.wait(wait)
            else:
                # Fell behind. Resynchronise rather than burn a backlog, and
                # let the achieved rate show the shortfall.
                next_due = time.monotonic()

    def stop(self) -> None:
        self._stop.set()


def _measure_region(
    backend: Backend,
    interval_s: float,
    body: Callable[[], Any],
) -> Tuple[List[Sample], float, List[str], Any]:
    """Run ``body`` while sampling ``backend``. Returns samples and duration."""
    sampler = _SamplerThread(backend, interval_s)
    t0 = time.monotonic()
    sampler.start()
    try:
        value = body()
    finally:
        sampler.stop()
        sampler.join(timeout=max(1.0, interval_s * 4))
        t1 = time.monotonic()
        # Closing read: bounds the region for a counter backend.
        try:
            closing = backend.read()
            closing.t_monotonic = time.monotonic()
            sampler.samples.append(closing)
        except Exception as exc:
            sampler.errors.append("closing read failed: %s" % exc)
            closing = None
    return sampler.samples, t1 - t0, sampler.errors, value


def _mean_power_watts(samples: Sequence[Sample]) -> Optional[float]:
    vals = [sum(s.power_w.values()) for s in samples if s.power_w]
    if not vals:
        return None
    return sum(vals) / float(len(vals))


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _build_result(
    backend: Backend,
    availability: Availability,
    rejected: List[Tuple[str, str]],
    samples: List[Sample],
    duration_s: float,
    read_errors: List[str],
    had_closing_read: bool,
    requested_hz: Optional[float],
    baseline_w: Optional[float],
    baseline_source: str,
    label: Optional[str],
    metadata: Dict[str, Any],
    tool_version: str,
) -> EnergyResult:
    supported = availability.available
    reasons: List[Reason] = []
    unreadable: List[str] = []

    # -- rate, measured from the cadence samples only ----------------------
    cadence = samples[:-1] if (had_closing_read and len(samples) >= 2) else samples
    closing_interval = None
    if had_closing_read and len(samples) >= 2:
        closing_interval = samples[-1].t_monotonic - samples[-2].t_monotonic
    rate = rate_report([s.t_monotonic for s in cadence], requested_hz)

    reads = [s.reads for s in cadence if s.reads]
    mean_reads = (sum(reads) / float(len(reads))) if reads else None
    channel_read_hz = (
        rate.achieved_hz * mean_reads if (mean_reads and rate.achieved_hz) else None
    )

    # -- reduce ------------------------------------------------------------
    # Reduce even with no samples, because a backend that could not run still
    # has something to say about why, and that belongs in ``reasons``.
    reduction: Optional[Reduction] = None
    try:
        reduction = backend.reduce(samples)
    except Exception as exc:
        read_errors.append("reduce failed: %s" % exc)

    if reduction is None:
        reduction = Reduction(
            joules=0.0, interval_power_w=[], per_domain_j={}, quantization_j=0.0
        )

    for code, severity, detail in reduction.reasons:
        reasons.append(reason(code, severity, detail))
    unreadable.extend(reduction.unreadable)

    kind = KIND_MEASUREMENT
    if backend.name == "model_estimate":
        kind = KIND_ESTIMATE
    if not supported:
        kind = KIND_NONE

    joules_raw: Optional[float] = reduction.joules if supported else None
    joules = joules_raw
    baseline_subtracted = False

    if joules is not None and baseline_w is not None and duration_s > 0:
        baseline_j = baseline_w * duration_s
        if baseline_j >= joules:
            reasons.append(
                reason(
                    "BASELINE_EXCEEDS_MEASUREMENT",
                    _schema.SEVERITY_INVALIDATING,
                    "The idle baseline of %.6g W over %.4g s is %.6g J, which is not less "
                    "than the measured %.6g J, so the net figure would be negative. The "
                    "unsubtracted figure is reported and the verdict is INVALID."
                    % (baseline_w, duration_s, baseline_j, joules),
                )
            )
        else:
            joules = joules - baseline_j
            baseline_subtracted = True
            reasons.append(
                reason(
                    "BASELINE_SUBTRACTED",
                    _schema.SEVERITY_INFO,
                    "An idle baseline of %.6g W (%s) was subtracted, removing %.6g J."
                    % (baseline_w, baseline_source, baseline_j),
                )
            )
    elif joules is not None:
        reasons.append(
            reason(
                "BASELINE_NOT_SUBTRACTED",
                _schema.SEVERITY_INFO,
                "No idle baseline was subtracted, so the figure includes the machine's "
                "idle draw over the measured region.",
            )
        )

    watts_mean = (
        (joules / duration_s) if (joules is not None and duration_s > 0) else None
    )

    # -- uncertainty -------------------------------------------------------
    budget: Optional[UncertaintyBudget] = None
    if joules is not None:
        series = list(reduction.interval_power_w)
        boundary_reads = 2 if backend.reduction_kind == REDUCTION_COUNTER else 2
        budget = energy_uncertainty(
            joules=joules,
            duration_s=duration_s,
            power_samples=series,
            mean_interval_s=rate.mean_interval_s,
            quantization_j=reduction.quantization_j,
            boundary_reads=boundary_reads,
        )
        note = reduction.extra.get("quantization_note")
        if isinstance(note, str):
            budget.notes.append(note)

    # -- the refusal rule --------------------------------------------------
    if supported:
        if rate.sample_count < 2:
            reasons.append(
                reason(
                    "NO_SAMPLES",
                    _schema.SEVERITY_INVALIDATING,
                    "The sampling loop delivered %d sample(s), so there is no interval "
                    "from which to measure a rate or evaluate scatter."
                    % rate.sample_count,
                )
            )
        elif rate.sample_count < MIN_SAMPLES:
            floor = min_duration_s(rate.achieved_hz)
            floor_text = (
                "about %.4g s at the achieved %.4g Hz" % (floor, rate.achieved_hz)
                if floor is not None
                else "unknown, since no rate could be measured"
            )
            reasons.append(
                reason(
                    "SAMPLE_COUNT_TOO_LOW",
                    _schema.SEVERITY_INVALIDATING,
                    "%d complete sample(s) is below the documented minimum of %d, so no "
                    "confident figure is emitted. The shortest run that can reach the "
                    "minimum here is %s."
                    % (rate.sample_count, MIN_SAMPLES, floor_text),
                )
            )
        elif rate.sample_count < MARGINAL_SAMPLES:
            rse = relative_se_of_stdev(rate.sample_count)
            reasons.append(
                reason(
                    "SAMPLE_COUNT_MARGINAL",
                    _schema.SEVERITY_WARNING,
                    "%d complete sample(s) clears the minimum of %d but stays under %d, so "
                    "the standard deviation behind the Type A term is itself uncertain by "
                    "about %.1f percent."
                    % (
                        rate.sample_count,
                        MIN_SAMPLES,
                        MARGINAL_SAMPLES,
                        100.0 * rse if rse else float("nan"),
                    ),
                )
            )

        if (
            requested_hz
            and rate.achieved_hz > 0
            and rate.achieved_hz < RATE_SHORTFALL_FRACTION * requested_hz
        ):
            reasons.append(
                reason(
                    "RATE_SHORTFALL",
                    _schema.SEVERITY_WARNING,
                    "The achieved rate of %.4g Hz is below %.0f percent of the requested "
                    "%.4g Hz. The reported figure uses the achieved rate; the requested "
                    "rate was not attained."
                    % (
                        rate.achieved_hz,
                        100.0 * RATE_SHORTFALL_FRACTION,
                        requested_hz,
                    ),
                )
            )

        if rate.jitter_rsd is not None and rate.jitter_rsd > JITTER_RSD_WARN:
            reasons.append(
                reason(
                    "SAMPLER_JITTER_HIGH",
                    _schema.SEVERITY_WARNING,
                    "Sample intervals varied with a relative standard deviation of %.2f, "
                    "above the %.2f threshold, so the rectangular bound behind the region "
                    "boundary term understates the timing spread."
                    % (rate.jitter_rsd, JITTER_RSD_WARN),
                )
            )

        if supported and not reduction.per_domain_j and backend.name != "model_estimate":
            reasons.append(
                reason(
                    "NO_READABLE_DOMAINS",
                    _schema.SEVERITY_INVALIDATING,
                    "The backend was available but no domain or rail produced a reading.",
                )
            )

    if not supported:
        unreadable = unreadable or ["all energy domains (no backend)"]

    extra: Dict[str, Any] = dict(reduction.extra)
    if read_errors:
        extra["read_errors"] = list(read_errors)

    return EnergyResult(
        tool_version=tool_version,
        duration_s=duration_s,
        backend=backend.name,
        kind=kind,
        joules=joules,
        watts_mean=watts_mean,
        rate=rate,
        uncertainty=budget,
        reasons=reasons,
        unreadable=unreadable,
        per_domain_j=reduction.per_domain_j,
        backend_detail=availability.detail,
        backend_coverage=backend.coverage,
        reduction=backend.reduction_kind,
        baseline_subtracted=baseline_subtracted,
        baseline_w=baseline_w,
        baseline_source=baseline_source,
        joules_before_baseline=joules_raw,
        channel_read_hz=channel_read_hz,
        mean_reads_per_sample=mean_reads,
        closing_interval_s=closing_interval,
        backends_rejected=[{"backend": n, "detail": d} for n, d in rejected],
        label=label,
        metadata=dict(metadata or {}),
        extra=extra,
        supported=supported,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _tool_version() -> str:
    from . import __version__

    return __version__


class Powerstamp:
    """Context manager that measures the energy of the enclosed block.

    ::

        with Powerstamp(sample_hz=20, label="decode") as ps:
            decode(frames)
        print(ps.result.summary())
        print(ps.result.to_json())

    The result is available as ``.result`` after the block exits, and is always
    present even when the verdict is INVALID or UNSUPPORTED: a refusal is a
    result, not an exception.
    """

    def __init__(
        self,
        sample_hz: float = DEFAULT_SAMPLE_HZ,
        backend: Optional[str] = None,
        baseline_s: Optional[float] = None,
        baseline_w: Optional[float] = None,
        allow_estimate: bool = False,
        label: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        candidates: Optional[List[Backend]] = None,
    ) -> None:
        if sample_hz <= 0:
            raise ValueError("sample_hz must be positive")
        if baseline_s is not None and baseline_w is not None:
            raise ValueError("pass baseline_s or baseline_w, not both")
        self.sample_hz = float(sample_hz)
        self.backend_name = backend
        self.baseline_s = baseline_s
        self.baseline_w = baseline_w
        self.allow_estimate = allow_estimate
        self.label = label
        self.metadata = dict(metadata or {})
        self._candidates = candidates
        self.result: Optional[EnergyResult] = None
        self._backend: Optional[Backend] = None
        self._availability: Optional[Availability] = None
        self._rejected: List[Tuple[str, str]] = []
        self._sampler: Optional[_SamplerThread] = None
        self._t0 = 0.0
        self._baseline_source = "none"

    # -- baseline ----------------------------------------------------------

    def _measure_baseline(self, backend: Backend) -> Optional[float]:
        """Measure idle power for ``baseline_s`` seconds before the region."""
        assert self.baseline_s is not None
        interval = 1.0 / self.sample_hz

        def idle() -> None:
            time.sleep(self.baseline_s)

        samples, duration, _errors, _ = _measure_region(backend, interval, idle)
        if len(samples) < 2 or duration <= 0:
            return None
        try:
            red = backend.reduce(samples)
        except Exception:
            return None
        if duration <= 0:
            return None
        return red.joules / duration

    # -- protocol ----------------------------------------------------------

    def __enter__(self) -> "Powerstamp":
        backend, avail, rejected = select_backend(
            prefer=self.backend_name,
            candidates=self._candidates,
            allow_estimate=self.allow_estimate,
        )
        self._backend = backend
        self._availability = avail
        self._rejected = rejected
        backend.open()

        if avail.available and self.baseline_s is not None:
            measured = self._measure_baseline(backend)
            if measured is not None:
                self.baseline_w = measured
                self._baseline_source = "measured over %.4g s of idle" % self.baseline_s
        elif self.baseline_w is not None:
            self._baseline_source = "provided by the caller"

        self._sampler = _SamplerThread(backend, 1.0 / self.sample_hz)
        self._t0 = time.monotonic()
        if avail.available:
            self._sampler.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        assert self._backend is not None and self._availability is not None
        sampler = self._sampler
        assert sampler is not None
        had_closing = False
        if self._availability.available:
            sampler.stop()
            sampler.join(timeout=max(1.0, 4.0 / self.sample_hz))
        t1 = time.monotonic()
        if self._availability.available:
            try:
                closing = self._backend.read()
                closing.t_monotonic = time.monotonic()
                sampler.samples.append(closing)
                had_closing = True
            except Exception as err:
                sampler.errors.append("closing read failed: %s" % err)
        try:
            self.result = _build_result(
                backend=self._backend,
                availability=self._availability,
                rejected=self._rejected,
                samples=sampler.samples,
                duration_s=t1 - self._t0,
                read_errors=sampler.errors,
                had_closing_read=had_closing,
                requested_hz=self.sample_hz,
                baseline_w=self.baseline_w,
                baseline_source=self._baseline_source,
                label=self.label,
                metadata=self.metadata,
                tool_version=_tool_version(),
            )
        finally:
            self._backend.close()
        return False


def measure(
    func: Callable[..., Any],
    *args: Any,
    sample_hz: float = DEFAULT_SAMPLE_HZ,
    backend: Optional[str] = None,
    baseline_s: Optional[float] = None,
    baseline_w: Optional[float] = None,
    allow_estimate: bool = False,
    label: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    candidates: Optional[List[Backend]] = None,
    **kwargs: Any,
) -> Tuple[Any, EnergyResult]:
    """Call ``func(*args, **kwargs)`` under measurement.

    Returns ``(return_value, result)``.
    """
    stamp = Powerstamp(
        sample_hz=sample_hz,
        backend=backend,
        baseline_s=baseline_s,
        baseline_w=baseline_w,
        allow_estimate=allow_estimate,
        label=label if label is not None else getattr(func, "__name__", None),
        metadata=metadata,
        candidates=candidates,
    )
    with stamp:
        value = func(*args, **kwargs)
    assert stamp.result is not None
    return value, stamp.result


def powerstamp(
    _func: Optional[Callable[..., Any]] = None,
    *,
    sample_hz: float = DEFAULT_SAMPLE_HZ,
    backend: Optional[str] = None,
    baseline_s: Optional[float] = None,
    baseline_w: Optional[float] = None,
    allow_estimate: bool = False,
    label: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    on_result: Optional[Callable[[EnergyResult], None]] = None,
    candidates: Optional[List[Backend]] = None,
) -> Callable[..., Any]:
    """Decorator form.

    ::

        @powerstamp(sample_hz=20)
        def decode(frames):
            ...

        decode(frames)
        print(decode.last_powerstamp.summary())

    The wrapped function returns its own value unchanged. Every result is
    appended to ``fn.powerstamp_results`` and the most recent one is also at
    ``fn.last_powerstamp``, so the decorator never changes a call signature.
    """

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        results: List[EnergyResult] = []

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            value, result = measure(
                func,
                *args,
                sample_hz=sample_hz,
                backend=backend,
                baseline_s=baseline_s,
                baseline_w=baseline_w,
                allow_estimate=allow_estimate,
                label=label if label is not None else func.__name__,
                metadata=metadata,
                candidates=candidates,
                **kwargs,
            )
            results.append(result)
            wrapper.last_powerstamp = result  # type: ignore[attr-defined]
            if on_result is not None:
                on_result(result)
            return value

        wrapper.powerstamp_results = results  # type: ignore[attr-defined]
        wrapper.last_powerstamp = None  # type: ignore[attr-defined]
        return wrapper

    if _func is not None:
        return decorate(_func)
    return decorate
