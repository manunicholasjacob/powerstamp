"""Stable result envelope for powerstamp.

The outer envelope is shared with the sibling tools described in the README so
that a consumer can read the provenance fields of any of them without knowing
which tool produced the record. Everything specific to energy measurement lives
under the ``measurement`` key.

Nothing in this module performs a measurement. It only defines the vocabulary
and assembles the record.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

SCHEMA_ID = "measurement-provenance/1"
TOOL_NAME = "powerstamp"

# ---------------------------------------------------------------------------
# Verdict vocabulary. Owned by the shared envelope, not by powerstamp.
# ---------------------------------------------------------------------------

VERDICT_PASS = "PASS"
VERDICT_INVALID = "INVALID"
VERDICT_UNSUPPORTED = "UNSUPPORTED"
VERDICT_UNKNOWN = "UNKNOWN"

VERDICTS = (VERDICT_PASS, VERDICT_INVALID, VERDICT_UNSUPPORTED, VERDICT_UNKNOWN)

# ---------------------------------------------------------------------------
# Severity vocabulary.
# ---------------------------------------------------------------------------

SEVERITY_INVALIDATING = "invalidating"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

SEVERITIES = (SEVERITY_INVALIDATING, SEVERITY_WARNING, SEVERITY_INFO)

# ---------------------------------------------------------------------------
# Measurement kind. This is the measurement-versus-estimate flag, carried as a
# field in the data structure and not only in prose.
# ---------------------------------------------------------------------------

KIND_MEASUREMENT = "MEASUREMENT"
KIND_ESTIMATE = "ESTIMATE"
KIND_NONE = "NONE"

KINDS = (KIND_MEASUREMENT, KIND_ESTIMATE, KIND_NONE)

# ---------------------------------------------------------------------------
# Reason codes emitted by powerstamp. Every code used anywhere in the package
# must appear here, and the README documents the same list.
# ---------------------------------------------------------------------------

REASON_CODES: Dict[str, str] = {
    # Sample sufficiency.
    "NO_SAMPLES": "The sampling loop produced fewer than two samples, so no interval exists.",
    "SAMPLE_COUNT_TOO_LOW": "Fewer complete samples than the documented minimum for a confident figure.",
    "SAMPLE_COUNT_MARGINAL": "Enough samples to report, but few enough that the error bar is itself loose.",
    "RATE_SHORTFALL": "The achieved sampling rate fell well short of the requested rate.",
    "SAMPLER_JITTER_HIGH": "Sample intervals varied enough that the timing term dominates the uncertainty.",
    # Counter handling.
    "COUNTER_WRAPAROUND": "A cumulative energy counter rolled over and the rollover was corrected.",
    "COUNTER_WRAPAROUND_AMBIGUOUS": (
        "A single sampling interval could have contained more than one counter rollover, "
        "so the corrected total is not recoverable."
    ),
    "ENERGY_FROM_POWER_INTEGRAL": (
        "Energy is the integral of sampled power, not a reading from a hardware energy counter."
    ),
    "ENERGY_FROM_HARDWARE_COUNTER": "Energy is the delta of a hardware energy counter.",
    # Coverage.
    "PARTIAL_DOMAIN_COVERAGE": "One or more requested domains or rails could not be read.",
    "PACKAGE_ONLY_COVERAGE": (
        "Only processor package domains were readable, so this is not a system level figure."
    ),
    "PERMISSION_DENIED": "A counter file existed but could not be read with the current privileges.",
    "NO_READABLE_DOMAINS": "The backend was detected but no domain or rail could be read.",
    "PMIC_VOLTAGE_ONLY_CHANNELS": (
        "Some PMIC channels report voltage with no matching current channel and contribute no power term."
    ),
    "PMIC_EXCLUDES_EXTERNAL_5V": (
        "PMIC rail readings exclude USB current and anything else drawing directly on the 5V supply."
    ),
    "BATTERY_SIDE_READING": (
        "A battery side reading includes the display, the modem and every other system load "
        "unless they are separately controlled for."
    ),
    "GPU_BOARD_SCOPE_UNSTATED": (
        "The vendor API does not state which internal rails the device figure covers."
    ),
    # Baseline.
    "BASELINE_SUBTRACTED": "An idle baseline power was subtracted from the measured power.",
    "BASELINE_NOT_SUBTRACTED": "No idle baseline was subtracted, so the figure includes idle draw.",
    "BASELINE_EXCEEDS_MEASUREMENT": (
        "The idle baseline was larger than the measured power, so the net figure would be negative."
    ),
    # Support.
    "NO_BACKEND": "No energy measurement backend is available on this platform.",
    "BACKEND_UNAVAILABLE": "A backend was requested by name but is not available here.",
    "ESTIMATE_NOT_MEASUREMENT": (
        "The figure is a model based estimate, not a measurement, and never earns a PASS verdict."
    ),
}


@dataclass(frozen=True)
class Reason:
    """One entry in the envelope's ``reasons`` list."""

    code: str
    severity: str
    detail: str

    def __post_init__(self) -> None:
        if self.code not in REASON_CODES:
            raise ValueError("unknown reason code: %s" % self.code)
        if self.severity not in SEVERITIES:
            raise ValueError("unknown severity: %s" % self.severity)

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "severity": self.severity, "detail": self.detail}


def reason(code: str, severity: str, detail: Optional[str] = None) -> Reason:
    """Build a Reason, defaulting the detail to the registered description."""
    if detail is None:
        detail = REASON_CODES[code]
    return Reason(code=code, severity=severity, detail=detail)


def platform_block() -> Dict[str, str]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": "%d.%d.%d" % sys.version_info[:3],
    }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def decide_verdict(reasons: List[Reason], kind: str, supported: bool) -> str:
    """Map a reason list onto the shared verdict vocabulary.

    The rule, in order:

    1. No backend at all gives UNSUPPORTED.
    2. Any invalidating reason gives INVALID.
    3. An ESTIMATE never earns PASS; the best it reaches is UNKNOWN.
    4. Otherwise PASS.
    """
    if not supported:
        return VERDICT_UNSUPPORTED
    if any(r.severity == SEVERITY_INVALIDATING for r in reasons):
        return VERDICT_INVALID
    if kind == KIND_ESTIMATE:
        return VERDICT_UNKNOWN
    if kind == KIND_NONE:
        return VERDICT_UNKNOWN
    return VERDICT_PASS


@dataclass
class Envelope:
    """The shared outer record. ``measurement`` holds the powerstamp payload."""

    tool_version: str
    duration_s: float
    verdict: str
    reasons: List[Reason] = field(default_factory=list)
    unreadable: List[str] = field(default_factory=list)
    measurement: Dict[str, Any] = field(default_factory=dict)
    taken_at_utc: Optional[str] = None
    tool_name: str = TOOL_NAME

    def to_dict(self) -> Dict[str, Any]:
        if self.verdict not in VERDICTS:
            raise ValueError("unknown verdict: %s" % self.verdict)
        return {
            "schema": SCHEMA_ID,
            "tool": {"name": self.tool_name, "version": self.tool_version},
            "taken_at_utc": self.taken_at_utc or utc_now_iso(),
            "duration_s": float(self.duration_s),
            "platform": platform_block(),
            "verdict": self.verdict,
            "reasons": [r.to_dict() for r in self.reasons],
            "unreadable": list(self.unreadable),
            "measurement": dict(self.measurement),
        }


ENVELOPE_TOP_LEVEL_KEYS = (
    "schema",
    "tool",
    "taken_at_utc",
    "duration_s",
    "platform",
    "verdict",
    "reasons",
    "unreadable",
    "measurement",
)
