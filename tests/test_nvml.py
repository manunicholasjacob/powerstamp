"""NVML backend: the hardware energy counter and the power sampling fallback.

The two are not the same measurement, and the output has to say which one was
used. These tests run against a fake NVML module, so they exercise the full
path on a machine with no NVIDIA GPU.
"""

from __future__ import annotations

import pytest

from conftest import FakeNvml
from powerstamp.backends.nvml import (
    SOURCE_ENERGY_COUNTER,
    SOURCE_POWER_SAMPLING,
    NvmlBackend,
)


def _samples(backend, count, dt=1.0):
    out = []
    for i in range(count):
        s = backend.read()
        s.t_monotonic = i * dt
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def test_no_pynvml_is_unavailable():
    backend = NvmlBackend(nvml=None)
    # No injected module, and pynvml is not installed in the test environment.
    avail = backend.detect()
    if avail.available:
        pytest.skip("pynvml is installed in this environment")
    assert "pynvml is not importable" in avail.detail


def test_zero_devices_is_unavailable():
    backend = NvmlBackend(nvml=FakeNvml(device_count=0))
    avail = backend.detect()
    assert avail.available is False
    assert "zero devices" in avail.detail


# ---------------------------------------------------------------------------
# Hardware energy counter path
# ---------------------------------------------------------------------------


def test_energy_counter_path_is_labelled_as_a_counter():
    nvml = FakeNvml(energy_mj=[1000, 2000, 3000, 4000, 5000])
    backend = NvmlBackend(nvml=nvml)
    avail = backend.detect()
    assert avail.available is True
    assert backend.energy_source == SOURCE_ENERGY_COUNTER
    assert backend.reduction_kind == "counter"

    red = backend.reduce(_samples(backend, 4))
    codes = [c for c, _s, _d in red.reasons]
    assert "ENERGY_FROM_HARDWARE_COUNTER" in codes
    assert "ENERGY_FROM_POWER_INTEGRAL" not in codes
    assert red.extra["energy_source"] == SOURCE_ENERGY_COUNTER
    assert red.quantization_j == pytest.approx(1e-3)


def test_energy_counter_delta_is_in_joules():
    # detect() consumes one counter read as its support probe, so the read
    # sequence used by the samples starts at the second scripted value.
    nvml = FakeNvml(energy_mj=[0, 1000, 2000, 3000, 4000])
    backend = NvmlBackend(nvml=nvml)
    backend.detect()
    red = backend.reduce(_samples(backend, 4))
    # Three intervals of 1000 mJ each.
    assert red.joules == pytest.approx(3.0)
    assert red.per_domain_j["gpu0"] == pytest.approx(3.0)


def test_board_scope_is_not_asserted():
    nvml = FakeNvml(energy_mj=[0, 1000])
    backend = NvmlBackend(nvml=nvml)
    backend.detect()
    red = backend.reduce(_samples(backend, 2))
    codes = [c for c, _s, _d in red.reasons]
    assert "GPU_BOARD_SCOPE_UNSTATED" in codes
    detail = [d for c, _s, d in red.reasons if c == "GPU_BOARD_SCOPE_UNSTATED"][0]
    assert "whole board" in detail


def test_counter_going_backwards_is_invalidating():
    # A since-driver-load counter does not roll over, so a fall means a driver
    # reload and the elapsed energy is not recoverable.
    nvml = FakeNvml(energy_mj=[0, 5000, 10_000, 100])
    backend = NvmlBackend(nvml=nvml)
    backend.detect()
    red = backend.reduce(_samples(backend, 3))
    severities = {c: s for c, s, _d in red.reasons}
    assert severities.get("COUNTER_WRAPAROUND_AMBIGUOUS") == "invalidating"


# ---------------------------------------------------------------------------
# Power sampling fallback
# ---------------------------------------------------------------------------


def test_unsupported_energy_counter_falls_back_to_power_sampling():
    nvml = FakeNvml(support_energy=False, power_mw=[100_000])
    backend = NvmlBackend(nvml=nvml)
    avail = backend.detect()
    assert avail.available is True
    assert backend.energy_source == SOURCE_POWER_SAMPLING
    assert backend.reduction_kind == "sampler"
    assert "nvmlDeviceGetPowerUsage" in avail.detail


def test_power_sampling_fallback_says_it_is_an_integral_not_a_counter():
    nvml = FakeNvml(support_energy=False, power_mw=[100_000])
    backend = NvmlBackend(nvml=nvml)
    backend.detect()
    red = backend.reduce(_samples(backend, 5))
    codes = [c for c, _s, _d in red.reasons]
    assert "ENERGY_FROM_POWER_INTEGRAL" in codes
    assert "ENERGY_FROM_HARDWARE_COUNTER" not in codes
    assert "PARTIAL_DOMAIN_COVERAGE" in codes
    detail = [d for c, _s, d in red.reasons if c == "PARTIAL_DOMAIN_COVERAGE"][0]
    assert "Volta or newer" in detail
    assert red.extra["energy_source"] == SOURCE_POWER_SAMPLING
    assert red.extra["energy_counter_unsupported_devices"] == ["gpu0"]


def test_power_integral_arithmetic():
    # 100 W held over four 1 s intervals is 400 J.
    nvml = FakeNvml(support_energy=False, power_mw=[100_000])
    backend = NvmlBackend(nvml=nvml)
    backend.detect()
    red = backend.reduce(_samples(backend, 5, dt=1.0))
    assert red.joules == pytest.approx(400.0)


def test_shutdown_is_called_on_close():
    nvml = FakeNvml(energy_mj=[0, 1])
    backend = NvmlBackend(nvml=nvml)
    backend.detect()
    backend.close()
    assert nvml.shutdown_calls == 1


def test_multi_device_sums():
    nvml = FakeNvml(device_count=2, support_energy=False, power_mw=[50_000])
    backend = NvmlBackend(nvml=nvml)
    backend.detect()
    red = backend.reduce(_samples(backend, 3, dt=1.0))
    # Two devices at 50 W each over two 1 s intervals.
    assert red.joules == pytest.approx(200.0)
    assert sorted(red.per_domain_j) == ["gpu0", "gpu1"]
