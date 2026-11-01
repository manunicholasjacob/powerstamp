"""Raspberry Pi 5 PMIC rails via ``vcgencmd pmic_read_adc``.

Why this backend reports two different rates
--------------------------------------------
``pmic_read_adc`` with no argument prints every channel of the PMIC's ADCs in
one go. On a Raspberry Pi 5 that is 26 lines: 12 current channels, 14 voltage
channels. Twelve rails have both a current and a voltage channel and therefore
yield a power term. Two channels, ``EXT5V_V`` and ``BATT_V``, are voltage only
and contribute no power term at all.

Counting individual channel reads as samples therefore reports roughly twice
the real rate. That is not hypothetical. The author's own Raspberry Pi 5 energy
work found the PMIC's effective sampling rate to be 21.2 Hz where a naive count
suggested 43 to 44 Hz, because the apparent figure was twice a per rail read
count; the paper's conclusions survived with better than a six times margin,
but the rate in an earlier draft was wrong and had to be corrected. A reported
rate that is an artefact of how the reads were counted is exactly the class of
error this tool exists to prevent, so this backend reports the complete sweep
rate and the raw channel read rate as two separate, separately named fields,
and the sweep rate is the one the refusal rule uses.

Scope caveat, from the Raspberry Pi documentation
-------------------------------------------------
The PMIC rail readings do not include USB current or anything else connected
directly to the 5V supply. So this is a board rail figure, not a wall figure,
and it carries a reason code saying so.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Callable, Dict, List, Optional, Sequence

from .base import (
    REDUCTION_SAMPLER,
    Availability,
    Backend,
    Reduction,
    Sample,
    integrate_power,
)

VCGENCMD = "vcgencmd"
PMIC_ARG = "pmic_read_adc"

#: ``NAME_A current(0)=0.04782057A`` or ``NAME_V volt(8)=3.70534400V``
_LINE = re.compile(
    r"^\s*(?P<channel>\S+?)_(?P<kind>[AV])\s+(?:current|volt)\((?P<idx>\d+)\)="
    r"(?P<value>[-+0-9.eE]+)(?P<unit>[AV])\s*$"
)


class PmicReading:
    """One parsed sweep of the PMIC ADC channels."""

    def __init__(self) -> None:
        self.amps: Dict[str, float] = {}
        self.volts: Dict[str, float] = {}
        self.channel_index: Dict[str, int] = {}
        self.line_count = 0

    @property
    def channel_reads(self) -> int:
        return len(self.amps) + len(self.volts)

    @property
    def power_rails(self) -> Dict[str, float]:
        """Watts per rail, for the rails that have both a current and a voltage."""
        out = {}
        for rail in sorted(set(self.amps) & set(self.volts)):
            out[rail] = self.volts[rail] * self.amps[rail]
        return out

    @property
    def voltage_only(self) -> List[str]:
        return sorted(set(self.volts) - set(self.amps))

    @property
    def current_only(self) -> List[str]:
        return sorted(set(self.amps) - set(self.volts))


def parse_pmic_read_adc(text: str) -> PmicReading:
    """Parse the full output of ``vcgencmd pmic_read_adc``.

    Unrecognised lines are ignored rather than guessed at, and the count of
    lines seen is kept so that a truncated sweep is visible.
    """
    reading = PmicReading()
    for raw in text.splitlines():
        if not raw.strip():
            continue
        reading.line_count += 1
        m = _LINE.match(raw)
        if m is None:
            continue
        channel = m.group("channel")
        kind = m.group("kind")
        try:
            value = float(m.group("value"))
        except ValueError:
            continue
        idx = int(m.group("idx"))
        key = "%s_%s" % (channel, kind)
        reading.channel_index[key] = idx
        if kind == "A":
            reading.amps[channel] = value
        else:
            reading.volts[channel] = value
    return reading


def _default_runner() -> str:
    proc = subprocess.run(
        [VCGENCMD, PMIC_ARG],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
    )
    return proc.stdout.decode("ascii", "replace")


class RpiPmicBackend(Backend):
    name = "rpi_pmic"
    reduction_kind = REDUCTION_SAMPLER
    coverage = (
        "Raspberry Pi 5 PMIC supply rails that expose both a current and a "
        "voltage channel. Excludes USB current and anything else drawing "
        "directly on the 5V supply, and excludes power supply conversion loss."
    )

    def __init__(self, runner: Optional[Callable[[], str]] = None) -> None:
        self._runner = runner or _default_runner
        self._injected = runner is not None
        self._first: Optional[PmicReading] = None

    def detect(self) -> Availability:
        if not self._injected and shutil.which(VCGENCMD) is None:
            return Availability(available=False, detail="vcgencmd is not on PATH")
        try:
            text = self._runner()
        except Exception as exc:
            return Availability(
                available=False, detail="%s %s failed: %s" % (VCGENCMD, PMIC_ARG, exc)
            )
        reading = parse_pmic_read_adc(text)
        if not reading.power_rails:
            return Availability(
                available=False,
                detail=(
                    "%s %s produced no rail with both a current and a voltage channel; "
                    "pmic_read_adc is documented on Raspberry Pi 5 only, and an "
                    "unrecognised channel name makes the command list every channel "
                    "instead of returning an error" % (VCGENCMD, PMIC_ARG)
                ),
            )
        self._first = reading
        return Availability(
            available=True,
            detail="%d rail(s) with power, %d channel read(s) per sweep"
            % (len(reading.power_rails), reading.channel_reads),
            detected_hardware="Raspberry Pi PMIC, %d channels" % reading.channel_reads,
        )

    def open(self) -> None:
        if self._first is None:
            self.detect()

    def read(self) -> Sample:
        reading = parse_pmic_read_adc(self._runner())
        return Sample(
            power_w=reading.power_rails,
            reads=reading.channel_reads,
        )

    def reduce(self, samples: Sequence[Sample]) -> Reduction:
        joules, interval_power, per_rail = integrate_power(samples)

        reads_per_sample = [s.reads for s in samples if s.reads]
        mean_reads = (
            sum(reads_per_sample) / float(len(reads_per_sample))
            if reads_per_sample
            else 0.0
        )

        first = self._first or PmicReading()
        voltage_only = first.voltage_only
        current_only = first.current_only

        reasons: List[tuple] = [
            (
                "ENERGY_FROM_POWER_INTEGRAL",
                "info",
                "Energy is the trapezoidal integral of per rail voltage times current "
                "from pmic_read_adc. The PMIC exposes no energy counter.",
            ),
            (
                "PMIC_EXCLUDES_EXTERNAL_5V",
                "warning",
                "Raspberry Pi documents that these readings exclude USB current and "
                "anything else connected directly to the 5V supply, so this is a board "
                "rail figure and not a wall figure.",
            ),
        ]
        if voltage_only:
            reasons.append(
                (
                    "PMIC_VOLTAGE_ONLY_CHANNELS",
                    "info",
                    "Channel(s) %s report voltage with no matching current channel and "
                    "contribute no power term. They are counted in the channel read rate "
                    "and excluded from the rail power sum."
                    % ", ".join(voltage_only),
                )
            )
        if current_only:
            reasons.append(
                (
                    "PARTIAL_DOMAIN_COVERAGE",
                    "warning",
                    "Channel(s) %s report current with no matching voltage channel, so "
                    "their power cannot be computed." % ", ".join(current_only),
                )
            )

        unreadable = ["pmic:%s (voltage only, no current channel)" % c for c in voltage_only]
        unreadable += ["pmic:%s (current only, no voltage channel)" % c for c in current_only]

        return Reduction(
            joules=joules,
            interval_power_w=interval_power,
            per_domain_j=per_rail,
            # The PMIC ADC resolution is not published by Raspberry Pi, and the
            # eight printed decimal places are print precision, not resolution.
            # Claiming a quantization term from the print format would be an
            # invented number, so the term is zero and the omission is stated.
            quantization_j=0.0,
            extra={
                "rails_with_power": sorted(per_rail.keys()),
                "rail_count": len(per_rail),
                "voltage_only_channels": voltage_only,
                "current_only_channels": current_only,
                "channel_reads_per_sample": mean_reads,
                "channels_per_powered_rail": 2,
                "coverage_note": self.coverage,
                "quantization_note": (
                    "The PMIC ADC resolution is not published, so the quantization term "
                    "is reported as zero rather than inferred from the printed decimal "
                    "places. The uncertainty budget is therefore missing this term."
                ),
                "rate_note": (
                    "achieved_hz counts complete sweeps and is the rate the refusal "
                    "rule uses. channel_read_hz counts individual ADC channel reads and "
                    "is larger by channel_reads_per_sample. A rate derived from a read "
                    "count is an artefact, not a rate. Each powered rail contributes two "
                    "channel reads, a voltage and a current, so counting per rail reads "
                    "instead of sweeps reports twice the real rate: that is the specific "
                    "error that put 43 to 44 Hz in an earlier draft of the author's "
                    "Raspberry Pi 5 energy work where the effective rate was 21.2 Hz."
                ),
            },
            reasons=reasons,
            unreadable=unreadable,
        )
