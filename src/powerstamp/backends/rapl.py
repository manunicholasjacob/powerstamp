"""Intel and AMD RAPL via the Linux powercap sysfs interface.

What this reads
---------------
``/sys/class/powercap/intel-rapl:*/`` and its subzones. Each zone directory
holds a ``name`` file naming the domain, an ``energy_uj`` file holding a
cumulative microjoule counter, and a ``max_energy_range_uj`` file holding the
value at which that counter rolls over. The glob also covers
``intel-rapl-mmio:*`` and ``amd-rapl*`` where a kernel exposes them, since the
same powercap driver is the access path on AMD parts.

Two facts the output must carry
-------------------------------
1. ``energy_uj`` is not world readable on a current kernel. Commit
   949dd0104c49 ("powercap: restrict energy meter to root access", Len Brown,
   10 November 2020, present in v5.10-rc4 and backported to stable) changed the
   mode from ``S_IWUSR | S_IRUGO`` to ``S_IWUSR | S_IRUSR`` in response to the
   PLATYPUS software power side channel, tracked as CVE-2020-8694 for the
   Intel and kernel side and CVE-2020-12912 for the AMD counterpart. So an
   unprivileged process typically sees the files and cannot read them. The
   kernel powercap sysfs documentation still describes the attribute as
   readable and has not been updated, so do not cite the documentation for the
   permission; cite the commit.
2. A package only reading is not a system reading. Which domains were readable
   and which were not goes in the output, and the unreadable ones are named in
   the envelope's ``unreadable`` list so a consumer sees an absence rather
   than a zero.
"""

from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional, Sequence

from .base import (
    REDUCTION_COUNTER,
    Availability,
    Backend,
    Reduction,
    Sample,
    counter_deltas,
)

POWERCAP_ROOT = "/sys/class/powercap"

#: Zone directories are named ``intel-rapl:0``, ``intel-rapl:0:1``,
#: ``intel-rapl-mmio:0`` and so on. Discovery matches on the prefix and then
#: checks for the attribute files, rather than globbing a fixed shape, because
#: the subzone nesting differs between kernels and because a colon is not a
#: legal character in a directory name on every platform the test suite runs
#: on. A directory only counts as a zone if it actually has ``name`` and
#: ``energy_uj``, so the prefix match cannot pull in anything else.
ZONE_PREFIXES = ("intel-rapl", "amd-rapl")

#: How deep below the powercap root to look for subzones.
MAX_ZONE_DEPTH = 3

#: Canonical domain families, used only for classification in the output.
DOMAIN_FAMILIES = ("package", "core", "uncore", "dram", "psys")


def classify_domain(name: str) -> str:
    """Map a sysfs domain name onto one of the canonical families.

    ``package-0`` and ``package_0`` become ``package``; ``core`` stays
    ``core``; anything unrecognised is reported as ``other`` rather than
    guessed into a family.
    """
    low = name.strip().lower()
    if low.startswith("package"):
        return "package"
    if low in ("core", "pp0", "cores"):
        return "core"
    if low in ("uncore", "pp1", "gpu", "igpu"):
        return "uncore"
    if low.startswith("dram") or low in ("ram", "memory"):
        return "dram"
    if low.startswith("psys") or low == "platform":
        return "psys"
    return "other"


#: Reader seam. Production code reads the file; the test suite injects a
#: reader so that the permission path and a mid run counter rollover can be
#: exercised on a machine that has no RAPL hardware. The seam is the same
#: pattern the NVML and PMIC backends use for their own dependencies.
Reader = Callable[[str], str]


def default_reader(path: str) -> str:
    with open(path, "r") as fh:
        return fh.read()


