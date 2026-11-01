"""Raspberry Pi 5 PMIC: the per rail read structure, and the rate artefact.

The central test here is that a complete sweep rate and a raw channel read
rate are reported as two different numbers. A rate derived from the channel
read count is roughly twice the real sweep rate on this hardware, and that
artefact is the specific error the tool exists to prevent.
"""

from __future__ import annotations

import pytest

from conftest import PmicRunner
from powerstamp.backends.rpi_pmic import RpiPmicBackend, parse_pmic_read_adc

# From the recorded capture: 12 rails carry both a current and a voltage
# channel, 2 channels are voltage only, 26 channel reads per sweep.
EXPECTED_RAILS = 12
EXPECTED_VOLTAGE_ONLY = ["BATT", "EXT5V"]
EXPECTED_CHANNEL_READS = 26


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_counts_the_recorded_capture(pmic_capture):
    reading = parse_pmic_read_adc(pmic_capture)
    assert reading.line_count == EXPECTED_CHANNEL_READS
    assert reading.channel_reads == EXPECTED_CHANNEL_READS
    assert len(reading.amps) == 12
    assert len(reading.volts) == 14
    assert len(reading.power_rails) == EXPECTED_RAILS


def test_voltage_only_channels_are_identified(pmic_capture):
    reading = parse_pmic_read_adc(pmic_capture)
    assert reading.voltage_only == EXPECTED_VOLTAGE_ONLY
    assert reading.current_only == []
    # They are voltage readings, so they must not appear as a power rail.
    assert "EXT5V" not in reading.power_rails
    assert "BATT" not in reading.power_rails


def test_rail_power_is_voltage_times_current(pmic_capture):
    reading = parse_pmic_read_adc(pmic_capture)
    assert reading.power_rails["VDD_CORE"] == pytest.approx(
        0.72097620 * 0.92871990, rel=1e-9
    )
    assert reading.power_rails["DDR_VDDQ"] == pytest.approx(0.0)


def test_channel_indices_are_preserved(pmic_capture):
    reading = parse_pmic_read_adc(pmic_capture)
    assert reading.channel_index["VDD_CORE_A"] == 7
    assert reading.channel_index["VDD_CORE_V"] == 15
    assert reading.channel_index["BATT_V"] == 25


def test_unparseable_lines_are_ignored_not_guessed():
    text = "VDD_CORE_A current(7)=1.0A\nnot a reading at all\nVDD_CORE_V volt(15)=1.0V\n"
    reading = parse_pmic_read_adc(text)
    assert reading.line_count == 3
    assert reading.channel_reads == 2
    assert reading.power_rails == {"VDD_CORE": pytest.approx(1.0)}


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def test_detect_on_the_recorded_capture(pmic_capture):
    backend = RpiPmicBackend(runner=PmicRunner(pmic_capture))
    avail = backend.detect()
    assert avail.available is True
    assert "12 rail(s) with power" in avail.detail
    assert "26 channel read(s) per sweep" in avail.detail


def test_detect_rejects_output_with_no_power_rail():
    backend = RpiPmicBackend(runner=lambda: "EXT5V_V volt(24)=5.0V\n")
    avail = backend.detect()
    assert avail.available is False
    assert "no rail with both a current and a voltage channel" in avail.detail


def test_detect_handles_a_failing_runner():
    def boom():
        raise OSError("vcgencmd: command not found")

    backend = RpiPmicBackend(runner=boom)
    avail = backend.detect()
    assert avail.available is False
    assert "failed" in avail.detail


# ---------------------------------------------------------------------------
# The rate artefact: the point of the tool
# ---------------------------------------------------------------------------


def test_reads_per_sample_is_the_channel_count_not_the_rail_count(pmic_capture):
    backend = RpiPmicBackend(runner=PmicRunner(pmic_capture))
    backend.detect()
    samples = []
    for i in range(5):
        s = backend.read()
        s.t_monotonic = float(i)
        samples.append(s)

    # Each sweep is one sample made of 26 channel reads.
    assert all(s.reads == EXPECTED_CHANNEL_READS for s in samples)
    assert all(len(s.power_w) == EXPECTED_RAILS for s in samples)

    red = backend.reduce(samples)
    assert red.extra["channel_reads_per_sample"] == pytest.approx(
        float(EXPECTED_CHANNEL_READS)
    )
    assert red.extra["rail_count"] == EXPECTED_RAILS


