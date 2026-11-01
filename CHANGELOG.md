# Changelog

All notable changes to this project are recorded here. The format follows
Keep a Changelog, and the project uses semantic versioning.

## [Unreleased]

Nothing yet.

## [0.1.0] - unreleased, not tagged and not published

First working version. Built, tested and documented; no tag pushed, nothing
uploaded to PyPI, no GitHub release. See `RELEASE_CHECKLIST.md` for what is
left.

### Added

- Library API: `Powerstamp` context manager, `powerstamp` decorator, and a
  `measure` function. All three return an `EnergyResult` whose `to_dict` emits
  the stable envelope. A refusal is a result, not an exception.
- A stable output envelope, `measurement-provenance/1`, with nine fixed top
  level keys and everything tool specific under `measurement`.
- Achieved sampling rate measured from `time.monotonic` timestamps on completed
  samples, reported alongside the requested rate, the interval count, the span,
  and the interval mean, standard deviation, minimum, maximum and relative
  standard deviation.
- Raw hardware read rate reported as a separate field, `channel_read_hz`, with
  `mean_reads_per_sample`, so a read count can never be mistaken for a sample
  count.
- An uncertainty budget in the structure of JCGM 100:2008 with a Type A sample
  scatter term, a Type B region boundary timing term and a Type B counter
  quantization term, combined in quadrature, reported at k = 1 and k = 2, with
  the excluded effects named in the record.
- A refusal rule: at least 30 complete samples for a confident figure, with a
  warning band to 100, the thresholds and the implied duration floor published
  in every record.
- RAPL backend over powercap sysfs, with rollover correction at
  `max_energy_range_uj`, detection of a rollover that cannot be disambiguated,
  per domain readability reporting, a package-only flag, and an explicit
  message about the root-only `energy_uj` attribute citing kernel commit
  949dd0104c49 and CVE-2020-8694.
- NVML backend using `nvmlDeviceGetTotalEnergyConsumption` where available and
  falling back to `nvmlDeviceGetPowerUsage` sampling, reporting which source
  was used, and reporting that NVML does not state the device figure's internal
  rail scope.
- Raspberry Pi 5 PMIC backend over `vcgencmd pmic_read_adc`, with the per rail
  read structure made explicit, voltage-only channels identified and excluded
  from the power sum, and the documented exclusion of USB current and the 5V
  supply recorded as a reason code.
- Android fuel gauge backend over `charge_counter` and `voltage_now`, with
  `dumpsys batterystats` read as context only, an always-on battery side
  caveat, and a refusal when the gauge produced fewer than three distinct
  counter values or when the device was charging.
- A clean `UNSUPPORTED` path that reports no joule figure at all.
- An opt-in model based estimate, off by default, labelled `ESTIMATE` as a
  field in the record and incapable of earning a `PASS` verdict.
- Optional idle baseline subtraction, either measured before the region or
  supplied by the caller, with whether it was subtracted and where it came from
  recorded, and an invalidating reason when the baseline is not less than the
  measurement.
- CLI with `detect`, `run`, `reasons` and `schema` subcommands, and verdict
  mapped exit codes.
- A composite GitHub Action at the repository root with nine outputs and a job
  summary table.
- 133 tests against synthetic fixtures: a fake powercap tree with an injectable
  counter reader, a fake NVML module, a recorded `vcgencmd pmic_read_adc`
  capture, and a fake power supply sysfs tree.

### Known limitations at 0.1.0

Agreement with real hardware has not been established; the suite exercises the
logic against fixtures. No calibration term, no attribution of energy to the
measured region, no AMD GPU backend, no Apple Silicon backend, no RAPL MSR or
perf PMU path. The full list is in the README.

[Unreleased]: https://github.com/manunicholasjacob/powerstamp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/manunicholasjacob/powerstamp/releases/tag/v0.1.0