class RaplZone:
    """One powercap zone: a domain name, a counter file and a rollover bound."""

    def __init__(
        self,
        path: str,
        name: str,
        max_range_uj: Optional[int],
        reader: Optional[Reader] = None,
    ) -> None:
        self.path = path
        self.name = name
        self.family = classify_domain(name)
        self.max_range_uj = max_range_uj
        self.energy_path = os.path.join(path, "energy_uj")
        self.reader = reader or default_reader
        self.readable = False
        self.unreadable_reason = ""

    #: Domain key as it appears in the output. The sysfs zone id is kept so
    #: that two sockets both named ``package-0`` by their subzones stay apart.
    @property
    def key(self) -> str:
        return "%s@%s" % (self.name, os.path.basename(self.path))

    def probe(self) -> bool:
        try:
            int(self.reader(self.energy_path).strip())
        except PermissionError as exc:
            self.readable = False
            self.unreadable_reason = "permission denied: %s" % (
                exc.strerror or "EACCES on energy_uj"
            )
            return False
        except (OSError, ValueError) as exc:
            self.readable = False
            self.unreadable_reason = "unreadable: %s" % exc
            return False
        self.readable = True
        return True

    def read_uj(self) -> int:
        return int(self.reader(self.energy_path).strip())


def _read_text(path: str, reader: Optional[Reader] = None) -> Optional[str]:
    try:
        return (reader or default_reader)(path).strip()
    except OSError:
        return None


def _zone_candidate_paths(root: str, depth: int = MAX_ZONE_DEPTH) -> List[str]:
    """Directories under ``root`` whose basename looks like a RAPL zone."""
    found: List[str] = []
    frontier = [(root, 0)]
    while frontier:
        current, level = frontier.pop(0)
        if level >= depth:
            continue
        try:
            entries = sorted(os.listdir(current))
        except OSError:
            continue
        for entry in entries:
            path = os.path.join(current, entry)
            if not os.path.isdir(path):
                continue
            if any(entry.startswith(p) for p in ZONE_PREFIXES):
                found.append(path)
            frontier.append((path, level + 1))
    return found


def discover_zones(
    root: str = POWERCAP_ROOT, reader: Optional[Reader] = None
) -> List[RaplZone]:
    """Enumerate powercap zones and their subzones under ``root``."""
    paths = _zone_candidate_paths(root)

    zones: List[RaplZone] = []
    seen = set()
    for path in paths:
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        name = _read_text(os.path.join(path, "name"))
        if name is None:
            continue
        if not os.path.exists(os.path.join(path, "energy_uj")):
            continue
        raw_range = _read_text(os.path.join(path, "max_energy_range_uj"))
        try:
            max_range = int(raw_range) if raw_range is not None else None
        except ValueError:
            max_range = None
        zones.append(
            RaplZone(path=path, name=name, max_range_uj=max_range, reader=reader)
        )
    return zones


