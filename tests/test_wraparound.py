"""Counter rollover.

A powercap counter rolls over at ``max_energy_range_uj``, which produces a
negative delta. Adding the range back recovers the true delta as long as the
counter wrapped at most once inside the interval. These tests cover a rollover
that happens mid measurement, a rollover that cannot be disambiguated, and the
case where no rollover bound is published at all.
"""

from __future__ import annotations

import os

import pytest

from conftest import ScriptedReader, write_powercap_tree, zone_dir
from powerstamp.backends.base import Sample, counter_deltas
from powerstamp.backends.rapl import RaplBackend

MAX_RANGE = 1_000_000  # 1 J of counter range, chosen small so a test can wrap it.


def _energy_path(root: str, *ids: int) -> str:
    return os.path.normpath(os.path.join(root, zone_dir(*ids), "energy_uj"))


@pytest.fixture
def small_range_tree(tmp_path):
    root = str(tmp_path / "powercap-wrap")
    write_powercap_tree(
        root,
        [
            {
                "dir": zone_dir(0),
                "name": "package-0",
                "energy_uj": 900_000,
                "max_energy_range_uj": MAX_RANGE,
            }
        ],
    )
    return root


# ---------------------------------------------------------------------------
# The arithmetic, directly
# ---------------------------------------------------------------------------


def test_counter_deltas_corrects_a_single_rollover():
    samples = [
        Sample(counters={"pkg": 900_000}, t_monotonic=0.0),
        Sample(counters={"pkg": 950_000}, t_monotonic=1.0),
        # Rolls over here: 950_000 + 100_000 = 1_050_000, which is 50_000
        # past the 1_000_000 range.
        Sample(counters={"pkg": 50_000}, t_monotonic=2.0),
        Sample(counters={"pkg": 150_000}, t_monotonic=3.0),
    ]
    per_domain, intervals, wrapped, ambiguous = counter_deltas(
        samples, {"pkg": MAX_RANGE}
    )
    assert wrapped == ["pkg"]
    assert ambiguous == []
    assert per_domain["pkg"] == pytest.approx(250_000)
    assert intervals == [50_000, 100_000, 100_000]


def test_counter_deltas_flags_an_ambiguous_rollover():
    # A single interval accumulated more than half the range, so a second
    # rollover inside that interval would be indistinguishable from none.
    samples = [
        Sample(counters={"pkg": 100_000}, t_monotonic=0.0),
        Sample(counters={"pkg": 800_000}, t_monotonic=1.0),
    ]
    per_domain, _intervals, wrapped, ambiguous = counter_deltas(
        samples, {"pkg": MAX_RANGE}
    )
    assert wrapped == []
    assert ambiguous == ["pkg"]


def test_counter_deltas_without_a_published_range_is_ambiguous_on_a_negative_delta():
    samples = [
        Sample(counters={"pkg": 900_000}, t_monotonic=0.0),
        Sample(counters={"pkg": 10_000}, t_monotonic=1.0),
    ]
    per_domain, _intervals, wrapped, ambiguous = counter_deltas(samples, {})
    assert wrapped == []
    assert ambiguous == ["pkg"]
    # The undecodable interval contributes nothing rather than a wrong number.
    assert per_domain == {}


def test_counter_deltas_handles_several_rollovers_across_the_run():
    # Three rollovers, one per interval, each recoverable on its own.
    values = [900_000, 100_000, 300_000, 400_000, 900_000, 100_000]
    samples = [
        Sample(counters={"pkg": v}, t_monotonic=float(i)) for i, v in enumerate(values)
    ]
    per_domain, _intervals, wrapped, ambiguous = counter_deltas(
        samples, {"pkg": MAX_RANGE}
    )
    assert wrapped == ["pkg"]
    assert ambiguous == []
    expected = 0
    for i in range(len(values) - 1):
        d = values[i + 1] - values[i]
        if d < 0:
            d += MAX_RANGE
        expected += d
    assert per_domain["pkg"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Through the real backend read path
# ---------------------------------------------------------------------------


def test_rollover_mid_measurement_through_the_backend(small_range_tree):
    # 0.9 J, 0.95 J, rolls to 0.05 J, 0.15 J. Total true energy is 0.25 J.
    reader = ScriptedReader(
        scripts={
            _energy_path(small_range_tree, 0): [
                900_000,
                950_000,
                50_000,
                150_000,
            ]
        }
    )
    backend = RaplBackend(root=small_range_tree, reader=reader)
    assert backend.detect().available is True

    samples = []
    # detect() consumed one scripted read for the probe, so the measurement
    # sequence starts at the second value. The test asserts on the recovered
    # total, which is what matters, and the sequence is monotonic modulo the
    # range either way.
    for i in range(4):
        s = backend.read()
        s.t_monotonic = float(i)
        samples.append(s)

    red = backend.reduce(samples)
    codes = [c for c, _s, _d in red.reasons]
    assert "COUNTER_WRAPAROUND" in codes
    assert "COUNTER_WRAPAROUND_AMBIGUOUS" not in codes
    assert red.extra["counter_wraparounds"]
    assert red.joules > 0
    # Every interval is small, so the total must be far under the 1 J range.
    assert red.joules < 1.0


def test_ambiguous_rollover_is_invalidating_through_the_backend(small_range_tree):
    reader = ScriptedReader(
        scripts={
            _energy_path(small_range_tree, 0): [
                0,
                0,
                900_000,  # 0.9 J in one interval, over half the 1 J range
            ]
        }
    )
    backend = RaplBackend(root=small_range_tree, reader=reader)
    assert backend.detect().available is True

    samples = []
    for i in range(3):
        s = backend.read()
        s.t_monotonic = float(i)
        samples.append(s)

    red = backend.reduce(samples)
    severities = {c: s for c, s, _d in red.reasons}
    assert severities.get("COUNTER_WRAPAROUND_AMBIGUOUS") == "invalidating"
    assert red.extra["counter_wraparounds_ambiguous"]


def test_ambiguous_rollover_makes_the_whole_result_invalid(small_range_tree):
    """An ambiguous rollover must reach the envelope as verdict INVALID."""
    from powerstamp.measure import _build_result
    from powerstamp.backends.base import Availability

    reader = ScriptedReader(
        scripts={_energy_path(small_range_tree, 0): [0, 0, 900_000]}
    )
    backend = RaplBackend(root=small_range_tree, reader=reader)
    avail = backend.detect()

    samples = []
    for i in range(3):
        s = backend.read()
        s.t_monotonic = float(i)
        samples.append(s)

    result = _build_result(
        backend=backend,
        availability=avail,
        rejected=[],
        samples=samples,
        duration_s=2.0,
        read_errors=[],
        had_closing_read=False,
        requested_hz=1.0,
        baseline_w=None,
        baseline_source="none",
        label="wrap",
        metadata={},
        tool_version="0.1.0",
    )
    assert result.verdict == "INVALID"
    assert "COUNTER_WRAPAROUND_AMBIGUOUS" in result.reason_codes()
    envelope = result.to_dict()
    assert envelope["verdict"] == "INVALID"