def test_the_engine_reports_sweep_rate_and_channel_read_rate_separately(pmic_capture):
    from powerstamp.backends.base import Availability
    from powerstamp.measure import _build_result

    backend = RpiPmicBackend(runner=PmicRunner(pmic_capture))
    avail = backend.detect()

    # 40 sweeps at a nominal 21.2 Hz, which is the rate the author's own
    # Raspberry Pi 5 work measured where a naive per read count suggested
    # 43 to 44 Hz. The fixture reproduces the structure, not the hardware.
    hz = 21.2
    samples = []
    for i in range(40):
        s = backend.read()
        s.t_monotonic = i / hz
        samples.append(s)

    result = _build_result(
        backend=backend,
        availability=avail,
        rejected=[],
        samples=samples,
        duration_s=39 / hz,
        read_errors=[],
        had_closing_read=False,
        requested_hz=hz,
        baseline_w=None,
        baseline_source="none",
        label="pmic",
        metadata={},
        tool_version="0.1.0",
    )

    assert result.rate is not None
    assert result.rate.achieved_hz == pytest.approx(hz, rel=1e-9)
    assert result.mean_reads_per_sample == pytest.approx(26.0)
    assert result.channel_read_hz == pytest.approx(hz * 26.0, rel=1e-9)

    # The two rates are different numbers in different fields, and the channel
    # read rate is the larger one. A tool that reported the channel read rate
    # as the sampling rate would be claiming roughly twice the real rate.
    assert result.channel_read_hz > result.rate.achieved_hz
    assert result.channel_read_hz / result.rate.achieved_hz == pytest.approx(26.0)

    block = result.to_dict()["measurement"]
    assert block["rate"]["achieved_hz"] == pytest.approx(hz, rel=1e-9)
    assert block["channel_read_hz"] == pytest.approx(hz * 26.0, rel=1e-9)
    assert "artefact" in block["rate_note"]


# ---------------------------------------------------------------------------
# Coverage honesty
# ---------------------------------------------------------------------------


def test_reduce_reports_the_scope_caveats(pmic_capture):
    backend = RpiPmicBackend(runner=PmicRunner(pmic_capture))
    backend.detect()
    samples = []
    for i in range(3):
        s = backend.read()
        s.t_monotonic = float(i)
        samples.append(s)
    red = backend.reduce(samples)

    codes = [c for c, _s, _d in red.reasons]
    assert "PMIC_EXCLUDES_EXTERNAL_5V" in codes
    assert "PMIC_VOLTAGE_ONLY_CHANNELS" in codes
    assert "ENERGY_FROM_POWER_INTEGRAL" in codes
    assert "ENERGY_FROM_HARDWARE_COUNTER" not in codes

    assert red.extra["voltage_only_channels"] == EXPECTED_VOLTAGE_ONLY
    joined = " ".join(red.unreadable)
    assert "EXT5V" in joined
    assert "BATT" in joined


def test_quantization_is_zero_and_the_omission_is_stated(pmic_capture):
    backend = RpiPmicBackend(runner=PmicRunner(pmic_capture))
    backend.detect()
    samples = []
    for i in range(3):
        s = backend.read()
        s.t_monotonic = float(i)
        samples.append(s)
    red = backend.reduce(samples)
    assert red.quantization_j == 0.0
    assert "not published" in red.extra["quantization_note"]


def test_changing_current_changes_the_integral(pmic_capture):
    runner = PmicRunner(pmic_capture, scales=[1.0, 1.0, 2.0, 3.0])
    backend = RpiPmicBackend(runner=runner)
    backend.detect()
    samples = []
    for i in range(3):
        s = backend.read()
        s.t_monotonic = float(i)
        samples.append(s)
    red = backend.reduce(samples)
    # The sampled power rose, so the per interval power series must not be flat.
    assert len(set(round(p, 9) for p in red.interval_power_w)) > 1