class RaplBackend(Backend):
    name = "rapl"
    reduction_kind = REDUCTION_COUNTER
    coverage = (
        "Processor package and, where the kernel exposes them, core, uncore, "
        "dram and psys domains. Never covers a discrete GPU, storage, the "
        "network interface or the power supply conversion loss."
    )

    def __init__(
        self, root: str = POWERCAP_ROOT, reader: Optional[Reader] = None
    ) -> None:
        self.root = root
        self.reader = reader
        self.zones: List[RaplZone] = []
        self._readable: List[RaplZone] = []

    def detect(self) -> Availability:
        if not os.path.isdir(self.root):
            return Availability(
                available=False,
                detail="no powercap sysfs at %s" % self.root,
            )
        zones = discover_zones(self.root, reader=self.reader)
        if not zones:
            return Availability(
                available=False,
                detail="powercap sysfs present but no RAPL zones with an energy_uj attribute",
            )
        readable = [z for z in zones if z.probe()]
        if not readable:
            denied = [z for z in zones if "permission" in z.unreadable_reason]
            if denied:
                return Availability(
                    available=False,
                    detail=(
                        "found %d RAPL zone(s) but energy_uj is not readable; the counter "
                        "has been root only since kernel commit 949dd0104c49 "
                        "(CVE-2020-8694, PLATYPUS), so run as root or grant read on "
                        "the energy_uj files" % len(zones)
                    ),
                    detected_hardware="%d RAPL zone(s)" % len(zones),
                )
            return Availability(
                available=False,
                detail="found %d RAPL zone(s), none readable" % len(zones),
                detected_hardware="%d RAPL zone(s)" % len(zones),
            )
        self.zones = zones
        self._readable = readable
        return Availability(
            available=True,
            detail="%d of %d RAPL zone(s) readable" % (len(readable), len(zones)),
            detected_hardware=", ".join(sorted(z.name for z in readable)),
        )

    def open(self) -> None:
        if not self.zones:
            self.detect()

    def read(self) -> Sample:
        counters: Dict[str, int] = {}
        for zone in self._readable:
            try:
                counters[zone.key] = zone.read_uj()
            except (OSError, ValueError):
                # A zone that stops answering mid run is dropped from this
                # sample rather than carried forward at a stale value.
                continue
        return Sample(counters=counters, reads=len(counters))

    def reduce(self, samples: Sequence[Sample]) -> Reduction:
        max_range = {
            z.key: z.max_range_uj
            for z in self._readable
            if z.max_range_uj is not None
        }
        per_domain_uj, interval_uj, wrapped, ambiguous = counter_deltas(
            samples, max_range
        )

        joules = sum(per_domain_uj.values()) / 1e6
        per_domain_j = {k: v / 1e6 for k, v in per_domain_uj.items()}

        interval_power: List[float] = []
        for i, total_uj in enumerate(interval_uj):
            dt = samples[i + 1].t_monotonic - samples[i].t_monotonic
            if dt > 0:
                interval_power.append((total_uj / 1e6) / dt)

        readable_keys = [z.key for z in self._readable]
        unreadable = []
        for zone in self.zones:
            if zone.key not in readable_keys:
                unreadable.append("rapl:%s (%s)" % (zone.key, zone.unreadable_reason))

        families_read = sorted({classify_domain(z.name) for z in self._readable})
        families_missing = sorted(
            {classify_domain(z.name) for z in self.zones if z.key not in readable_keys}
        )

        reasons: List[tuple] = [
            (
                "ENERGY_FROM_HARDWARE_COUNTER",
                "info",
                "Energy is the sum of per interval deltas of the powercap energy_uj counters.",
            )
        ]

        for domain in wrapped:
            reasons.append(
                (
                    "COUNTER_WRAPAROUND",
                    "info",
                    "Domain %s rolled over at max_energy_range_uj and the rollover was "
                    "corrected by adding the range back to the negative delta." % domain,
                )
            )
        for domain in ambiguous:
            reasons.append(
                (
                    "COUNTER_WRAPAROUND_AMBIGUOUS",
                    "invalidating",
                    "Domain %s accumulated more than half of max_energy_range_uj inside a "
                    "single sampling interval, so a second rollover in that interval "
                    "cannot be distinguished from none; sample faster or shorten the run."
                    % domain,
                )
            )

        if unreadable:
            reasons.append(
                (
                    "PARTIAL_DOMAIN_COVERAGE",
                    "warning",
                    "Readable domain families: %s. Unreadable: %s."
                    % (", ".join(families_read) or "none", ", ".join(families_missing)),
                )
            )
            if any("permission" in u for u in unreadable):
                reasons.append(
                    (
                        "PERMISSION_DENIED",
                        "warning",
                        "At least one energy_uj file exists but is not readable; the "
                        "attribute has been root only since kernel commit 949dd0104c49 "
                        "(CVE-2020-8694, PLATYPUS).",
                    )
                )

        if families_read and set(families_read) <= {"package"}:
            reasons.append(
                (
                    "PACKAGE_ONLY_COVERAGE",
                    "warning",
                    "Only package domains were readable, so this figure excludes dram and "
                    "psys and is not a system level energy figure.",
                )
            )

        return Reduction(
            joules=joules,
            interval_power_w=interval_power,
            per_domain_j=per_domain_j,
            # energy_uj is an integer microjoule counter, so one read quantizes
            # at 1 uJ.
            quantization_j=1e-6,
            extra={
                "domains_read": readable_keys,
                "domain_families_read": families_read,
                "domain_families_unreadable": families_missing,
                "max_energy_range_uj": {k: max_range.get(k) for k in readable_keys},
                "counter_wraparounds": wrapped,
                "counter_wraparounds_ambiguous": ambiguous,
                "sysfs_root": self.root,
                "coverage_note": self.coverage,
            },
            reasons=reasons,
            unreadable=unreadable,
        )
