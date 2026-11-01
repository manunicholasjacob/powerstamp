"""powerstamp: energy measurement that reports its own uncertainty.

The first class output is not the joule figure. It is the joule figure with the
achieved sampling rate, the rails and domains actually covered, and the
uncertainty attached, and a refusal to emit a confident number when the
measurement cannot support one.

Minimal use::

    from powerstamp import Powerstamp

    with Powerstamp(sample_hz=20, label="decode") as ps:
        decode(frames)

    print(ps.result.summary())
    print(ps.result.to_json())

Author: Manu Nicholas Jacob, Independent Researcher.
ORCID 0009-0007-6589-6572. MIT licensed.
"""

from __future__ import annotations

__version__ = "0.1.0"
__author__ = "Manu Nicholas Jacob"
__license__ = "MIT"

from .backends import (
    AndroidBackend,
    Availability,
    Backend,
    ModelEstimateBackend,
    NvmlBackend,
    RaplBackend,
    RpiPmicBackend,
    UnsupportedBackend,
    available_backend_names,
    detect_backends,
    select_backend,
)
from .measure import (
    DEFAULT_SAMPLE_HZ,
    EnergyResult,
    Powerstamp,
    measure,
    powerstamp,
)
from .schema import (
    KIND_ESTIMATE,
    KIND_MEASUREMENT,
    KIND_NONE,
    REASON_CODES,
    SCHEMA_ID,
    SEVERITIES,
    VERDICT_INVALID,
    VERDICT_PASS,
    VERDICT_UNKNOWN,
    VERDICT_UNSUPPORTED,
    VERDICTS,
    Envelope,
    Reason,
)
from .uncertainty import (
    COVERAGE_FACTOR,
    MARGINAL_SAMPLES,
    MIN_SAMPLES,
    RATE_SHORTFALL_FRACTION,
    RateReport,
    UncertaintyBudget,
    energy_uncertainty,
    min_duration_s,
    rate_report,
)

__all__ = [
    "__version__",
    "__author__",
    "__license__",
    # engine
    "Powerstamp",
    "powerstamp",
    "measure",
    "EnergyResult",
    "DEFAULT_SAMPLE_HZ",
    # backends
    "Backend",
    "Availability",
    "RaplBackend",
    "NvmlBackend",
    "RpiPmicBackend",
    "AndroidBackend",
    "UnsupportedBackend",
    "ModelEstimateBackend",
    "detect_backends",
    "select_backend",
    "available_backend_names",
    # schema
    "SCHEMA_ID",
    "VERDICTS",
    "VERDICT_PASS",
    "VERDICT_INVALID",
    "VERDICT_UNSUPPORTED",
    "VERDICT_UNKNOWN",
    "SEVERITIES",
    "KIND_MEASUREMENT",
    "KIND_ESTIMATE",
    "KIND_NONE",
    "REASON_CODES",
    "Reason",
    "Envelope",
    # uncertainty
    "MIN_SAMPLES",
    "MARGINAL_SAMPLES",
    "RATE_SHORTFALL_FRACTION",
    "COVERAGE_FACTOR",
    "RateReport",
    "UncertaintyBudget",
    "rate_report",
    "energy_uncertainty",
    "min_duration_s",
]
