"""Synthetic fixtures.

The suite runs on a machine with no RAPL, no NVIDIA GPU, no Raspberry Pi PMIC
and no Android fuel gauge. Every hardware path is exercised through an injected
dependency: a fake powercap tree plus a scripted counter reader, a fake NVML
module, a recorded ``vcgencmd pmic_read_adc`` capture, and a fake power supply
sysfs tree.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Sequence

import pytest

from powerstamp.backends.base import Availability, Backend, Reduction, Sample, integrate_power

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

#: A real kernel names zones ``intel-rapl:0`` and ``intel-rapl:0:1``. A colon
#: is not a legal character in a directory name on Windows, where it opens an
#: NTFS alternate data stream instead, so the fixture tree uses a dash there.
#: Discovery matches on the ``intel-rapl`` prefix and on the presence of the
#: attribute files, so either spelling exercises the same code path.
ZONE_SEP = ":" if os.sep == "/" else "-"


def zone_dir(*ids: int) -> str:
    """Zone directory name for the fake tree, for example ``intel-rapl:0:1``."""
    return "intel-rapl" + "".join("%s%d" % (ZONE_SEP, i) for i in ids)


# ---------------------------------------------------------------------------
# Fake powercap tree
# ---------------------------------------------------------------------------


def write_powercap_tree(root: str, zones: Sequence[dict]) -> None:
    """Create a fake ``/sys/class/powercap`` tree.

    Each zone dict takes ``dir`` (the zone directory name, for example
    ``intel-rapl:0``), ``name`` (the domain name written to the ``name`` file),
    ``energy_uj`` (the initial counter value) and ``max_energy_range_uj``.
    """
    os.makedirs(root, exist_ok=True)
    for zone in zones:
        path = os.path.join(root, zone["dir"])
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "name"), "w") as fh:
            fh.write(str(zone["name"]) + "\n")
        with open(os.path.join(path, "energy_uj"), "w") as fh:
            fh.write(str(zone.get("energy_uj", 0)) + "\n")
        if zone.get("max_energy_range_uj") is not None:
            with open(os.path.join(path, "max_energy_range_uj"), "w") as fh:
                fh.write(str(zone["max_energy_range_uj"]) + "\n")


class ScriptedReader:
    """A reader that serves a pre-set sequence of counter values per path.

    ``scripts`` maps an ``energy_uj`` path to the list of values it returns on
    successive reads; the final value repeats once the list runs out. ``denied``
    is a set of paths for which the reader raises PermissionError, which is how
    the suite exercises the root-only ``energy_uj`` path portably: a chmod based
    fixture would behave differently on Windows and on Linux.
    """

    def __init__(
        self,
        scripts: Optional[Dict[str, Sequence[int]]] = None,
        denied: Optional[Sequence[str]] = None,
    ) -> None:
        self.scripts = {k: list(v) for k, v in (scripts or {}).items()}
        self.denied = set(denied or ())
        self.calls: Dict[str, int] = {}

    def __call__(self, path: str) -> str:
        norm = os.path.normpath(path)
        if norm in self.denied:
            raise PermissionError(13, "Permission denied", path)
        if norm in self.scripts:
            idx = self.calls.get(norm, 0)
            values = self.scripts[norm]
            value = values[min(idx, len(values) - 1)]
            self.calls[norm] = idx + 1
            return "%d\n" % value
        with open(path, "r") as fh:
            return fh.read()


@pytest.fixture
def powercap_full(tmp_path):
    """A tree with package, core, dram and psys, all readable."""
    root = str(tmp_path / "powercap")
    write_powercap_tree(
        root,
        [
            {
                "dir": zone_dir(0),
                "name": "package-0",
                "energy_uj": 1_000_000,
                "max_energy_range_uj": 262_143_328_850,
            },
            {
                "dir": zone_dir(0, 0),
                "name": "core",
                "energy_uj": 500_000,
                "max_energy_range_uj": 262_143_328_850,
            },
            {
                "dir": zone_dir(0, 1),
                "name": "dram",
                "energy_uj": 200_000,
                "max_energy_range_uj": 262_143_328_850,
            },
            {
                "dir": zone_dir(1),
                "name": "psys",
                "energy_uj": 3_000_000,
                "max_energy_range_uj": 262_143_328_850,
            },
        ],
    )
    return root


@pytest.fixture
def powercap_package_only(tmp_path):
    """A tree where only the package domain is readable."""
    root = str(tmp_path / "powercap-partial")
    write_powercap_tree(
        root,
        [
            {
                "dir": zone_dir(0),
                "name": "package-0",
                "energy_uj": 1_000_000,
                "max_energy_range_uj": 262_143_328_850,
            },
            {
                "dir": zone_dir(0, 1),
                "name": "dram",
                "energy_uj": 200_000,
                "max_energy_range_uj": 262_143_328_850,
            },
            {
                "dir": zone_dir(1),
                "name": "psys",
                "energy_uj": 3_000_000,
                "max_energy_range_uj": 262_143_328_850,
            },
        ],
    )
    return root


@pytest.fixture
def powercap_empty(tmp_path):
    """A powercap directory with no RAPL zones at all."""
    root = str(tmp_path / "powercap-empty")
    os.makedirs(root, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# Fake NVML
# ---------------------------------------------------------------------------


class FakeNvmlError(Exception):
    pass


class FakeNvml:
    """A minimal stand in for pynvml.

    ``energy_mj`` is a list of successive values for the cumulative energy
    counter. ``power_mw`` is the same for instantaneous power. Setting
    ``support_energy`` to False makes the energy call raise, which is how the
    power sampling fallback is exercised.
    """

    def __init__(
        self,
        device_count: int = 1,
        energy_mj: Optional[Sequence[int]] = None,
        power_mw: Optional[Sequence[int]] = None,
        support_energy: bool = True,
        name: str = "FakeGPU 9000",
    ) -> None:
        self.device_count = device_count
        self.energy_mj = list(energy_mj or [0])
        self.power_mw = list(power_mw or [100_000])
        self.support_energy = support_energy
        self.gpu_name = name
        self.init_calls = 0
        self.shutdown_calls = 0
        self._energy_idx = 0
        self._power_idx = 0

    def nvmlInit(self) -> None:
        self.init_calls += 1

    def nvmlShutdown(self) -> None:
        self.shutdown_calls += 1

    def nvmlDeviceGetCount(self) -> int:
        return self.device_count

    def nvmlDeviceGetHandleByIndex(self, index: int):
        if index >= self.device_count:
            raise FakeNvmlError("NVML_ERROR_INVALID_ARGUMENT")
        return "handle-%d" % index

    def nvmlDeviceGetName(self, handle) -> str:
        return self.gpu_name

    def nvmlDeviceGetTotalEnergyConsumption(self, handle) -> int:
        if not self.support_energy:
            raise FakeNvmlError("NVML_ERROR_NOT_SUPPORTED")
        value = self.energy_mj[min(self._energy_idx, len(self.energy_mj) - 1)]
        self._energy_idx += 1
        return value

    def nvmlDeviceGetPowerUsage(self, handle) -> int:
        value = self.power_mw[min(self._power_idx, len(self.power_mw) - 1)]
        self._power_idx += 1
        return value


# ---------------------------------------------------------------------------
# Recorded vcgencmd output
# ---------------------------------------------------------------------------


@pytest.fixture
def pmic_capture() -> str:
    with open(os.path.join(FIXTURE_DIR, "pmic_read_adc_pi5.txt"), "r") as fh:
        return fh.read()


class PmicRunner:
    """Returns a recorded ``pmic_read_adc`` capture, optionally scaled.

    ``scale`` multiplies every current reading on each successive call, so a
    sequence of calls produces a changing power figure without needing real
    hardware.
    """

    def __init__(self, text: str, scales: Optional[Sequence[float]] = None) -> None:
        self.text = text
        self.scales = list(scales or [1.0])
        self.calls = 0

    def __call__(self) -> str:
        scale = self.scales[min(self.calls, len(self.scales) - 1)]
        self.calls += 1
        if scale == 1.0:
            return self.text
        out = []
        for line in self.text.splitlines():
            if line.rstrip().endswith("A") and "current(" in line:
                head, _, tail = line.rpartition("=")
                value = float(tail[:-1]) * scale
                out.append("%s=%.8fA" % (head, value))
            else:
                out.append(line)
        return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Fake power supply sysfs
# ---------------------------------------------------------------------------


def write_power_supply_tree(
    root: str, node: str = "battery", charge_uah: int = 3_000_000, voltage_uv: int = 3_900_000
) -> str:
    path = os.path.join(root, node)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "charge_counter"), "w") as fh:
        fh.write("%d\n" % charge_uah)
    with open(os.path.join(path, "voltage_now"), "w") as fh:
        fh.write("%d\n" % voltage_uv)
    return path


@pytest.fixture
def power_supply_root(tmp_path):
    root = str(tmp_path / "power_supply")
    write_power_supply_tree(root)
    return root


# ---------------------------------------------------------------------------
# Deterministic engine backends
# ---------------------------------------------------------------------------


class FakeConstantPowerBackend(Backend):
    """A sampler backend that always reports the same power. Engine fixture."""

    name = "fake_constant"
    reduction_kind = "sampler"
    coverage = "nothing; a synthetic fixture for the engine tests"

    def __init__(self, watts: float = 10.0, rails: int = 2, read_delay_s: float = 0.0) -> None:
        self.watts = watts
        self.rails = rails
        self.read_delay_s = read_delay_s
        self.reads = 0

    def detect(self) -> Availability:
        return Availability(available=True, detail="synthetic", detected_hardware="none")

    def read(self) -> Sample:
        if self.read_delay_s:
            import time

            time.sleep(self.read_delay_s)
        self.reads += 1
        per_rail = self.watts / float(self.rails)
        return Sample(
            power_w={"rail%d" % i: per_rail for i in range(self.rails)},
            reads=self.rails,
        )

    def reduce(self, samples: Sequence[Sample]) -> Reduction:
        joules, interval_power, per_rail = integrate_power(samples)
        return Reduction(
            joules=joules,
            interval_power_w=interval_power,
            per_domain_j=per_rail,
            quantization_j=0.0,
            extra={"coverage_note": self.coverage},
            reasons=[
                (
                    "ENERGY_FROM_POWER_INTEGRAL",
                    "info",
                    "Synthetic fixture integrates a constant sampled power.",
                )
            ],
        )


class NeverAvailableBackend(Backend):
    name = "never_available"
    reduction_kind = "sampler"
    coverage = "nothing"

    def __init__(self, detail: str = "synthetic backend is never available") -> None:
        self.detail = detail

    def detect(self) -> Availability:
        return Availability(available=False, detail=self.detail)

    def read(self) -> Sample:
        raise AssertionError("read must never be called on an unavailable backend")

    def reduce(self, samples):
        raise AssertionError("reduce must never be called on an unavailable backend")


@pytest.fixture
def constant_power_backend():
    return FakeConstantPowerBackend(watts=10.0, rails=2)
