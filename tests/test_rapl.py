"""RAPL backend: discovery, partial domain availability, permissions, reduction."""

from __future__ import annotations

import os

import pytest

from conftest import ScriptedReader, write_powercap_tree, zone_dir
from powerstamp.backends.rapl import (
    RaplBackend,
    classify_domain,
    discover_zones,
)


def _energy_path(root: str, *ids: int) -> str:
    return os.path.normpath(os.path.join(root, zone_dir(*ids), "energy_uj"))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,family",
    [
        ("package-0", "package"),
        ("package-1", "package"),
        ("core", "core"),
        ("uncore", "uncore"),
        ("dram", "dram"),
        ("psys", "psys"),
        ("something-vendor-specific", "other"),
    ],
)
def test_classify_domain(name, family):
    assert classify_domain(name) == family


def test_classify_domain_does_not_guess():
    # An unrecognised domain is reported as other rather than folded into a
    # family it might not belong to.
    assert classify_domain("mystery-zone") == "other"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_zones_finds_package_core_dram_psys(powercap_full):
    zones = discover_zones(powercap_full)
    names = sorted(z.name for z in zones)
    assert names == ["core", "dram", "package-0", "psys"]
    for zone in zones:
        assert zone.max_range_uj == 262_143_328_850


def test_detect_reports_no_powercap(tmp_path):
    backend = RaplBackend(root=str(tmp_path / "does-not-exist"))
    avail = backend.detect()
    assert avail.available is False
    assert "no powercap sysfs" in avail.detail


def test_detect_reports_empty_powercap(powercap_empty):
    backend = RaplBackend(root=powercap_empty)
    avail = backend.detect()
    assert avail.available is False
    assert "no RAPL zones" in avail.detail


def test_detect_all_domains_readable(powercap_full):
    backend = RaplBackend(root=powercap_full)
    avail = backend.detect()
    assert avail.available is True
    assert avail.detail == "4 of 4 RAPL zone(s) readable"


# ---------------------------------------------------------------------------
# The permission restriction introduced after PLATYPUS
# ---------------------------------------------------------------------------


def test_detect_all_energy_uj_denied_cites_the_commit(powercap_full):
    denied = [
        _energy_path(powercap_full, *ids)
        for ids in ((0,), (0, 0), (0, 1), (1,))
    ]
    backend = RaplBackend(root=powercap_full, reader=ScriptedReader(denied=denied))
    avail = backend.detect()
    assert avail.available is False
    assert "949dd0104c49" in avail.detail
    assert "CVE-2020-8694" in avail.detail
    assert avail.detected_hardware == "4 RAPL zone(s)"


def test_partial_domain_availability_is_reported_not_zeroed(powercap_package_only):
    # dram and psys exist but cannot be read. They must appear as absent, not
    # as zero, and the figure must be marked package only.
    denied = [
        _energy_path(powercap_package_only, 0, 1),
        _energy_path(powercap_package_only, 1),
    ]
    # detect() takes one probe read per zone before the measurement starts, so
    # the scripted sequence carries one leading value for the probe and then
    # four for the four measurement reads.
    reader = ScriptedReader(
        scripts={
            _energy_path(powercap_package_only, 0): [
                0,
                1_000_000,
                2_000_000,
                3_000_000,
                4_000_000,
            ]
        },
        denied=denied,
    )
    backend = RaplBackend(root=powercap_package_only, reader=reader)
    avail = backend.detect()
    assert avail.available is True
    assert avail.detail == "1 of 3 RAPL zone(s) readable"

    samples = []
    for i in range(4):
        s = backend.read()
        s.t_monotonic = float(i)
        samples.append(s)

    red = backend.reduce(samples)

    # 1 J accumulated per interval over three intervals.
    assert red.joules == pytest.approx(3.0)
    assert red.extra["domain_families_read"] == ["package"]
    assert red.extra["domain_families_unreadable"] == ["dram", "psys"]

    codes = [c for c, _s, _d in red.reasons]
    assert "PARTIAL_DOMAIN_COVERAGE" in codes
    assert "PACKAGE_ONLY_COVERAGE" in codes
    assert "PERMISSION_DENIED" in codes

    # The unreadable list names what is absent so a consumer cannot read the
    # missing domains as zero.
    joined = " ".join(red.unreadable)
    assert "dram" in joined
    assert "psys" in joined
    assert len(red.unreadable) == 2


def test_full_coverage_does_not_claim_package_only(powercap_full):
    # The leading value is consumed by the detect() probe.
    scripts = {
        _energy_path(powercap_full, *ids): [base, base, base + 1_000_000]
        for ids, base in (
            ((0,), 1_000_000),
            ((0, 0), 500_000),
            ((0, 1), 200_000),
            ((1,), 3_000_000),
        )
    }
    backend = RaplBackend(root=powercap_full, reader=ScriptedReader(scripts=scripts))
    assert backend.detect().available is True

    samples = []
    for i in range(2):
        s = backend.read()
        s.t_monotonic = float(i)
        samples.append(s)
    red = backend.reduce(samples)

    codes = [c for c, _s, _d in red.reasons]
    assert "PACKAGE_ONLY_COVERAGE" not in codes
    assert "PARTIAL_DOMAIN_COVERAGE" not in codes
    assert red.unreadable == []
    # Four domains each advanced 1 J.
    assert red.joules == pytest.approx(4.0)
    assert sorted(red.extra["domain_families_read"]) == [
        "core",
        "dram",
        "package",
        "psys",
    ]


def test_energy_is_labelled_as_a_hardware_counter(powercap_full):
    backend = RaplBackend(root=powercap_full)
    backend.detect()
    samples = []
    for i in range(2):
        s = backend.read()
        s.t_monotonic = float(i)
        samples.append(s)
    red = backend.reduce(samples)
    codes = [c for c, _s, _d in red.reasons]
    assert "ENERGY_FROM_HARDWARE_COUNTER" in codes
    assert "ENERGY_FROM_POWER_INTEGRAL" not in codes


def test_quantization_is_one_microjoule(powercap_full):
    backend = RaplBackend(root=powercap_full)
    backend.detect()
    samples = []
    for i in range(2):
        s = backend.read()
        s.t_monotonic = float(i)
        samples.append(s)
    red = backend.reduce(samples)
    assert red.quantization_j == pytest.approx(1e-6)


def test_zone_without_max_range_is_still_discovered(tmp_path):
    root = str(tmp_path / "powercap-norange")
    write_powercap_tree(
        root,
        [
            {
                "dir": zone_dir(0),
                "name": "package-0",
                "energy_uj": 10,
                "max_energy_range_uj": None,
            }
        ],
    )
    zones = discover_zones(root)
    assert len(zones) == 1
    assert zones[0].max_range_uj is None
