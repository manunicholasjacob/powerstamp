"""Android via the fuel gauge, with batterystats as the coarse fallback.

The loud caveat, carried in the data and not only in the docs
-------------------------------------------------------------
A battery side reading is the whole device. It includes the display, the modem,
the radios and every background process, and it is not the energy of the
measured region unless those are separately controlled for. The backend always
emits ``BATTERY_SIDE_READING`` at warning severity. There is no configuration
that removes it, because there is no reading this backend can take that makes
it untrue.

What it reads
-------------
``/sys/class/power_supply/battery/charge_counter`` (microamp hours, falling as
the battery discharges) and ``voltage_now`` (microvolts). Energy over an
interval is the charge delta times the mean voltage. Vendors place these files
under ``battery`` or ``bms``, so the path is discovered rather than assumed.

``dumpsys batterystats`` is read once, if available, for the device's own
accounting, but it is recorded as context in the output and is not the source
of the joule figure: its reporting granularity is coarse and vendor dependent,
and mixing it into the integral would hide which number came from where.

Fuel gauge coarseness is the usual failure here. The counter updates on the
gauge's own schedule, not on request, so a short run can produce many samples
and only one or two distinct counter values. The backend counts distinct
charge counter values and refuses rather than reporting a figure built from a
single step.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable, List, Optional, Sequence

from .base import REDUCTION_COUNTER, Availability, Backend, Reduction, Sample

POWER_SUPPLY_ROOT = "/sys/class/power_supply"
CANDIDATE_NODES = ("battery", "bms", "Battery", "max170xx_battery")

#: Minimum number of distinct fuel gauge charge counter values for a figure.
#: Two values give exactly one step and therefore no scatter at all; three is
#: the smallest count from which a step size can be seen to be consistent.
MIN_DISTINCT_CHARGE_VALUES = 3


def _read_int(path: str) -> Optional[int]:
    try:
        with open(path, "r") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def find_battery_node(root: str = POWER_SUPPLY_ROOT) -> Optional[str]:
    """Locate a power supply node exposing both charge_counter and voltage_now."""
    if not os.path.isdir(root):
        return None
    names = list(CANDIDATE_NODES)
    try:
        names += sorted(n for n in os.listdir(root) if n not in CANDIDATE_NODES)
    except OSError:
        pass
    for name in names:
        node = os.path.join(root, name)
        if os.path.exists(os.path.join(node, "charge_counter")) and os.path.exists(
            os.path.join(node, "voltage_now")
        ):
            return node
    return None


def _default_batterystats() -> str:
    proc = subprocess.run(
        ["dumpsys", "batterystats", "--charged"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    return proc.stdout.decode("ascii", "replace")


class AndroidBackend(Backend):
    name = "android_fuel_gauge"
    reduction_kind = REDUCTION_COUNTER
    coverage = (
        "The whole device as seen from the battery terminals: SoC, display, "
        "modem, radios and every background process. Not the measured region "
        "unless those are separately controlled for."
    )

    def __init__(
        self,
        power_supply_root: str = POWER_SUPPLY_ROOT,
        batterystats_runner: Optional[Callable[[], str]] = None,
    ) -> None:
        self.root = power_supply_root
        self.node: Optional[str] = None
        self._batterystats_runner = batterystats_runner
        self._batterystats_available = False
        self._batterystats_head: List[str] = []

    def detect(self) -> Availability:
        node = find_battery_node(self.root)
        if node is None:
            return Availability(
                available=False,
                detail="no power supply node under %s exposes both charge_counter and "
                "voltage_now" % self.root,
            )
        if _read_int(os.path.join(node, "charge_counter")) is None:
            return Availability(
                available=False,
                detail="charge_counter at %s is present but not readable" % node,
            )
        if _read_int(os.path.join(node, "voltage_now")) is None:
            return Availability(
                available=False,
                detail="voltage_now at %s is present but not readable" % node,
            )
        self.node = node

        runner = self._batterystats_runner
        if runner is None and shutil.which("dumpsys") is not None:
            runner = _default_batterystats
        if runner is not None:
            try:
                text = runner()
                self._batterystats_available = bool(text.strip())
                self._batterystats_head = text.splitlines()[:5]
            except Exception:
                self._batterystats_available = False

        return Availability(
            available=True,
            detail="fuel gauge at %s%s"
            % (node, ", batterystats available" if self._batterystats_available else ""),
            detected_hardware=os.path.basename(node),
        )

    def open(self) -> None:
        if self.node is None:
            self.detect()

    def read(self) -> Sample:
        assert self.node is not None
        charge = _read_int(os.path.join(self.node, "charge_counter"))
        volt = _read_int(os.path.join(self.node, "voltage_now"))
        sample = Sample(reads=2)
        if charge is not None:
            sample.counters["charge_counter_uah"] = charge
        if volt is not None:
            sample.counters["voltage_now_uv"] = volt
        return sample

    def reduce(self, samples: Sequence[Sample]) -> Reduction:
        reasons: List[tuple] = [
            (
                "BATTERY_SIDE_READING",
                "warning",
                "This is a battery side reading of the whole device. It includes the "
                "display, the modem and every background process, and it is not the "
                "energy of the measured region unless those are controlled for.",
            ),
            (
                "ENERGY_FROM_HARDWARE_COUNTER",
                "info",
                "Energy is the fuel gauge charge counter delta times the mean terminal "
                "voltage over each interval.",
            ),
        ]

        charges = [
            s.counters["charge_counter_uah"]
            for s in samples
            if "charge_counter_uah" in s.counters
        ]
        distinct = sorted(set(charges))
        joules = 0.0
        interval_power: List[float] = []

        for i in range(len(samples) - 1):
            a, b = samples[i].counters, samples[i + 1].counters
            dt = samples[i + 1].t_monotonic - samples[i].t_monotonic
            if dt <= 0:
                continue
            if "charge_counter_uah" not in a or "charge_counter_uah" not in b:
                continue
            # Discharging makes the counter fall, so the energy drawn is the
            # magnitude of the fall. A rise means the device was charging, and
            # the interval contributes nothing rather than a negative figure.
            d_uah = a["charge_counter_uah"] - b["charge_counter_uah"]
            if d_uah <= 0:
                interval_power.append(0.0)
                continue
            va = a.get("voltage_now_uv")
            vb = b.get("voltage_now_uv")
            if va is None or vb is None:
                continue
            mean_v = 0.5 * (va + vb) / 1e6
            # 1 uAh = 3.6 millicoulombs.
            coulombs = d_uah * 3.6e-3
            e = coulombs * mean_v
            joules += e
            interval_power.append(e / dt)

        charging = any(p == 0.0 for p in interval_power) and joules == 0.0
        if charging:
            reasons.append(
                (
                    "NO_READABLE_DOMAINS",
                    "invalidating",
                    "The fuel gauge charge counter never fell during the run, which "
                    "means the device was charging or the counter did not update; no "
                    "discharge energy can be attributed.",
                )
            )
        elif len(distinct) < MIN_DISTINCT_CHARGE_VALUES:
            reasons.append(
                (
                    "SAMPLE_COUNT_TOO_LOW",
                    "invalidating",
                    "The fuel gauge charge counter took only %d distinct value(s) during "
                    "the run, so the figure rests on fewer than %d gauge updates "
                    "regardless of how often it was polled; lengthen the run."
                    % (len(distinct), MIN_DISTINCT_CHARGE_VALUES),
                )
            )

        step_uah = None
        if len(distinct) >= 2:
            steps = [distinct[i + 1] - distinct[i] for i in range(len(distinct) - 1)]
            step_uah = min(steps)

        nominal_v = 3.85
        quant_j = (step_uah or 1) * 3.6e-3 * nominal_v

        return Reduction(
            joules=joules,
            interval_power_w=interval_power,
            per_domain_j={"battery_terminal": joules},
            quantization_j=quant_j,
            extra={
                "battery_node": self.node,
                "distinct_charge_counter_values": len(distinct),
                "observed_charge_step_uah": step_uah,
                "min_distinct_charge_values_required": MIN_DISTINCT_CHARGE_VALUES,
                "batterystats_available": self._batterystats_available,
                "batterystats_role": (
                    "read as context only; it is not the source of the joule figure"
                ),
                "coverage_note": self.coverage,
                "quantization_note": (
                    "The quantization term uses the smallest observed charge counter step "
                    "at a nominal %.2f V cell voltage, because the gauge's own resolution "
                    "is not published." % nominal_v
                ),
            },
            reasons=reasons,
            unreadable=[],
        )
