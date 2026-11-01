"""Android fuel gauge: discovery, the battery side caveat, gauge coarseness."""

from __future__ import annotations

import os

import pytest

from conftest import write_power_supply_tree
from powerstamp.backends.android import (
    MIN_DISTINCT_CHARGE_VALUES,
    AndroidBackend,
    find_battery_node,
)
from powerstamp.backends.base import Sample


def _samples(charges, volts, dt=1.0):
    out = []
    for i, (c, v) in enumerate(zip(charges, volts)):
        out.append(
            Sample(
                counters={"charge_counter_uah": c, "voltage_now_uv": v},
                t_monotonic=i * dt,
                reads=2,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_find_battery_node(power_supply_root):
    node = find_battery_node(power_supply_root)
    assert node is not None
    assert os.path.basename(node) == "battery"


def test_find_battery_node_accepts_a_vendor_name(tmp_path):
    root = str(tmp_path / "ps")
    write_power_supply_tree(root, node="bms")
    node = find_battery_node(root)
    assert node is not None
    assert os.path.basename(node) == "bms"


def test_detect_without_a_power_supply_tree(tmp_path):
    backend = AndroidBackend(power_supply_root=str(tmp_path / "absent"))
    avail = backend.detect()
    assert avail.available is False
    assert "charge_counter" in avail.detail


def test_detect_with_a_fuel_gauge(power_supply_root):
    backend = AndroidBackend(
        power_supply_root=power_supply_root, batterystats_runner=lambda: ""
    )
    avail = backend.detect()
    assert avail.available is True
    assert "fuel gauge at" in avail.detail


def test_batterystats_is_recorded_as_context_only(power_supply_root):
    backend = AndroidBackend(
        power_supply_root=power_supply_root,
        batterystats_runner=lambda: "Battery History (0% used):\n0 (1) RESET\n",
    )
    avail = backend.detect()
    assert avail.available is True
    assert "batterystats available" in avail.detail
    red = backend.reduce(_samples([100, 90, 80, 70], [3_900_000] * 4))
    assert red.extra["batterystats_available"] is True
    assert "not the source of the joule figure" in red.extra["batterystats_role"]


# ---------------------------------------------------------------------------
# The loud caveat, in the data
# ---------------------------------------------------------------------------


def test_battery_side_caveat_is_always_emitted(power_supply_root):
    backend = AndroidBackend(
        power_supply_root=power_supply_root, batterystats_runner=lambda: ""
    )
    backend.detect()
    red = backend.reduce(_samples([1000, 900, 800, 700], [3_900_000] * 4))
    reasons = {c: (s, d) for c, s, d in red.reasons}
    assert "BATTERY_SIDE_READING" in reasons
    severity, detail = reasons["BATTERY_SIDE_READING"]
    assert severity == "warning"
    assert "display" in detail
    assert "modem" in detail


def test_energy_arithmetic_uses_charge_times_voltage(power_supply_root):
    backend = AndroidBackend(
        power_supply_root=power_supply_root, batterystats_runner=lambda: ""
    )
    backend.detect()
    # 100 uAh drawn per interval over three intervals at 4.0 V.
    red = backend.reduce(_samples([1000, 900, 800, 700], [4_000_000] * 4))
    per_interval = 100 * 3.6e-3 * 4.0
    assert red.joules == pytest.approx(3 * per_interval)
    assert red.per_domain_j["battery_terminal"] == pytest.approx(red.joules)


def test_too_few_distinct_gauge_values_is_invalidating(power_supply_root):
    backend = AndroidBackend(
        power_supply_root=power_supply_root, batterystats_runner=lambda: ""
    )
    backend.detect()
    # Polled ten times, but the gauge only ever showed two values.
    charges = [1000] * 5 + [900] * 5
    red = backend.reduce(_samples(charges, [3_900_000] * 10))
    reasons = {c: s for c, s, _d in red.reasons}
    assert reasons.get("SAMPLE_COUNT_TOO_LOW") == "invalidating"
    assert red.extra["distinct_charge_counter_values"] == 2
    assert red.extra["min_distinct_charge_values_required"] == MIN_DISTINCT_CHARGE_VALUES


def test_enough_distinct_gauge_values_is_not_refused(power_supply_root):
    backend = AndroidBackend(
        power_supply_root=power_supply_root, batterystats_runner=lambda: ""
    )
    backend.detect()
    charges = [1000, 950, 900, 850, 800]
    red = backend.reduce(_samples(charges, [3_900_000] * 5))
    reasons = {c: s for c, s, _d in red.reasons}
    assert "SAMPLE_COUNT_TOO_LOW" not in reasons
    assert red.extra["distinct_charge_counter_values"] == 5
    assert red.extra["observed_charge_step_uah"] == 50


def test_a_charging_device_is_invalidating(power_supply_root):
    backend = AndroidBackend(
        power_supply_root=power_supply_root, batterystats_runner=lambda: ""
    )
    backend.detect()
    # The counter rose, so the device was charging. No discharge energy can be
    # attributed, and the result must say so rather than report zero joules.
    red = backend.reduce(_samples([700, 800, 900, 1000], [3_900_000] * 4))
    reasons = {c: s for c, s, _d in red.reasons}
    assert reasons.get("NO_READABLE_DOMAINS") == "invalidating"
    assert red.joules == 0.0


def test_read_path_against_the_fake_sysfs(power_supply_root):
    backend = AndroidBackend(
        power_supply_root=power_supply_root, batterystats_runner=lambda: ""
    )
    backend.detect()
    sample = backend.read()
    assert sample.counters["charge_counter_uah"] == 3_000_000
    assert sample.counters["voltage_now_uv"] == 3_900_000
    assert sample.reads == 2


def test_quantization_uses_the_observed_step(power_supply_root):
    backend = AndroidBackend(
        power_supply_root=power_supply_root, batterystats_runner=lambda: ""
    )
    backend.detect()
    red = backend.reduce(_samples([1000, 950, 900, 850], [3_900_000] * 4))
    assert red.quantization_j == pytest.approx(50 * 3.6e-3 * 3.85)
    assert "not published" in red.extra["quantization_note"]
