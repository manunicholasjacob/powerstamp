"""Backend registry and detection.

Detection never guesses. Each backend is asked whether it can run, and a
backend that cannot run says why. If none can, the result is UNSUPPORTED and
carries every reason it collected, so the caller sees "powercap sysfs present
but energy_uj is not readable" rather than a bare failure.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .android import AndroidBackend
from .base import (
    REDUCTION_COUNTER,
    REDUCTION_SAMPLER,
    Availability,
    Backend,
    Reduction,
    Sample,
    counter_deltas,
    integrate_power,
)
from .nvml import NvmlBackend
from .rapl import RaplBackend
from .rpi_pmic import RpiPmicBackend
from .unsupported import ModelEstimateBackend, UnsupportedBackend

#: Detection order. RAPL first because it is the most widely available real
#: counter; the PMIC before the fuel gauge because a Raspberry Pi 5 rail
#: reading is a far tighter scope than a battery side reading.
BACKEND_ORDER: Tuple[str, ...] = ("rapl", "nvml", "rpi_pmic", "android_fuel_gauge")

BACKEND_CLASSES: Dict[str, Any] = {
    "rapl": RaplBackend,
    "nvml": NvmlBackend,
    "rpi_pmic": RpiPmicBackend,
    "android_fuel_gauge": AndroidBackend,
    "model_estimate": ModelEstimateBackend,
    "unsupported": UnsupportedBackend,
}


def available_backend_names() -> Tuple[str, ...]:
    return tuple(BACKEND_CLASSES)


def detect_backends(
    candidates: Optional[List[Backend]] = None,
) -> List[Tuple[Backend, Availability]]:
    """Probe every real backend and return each one with its availability."""
    if candidates is None:
        candidates = [BACKEND_CLASSES[n]() for n in BACKEND_ORDER]
    out = []
    for backend in candidates:
        try:
            avail = backend.detect()
        except Exception as exc:  # a broken probe is an unavailable backend
            avail = Availability(
                available=False, detail="%s probe raised %s" % (backend.name, exc)
            )
        out.append((backend, avail))
    return out


def select_backend(
    prefer: Optional[str] = None,
    candidates: Optional[List[Backend]] = None,
    allow_estimate: bool = False,
) -> Tuple[Backend, Availability, List[Tuple[str, str]]]:
    """Pick a backend.

    Returns ``(backend, availability, rejected)`` where ``rejected`` is a list
    of ``(name, detail)`` for every backend that could not run, so the caller
    can report what was tried.
    """
    probed = detect_backends(candidates)
    rejected: List[Tuple[str, str]] = []

    if prefer is not None:
        for backend, avail in probed:
            if backend.name != prefer:
                continue
            if avail.available:
                return backend, avail, rejected
            return (
                UnsupportedBackend(
                    detail="backend %r was requested but is unavailable: %s"
                    % (prefer, avail.detail),
                    code="BACKEND_UNAVAILABLE",
                ),
                Availability(available=False, detail=avail.detail),
                [(backend.name, avail.detail)],
            )
        if prefer in ("model_estimate",):
            backend = ModelEstimateBackend()
            return backend, backend.detect(), rejected
        if prefer == "unsupported":
            backend = UnsupportedBackend(detail="backend 'unsupported' was requested")
            return backend, backend.detect(), rejected
        return (
            UnsupportedBackend(
                detail="no backend named %r" % prefer, code="BACKEND_UNAVAILABLE"
            ),
            Availability(available=False, detail="no backend named %r" % prefer),
            rejected,
        )

    for backend, avail in probed:
        if avail.available:
            return backend, avail, rejected
        rejected.append((backend.name, avail.detail))

    if allow_estimate:
        backend = ModelEstimateBackend()
        return backend, backend.detect(), rejected

    detail = "no energy measurement backend is available on this platform"
    if rejected:
        detail = detail + "; tried " + "; ".join(
            "%s (%s)" % (n, d) for n, d in rejected
        )
    unsupported = UnsupportedBackend(detail=detail)
    return unsupported, unsupported.detect(), rejected


__all__ = [
    "REDUCTION_COUNTER",
    "REDUCTION_SAMPLER",
    "Availability",
    "Backend",
    "Reduction",
    "Sample",
    "AndroidBackend",
    "ModelEstimateBackend",
    "NvmlBackend",
    "RaplBackend",
    "RpiPmicBackend",
    "UnsupportedBackend",
    "BACKEND_CLASSES",
    "BACKEND_ORDER",
    "available_backend_names",
    "counter_deltas",
    "detect_backends",
    "integrate_power",
    "select_backend",
]
