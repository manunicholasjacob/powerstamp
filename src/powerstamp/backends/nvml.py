"""NVIDIA GPUs via NVML.

Two different measurements live behind this one backend, and which one was used
is recorded in the output because they are not interchangeable.

``nvmlDeviceGetTotalEnergyConsumption``
    A cumulative hardware energy counter, in millijoules, counting since the
    driver was last loaded. NVIDIA documents it as supported on "Volta or
    newer fully supported devices", and it returns NVML_ERROR_NOT_SUPPORTED on
    older parts. NVML describes the figure as being for "this GPU" and does
    not state which internal rails it covers, so the output carries a reason
    code saying the board scope is unstated rather than asserting that it is a
    whole board figure. The counter also has an update period that NVIDIA does
    not publish, which is one more reason the achieved sampling rate and the
    refusal rule matter here.

``nvmlDeviceGetPowerUsage``
    Instantaneous power in milliwatts. Integrating it gives an energy figure,
    but the integral of sampled power is a different measurement from a
    hardware energy counter, and the output says which one it is.

The backend takes an injectable NVML module so the whole path is exercised by
the test suite on a machine with no NVIDIA GPU.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .base import (
    REDUCTION_COUNTER,
    REDUCTION_SAMPLER,
    Availability,
    Backend,
    Reduction,
    Sample,
)

SOURCE_ENERGY_COUNTER = "nvmlDeviceGetTotalEnergyConsumption"
SOURCE_POWER_SAMPLING = "nvmlDeviceGetPowerUsage"


def _import_nvml() -> Optional[Any]:
    try:
        import pynvml  # type: ignore

        return pynvml
    except Exception:
        return None


class NvmlBackend(Backend):
    name = "nvml"
    reduction_kind = REDUCTION_COUNTER
    coverage = (
        "The NVIDIA device as NVML reports it. NVML does not state which "
        "internal rails the device figure covers. Never covers the host CPU, "
        "host memory or anything outside the card."
    )

    def __init__(self, nvml: Optional[Any] = None, device_indices: Optional[Sequence[int]] = None) -> None:
        self._nvml = nvml
        self._injected = nvml is not None
        self._handles: List[Any] = []
        self._indices: List[int] = list(device_indices) if device_indices else []
        self._names: Dict[int, str] = {}
        self.energy_source = SOURCE_ENERGY_COUNTER
        self._unsupported_energy: List[int] = []

    # -- detection ---------------------------------------------------------

    def detect(self) -> Availability:
        nvml = self._nvml if self._injected else _import_nvml()
        if nvml is None:
            return Availability(available=False, detail="pynvml is not importable")
        self._nvml = nvml
        try:
            nvml.nvmlInit()
        except Exception as exc:
            return Availability(available=False, detail="nvmlInit failed: %s" % exc)

        try:
            count = nvml.nvmlDeviceGetCount()
        except Exception as exc:
            return Availability(available=False, detail="nvmlDeviceGetCount failed: %s" % exc)
        if count == 0:
            return Availability(available=False, detail="NVML reports zero devices")

        wanted = self._indices or list(range(count))
        handles = []
        names = {}
        for idx in wanted:
            try:
                h = nvml.nvmlDeviceGetHandleByIndex(idx)
            except Exception:
                continue
            handles.append(h)
            try:
                raw = nvml.nvmlDeviceGetName(h)
            except Exception:
                raw = "device %d" % idx
            if isinstance(raw, bytes):
                raw = raw.decode("ascii", "replace")
            names[idx] = raw
        if not handles:
            return Availability(available=False, detail="no NVML device handle could be opened")

        self._handles = handles
        self._indices = wanted[: len(handles)]
        self._names = names

        # Decide the energy source once, by trying the counter on every device.
        unsupported = []
        for idx, h in zip(self._indices, self._handles):
            try:
                nvml.nvmlDeviceGetTotalEnergyConsumption(h)
            except Exception:
                unsupported.append(idx)
        self._unsupported_energy = unsupported
        if unsupported:
            self.energy_source = SOURCE_POWER_SAMPLING
            self.reduction_kind = REDUCTION_SAMPLER
        else:
            self.energy_source = SOURCE_ENERGY_COUNTER
            self.reduction_kind = REDUCTION_COUNTER

        return Availability(
            available=True,
            detail="%d NVML device(s), energy source %s" % (len(handles), self.energy_source),
            detected_hardware=", ".join(names.get(i, "device %d" % i) for i in self._indices),
        )

    def open(self) -> None:
        if not self._handles:
            self.detect()

    def close(self) -> None:
        if self._nvml is not None:
            try:
                self._nvml.nvmlShutdown()
            except Exception:
                pass

    # -- sampling ----------------------------------------------------------

    def read(self) -> Sample:
        nvml = self._nvml
        assert nvml is not None
        sample = Sample()
        reads = 0
        if self.energy_source == SOURCE_ENERGY_COUNTER:
            for idx, h in zip(self._indices, self._handles):
                try:
                    mj = nvml.nvmlDeviceGetTotalEnergyConsumption(h)
                except Exception:
                    continue
                sample.counters["gpu%d" % idx] = int(mj)
                reads += 1
        else:
            for idx, h in zip(self._indices, self._handles):
                try:
                    mw = nvml.nvmlDeviceGetPowerUsage(h)
                except Exception:
                    continue
                sample.power_w["gpu%d" % idx] = float(mw) / 1000.0
                reads += 1
        sample.reads = reads
        return sample

    # -- reduction ---------------------------------------------------------

    def reduce(self, samples: Sequence[Sample]) -> Reduction:
        device_block = {
            "gpu%d" % i: self._names.get(i, "device %d" % i) for i in self._indices
        }
        common = {
            "energy_source": self.energy_source,
            "devices": device_block,
            "coverage_note": self.coverage,
            "energy_counter_unsupported_devices": [
                "gpu%d" % i for i in self._unsupported_energy
            ],
        }
        board_scope_reason = (
            "GPU_BOARD_SCOPE_UNSTATED",
            "warning",
            "NVML reports the figure for the device without stating which internal "
            "rails it covers, so do not present it as a whole board number.",
        )

        if self.energy_source == SOURCE_ENERGY_COUNTER:
            return self._reduce_counter(samples, common, board_scope_reason)
        return self._reduce_sampler(samples, common, board_scope_reason)

    def _reduce_counter(self, samples, common, board_scope_reason) -> Reduction:
        per_device_mj: Dict[str, float] = {}
        interval_power: List[float] = []
        reasons: List[tuple] = [
            (
                "ENERGY_FROM_HARDWARE_COUNTER",
                "info",
                "Energy is the delta of nvmlDeviceGetTotalEnergyConsumption, a cumulative "
                "millijoule counter, not an integral of sampled power.",
            ),
            board_scope_reason,
        ]
        reset: List[str] = []

        for i in range(len(samples) - 1):
            a, b = samples[i].counters, samples[i + 1].counters
            dt = samples[i + 1].t_monotonic - samples[i].t_monotonic
            total_mj = 0.0
            for key in set(a) & set(b):
                delta = b[key] - a[key]
                if delta < 0:
                    # The counter is a 64 bit cumulative value since driver
                    # load, so it does not roll over in practice. A negative
                    # delta means the driver was reloaded mid run, which is not
                    # recoverable rather than correctable.
                    if key not in reset:
                        reset.append(key)
                    continue
                per_device_mj[key] = per_device_mj.get(key, 0.0) + delta
                total_mj += delta
            if dt > 0:
                interval_power.append((total_mj / 1000.0) / dt)

        for key in reset:
            reasons.append(
                (
                    "COUNTER_WRAPAROUND_AMBIGUOUS",
                    "invalidating",
                    "The cumulative energy counter for %s went backwards, which for a "
                    "since-driver-load counter means the driver was reloaded during the "
                    "run; the elapsed energy is not recoverable." % key,
                )
            )

        joules = sum(per_device_mj.values()) / 1000.0
        return Reduction(
            joules=joules,
            interval_power_w=interval_power,
            per_domain_j={k: v / 1000.0 for k, v in per_device_mj.items()},
            # The counter is an integer millijoule value.
            quantization_j=1e-3,
            extra=common,
            reasons=reasons,
            unreadable=[],
        )

    def _reduce_sampler(self, samples, common, board_scope_reason) -> Reduction:
        from .base import integrate_power

        joules, interval_power, per_rail = integrate_power(samples)
        reasons: List[tuple] = [
            (
                "ENERGY_FROM_POWER_INTEGRAL",
                "info",
                "nvmlDeviceGetTotalEnergyConsumption was unavailable on at least one "
                "device, so energy is the trapezoidal integral of nvmlDeviceGetPowerUsage. "
                "That is a different measurement from a hardware energy counter.",
            ),
            board_scope_reason,
        ]
        if self._unsupported_energy:
            reasons.append(
                (
                    "PARTIAL_DOMAIN_COVERAGE",
                    "warning",
                    "The hardware energy counter is unavailable on %s, which NVIDIA "
                    "documents as supported on Volta or newer devices only."
                    % ", ".join("gpu%d" % i for i in self._unsupported_energy),
                )
            )
        return Reduction(
            joules=joules,
            interval_power_w=interval_power,
            per_domain_j=per_rail,
            # nvmlDeviceGetPowerUsage returns integer milliwatts. One sample
            # quantizes at 1 mW, which over the run contributes 1 mW times the
            # duration; expressed per read that is 1e-3 J per second of run.
            quantization_j=1e-3,
            extra=common,
            reasons=reasons,
            unreadable=[],
        )
