# powerstamp

[![PyPI](https://img.shields.io/pypi/v/powerstamp)](https://pypi.org/project/powerstamp/)
[![CI](https://github.com/manunicholasjacob/powerstamp/actions/workflows/ci.yml/badge.svg)](https://github.com/manunicholasjacob/powerstamp/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/powerstamp)](https://pypi.org/project/powerstamp/)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Energy measurement that reports its own measurement uncertainty.

`powerstamp` measures the energy consumed by a piece of work and reports the
joule figure together with the sampling rate it actually achieved, the rails and
domains it actually covered, and a quantified uncertainty. When the measurement
cannot support a confident number, it says so instead of printing one.

The field this serves reports "joules per token" and "energy per inference"
constantly, and almost never states the sampling rate, the rails covered, or an
error bar. Numbers reported that way are not comparable between papers. The
first class output of this tool is therefore not the joule figure. It is the
joule figure with its rate, its rails and its uncertainty attached.

Status: version 0.1.0, released on PyPI and tagged `v0.1.0`. No backend has yet
been validated against real hardware; see Limitations.

## Why this exists

Two findings from the author's own prior work set the shape of the tool.

The first is a measurement error that was caught and corrected. In the author's
Raspberry Pi 5 energy work, the PMIC's effective sampling rate turned out to be
21.2 Hz, not the 43 to 44 Hz that a naive read count suggested, because the
apparent figure was twice a per rail read count: `vcgencmd pmic_read_adc` returns
a voltage channel and a current channel for each rail, and counting both as
samples doubles the apparent rate. The paper's conclusions survived with better
than a six times margin, but the rate was wrong in an earlier draft and had to be
corrected. A reported rate that is an artefact of how the reads were counted is
exactly the class of error this tool exists to prevent, so `powerstamp` measures
the rate from timestamps on completed samples and reports the raw channel read
rate as a separate, separately named field.

The second is why the rate matters at all. That line of work found that the
latency-optimal configuration is not the energy-optimal one. That is only a
meaningful claim if the energy measurement is trustworthy, which means the rate,
the rails and the error bar have to travel with the number.

## Install

```sh
pip install powerstamp
pip install "powerstamp[nvml]"   # adds nvidia-ml-py for the NVIDIA backend
```

Or from a checkout, for development:

```sh
pip install -e ".[test]"
```

Pure standard library at runtime. The NVIDIA backend is the only optional
dependency, and its absence is reported as an unavailable backend rather than an
import error.

## Usage

### Context manager

```python
from powerstamp import Powerstamp

with Powerstamp(sample_hz=20, label="decode") as ps:
    decode(frames)

print(ps.result.summary())
print(ps.result.to_json())
```

A refusal is a result, not an exception. `ps.result` is always present, and its
`verdict` is `PASS`, `INVALID`, `UNSUPPORTED` or `UNKNOWN`.

### Decorator

```python
from powerstamp import powerstamp

@powerstamp(sample_hz=20, baseline_s=2.0)
def decode(frames):
    ...

decode(frames)
print(decode.last_powerstamp.summary())
for r in decode.powerstamp_results:
    print(r.joules, r.verdict)
```

The wrapped function returns its own value unchanged, so the decorator never
changes a call signature.

### Function

```python
from powerstamp import measure

value, result = measure(decode, frames, sample_hz=20, baseline_w=3.1)
```

### CLI

```sh
powerstamp detect                       # which backends are available, and why not
powerstamp run --sample-hz 20 -- python bench.py
powerstamp run --sample-hz 20 --baseline-seconds 2 -o result.json -- ./bench
powerstamp reasons                      # every reason code the tool can emit
powerstamp schema                       # the output envelope
```

Exit codes: `0` PASS, `1` INVALID, `2` UNSUPPORTED, `3` UNKNOWN, `4` usage error
or, with `--propagate`, a failure of the measured command itself. A non-zero exit
on INVALID is deliberate. A CI job that treats a refusal as a success is back to
publishing numbers the measurement cannot support. `--allow-invalid` exits 0
anyway; it does not change the verdict in the JSON.

Real output from a machine with no energy interface at all:

```
$ powerstamp detect
rapl                 unavailable  no powercap sysfs at /sys/class/powercap
nvml                 unavailable  pynvml is not importable
rpi_pmic             unavailable  vcgencmd is not on PATH
android_fuel_gauge   unavailable  no power supply node under /sys/class/power_supply exposes both charge_counter and voltage_now

No backend is available, so powerstamp would report UNSUPPORTED. It does not
estimate energy from CPU utilization and present it as a measurement.
```

### GitHub Action

`action.yml` sits at the repository root, so the Action is usable directly:

```yaml
- uses: manunicholasjacob/powerstamp@v0.1.0
  with:
    run: python bench.py
    sample-hz: '20'
    baseline-seconds: '2'
    output: powerstamp.json
```

Outputs: `joules`, `watts-mean`, `achieved-hz`, `sample-count`,
`expanded-uncertainty-j`, `verdict`, `kind`, `backend`, `json-path`.

GitHub hosted runners do not expose a readable RAPL counter, so on a hosted
runner the Action will report `UNSUPPORTED` and, by default, not fail the job.
It is useful as it stands on a self-hosted runner with a readable energy
interface, and on a hosted runner it is an honest `UNSUPPORTED` record rather
than a fabricated number. `fail-on` controls that: see `action.yml`.

## Output schema

The outer envelope is a shared provenance record. Everything specific to energy
measurement lives under `measurement`.

```json
{
  "schema": "measurement-provenance/1",
  "tool": {"name": "powerstamp", "version": "0.1.0"},
  "taken_at_utc": "2026-11-01T00:00:00.000000Z",
  "duration_s": 2.9716981132075473,
  "platform": {"system": "", "release": "", "machine": "", "python": ""},
  "verdict": "PASS",
  "reasons": [{"code": "SAMPLE_COUNT_MARGINAL", "severity": "warning", "detail": "..."}],
  "unreadable": ["pmic:EXT5V (voltage only, no current channel)"],
  "measurement": { "...": "..." }
}
```

Top level keys, all nine always present:

| Key | Meaning |
| --- | --- |
| `schema` | `measurement-provenance/1` |
| `tool` | `{name, version}` |
| `taken_at_utc` | ISO 8601 UTC with a `Z` suffix |
| `duration_s` | wall clock duration of the measured region |
| `platform` | `system`, `release`, `machine`, `python` |
| `verdict` | `PASS`, `INVALID`, `UNSUPPORTED` or `UNKNOWN` |
| `reasons` | list of `{code, severity, detail}`; severity is `invalidating`, `warning` or `info` |
| `unreadable` | every domain or rail that could not be read, so a consumer sees an absence rather than a zero |
| `measurement` | the energy payload |

The `unreadable` list is the one that matters most here. A package-only RAPL
reading must not be presentable as a system reading, and this is where that is
recorded.

Inside `measurement`:

| Field | Meaning |
| --- | --- |
| `kind` | `MEASUREMENT`, `ESTIMATE` or `NONE`. The measurement-versus-estimate flag, as a field |
| `is_measurement` | boolean form of the same flag, for consumers that prefer a boolean |
| `backend`, `backend_detail`, `backend_coverage` | which backend ran, what it found, and what it physically covers |
| `reduction` | `counter` (a hardware energy counter delta) or `sampler` (an integral of sampled power) |
| `joules`, `watts_mean` | the figure, and the figure divided by the duration. `null` when UNSUPPORTED |
| `rate` | `requested_hz`, `achieved_hz`, `sample_count`, `interval_count`, `span_s`, and the interval mean, standard deviation, minimum, maximum and relative standard deviation |
| `channel_read_hz`, `mean_reads_per_sample` | the raw hardware read rate, kept strictly separate from the sample rate |
| `uncertainty` | `standard_uncertainty_j`, `expanded_uncertainty_j`, `coverage_factor`, `relative_expanded`, the three `components_j`, an `excludes` list and free-text `notes` |
| `refusal_rule` | the thresholds in force, and the duration floor they imply at the achieved rate |
| `per_domain_j` | joules per domain or per rail |
| `baseline` | `subtracted`, `baseline_w`, `source`, `joules_before_baseline` |
| `backends_rejected` | every backend that was tried and why it could not run |

`docs/example_envelope.json` holds a complete record. It was produced by
replaying the recorded Raspberry Pi 5 `pmic_read_adc` capture in
`tests/fixtures/` through the real code path at a 21.2 Hz sample timeline, so the
structure and the arithmetic are real; the Type A component is zero in it because
a replayed constant capture has no scatter, and the rail currents are a single
snapshot rather than a live run.

### `reasons[].code` values

Sample sufficiency and timing:

| Code | Severity | Meaning |
| --- | --- | --- |
| `NO_SAMPLES` | invalidating | Fewer than two samples, so no interval exists |
| `SAMPLE_COUNT_TOO_LOW` | invalidating | Fewer complete samples than the documented minimum |
| `SAMPLE_COUNT_MARGINAL` | warning | Over the minimum but under the marginal band, so the error bar is itself loose |
| `RATE_SHORTFALL` | warning | The achieved rate fell below 80 percent of the requested rate |
| `SAMPLER_JITTER_HIGH` | warning | Interval relative standard deviation above 0.25 |

Counters:

| Code | Severity | Meaning |
| --- | --- | --- |
| `COUNTER_WRAPAROUND` | info | A counter rolled over and the rollover was corrected |
| `COUNTER_WRAPAROUND_AMBIGUOUS` | invalidating | A single interval could have held more than one rollover, so the total is not recoverable |
| `ENERGY_FROM_HARDWARE_COUNTER` | info | The figure is a counter delta |
| `ENERGY_FROM_POWER_INTEGRAL` | info | The figure is an integral of sampled power, which is a different measurement |

Coverage:

| Code | Severity | Meaning |
| --- | --- | --- |
| `PARTIAL_DOMAIN_COVERAGE` | warning | One or more domains or rails could not be read |
| `PACKAGE_ONLY_COVERAGE` | warning | Only package domains were readable, so this is not a system figure |
| `PERMISSION_DENIED` | warning | A counter file exists but is not readable with the current privileges |
| `NO_READABLE_DOMAINS` | invalidating | The backend ran but no domain or rail produced a reading |
| `PMIC_VOLTAGE_ONLY_CHANNELS` | info | Some PMIC channels have no matching current channel and contribute no power term |
| `PMIC_EXCLUDES_EXTERNAL_5V` | warning | PMIC rail readings exclude USB current and anything else on the 5V supply |
| `BATTERY_SIDE_READING` | warning | A battery side reading includes the display, the modem and everything else |
| `GPU_BOARD_SCOPE_UNSTATED` | warning | The vendor API does not state which internal rails the device figure covers |

Baseline and support:

| Code | Severity | Meaning |
| --- | --- | --- |
| `BASELINE_SUBTRACTED` | info | An idle baseline was subtracted |
| `BASELINE_NOT_SUBTRACTED` | info | No baseline was subtracted, so idle draw is included |
| `BASELINE_EXCEEDS_MEASUREMENT` | invalidating | The baseline was not less than the measurement, so the net would be negative |
| `NO_BACKEND` | invalidating | No backend is available on this platform |
| `BACKEND_UNAVAILABLE` | invalidating | A backend was requested by name and is not available here |
| `ESTIMATE_NOT_MEASUREMENT` | warning | The figure is a model based estimate and can never earn PASS |

The verdict rule, in order: no backend gives `UNSUPPORTED`; any invalidating
reason gives `INVALID`; an `ESTIMATE` caps at `UNKNOWN`; otherwise `PASS`.

## Backend matrix

What each backend does and, just as importantly, does not cover.

| Backend | Source | Reduction | Covers | Does not cover | Notes |
| --- | --- | --- | --- | --- | --- |
| `rapl` | `/sys/class/powercap/intel-rapl*/energy_uj` and subzones, plus `amd-rapl*` | counter | whichever of package, core, uncore, dram, psys the kernel exposes and the process can read | discrete GPU, storage, network interface, display, power supply conversion loss | Root only since kernel commit `949dd0104c49`. Rollover handled at `max_energy_range_uj`. Which domains were readable is reported; a package-only reading is flagged as such |
| `nvml` | `nvmlDeviceGetTotalEnergyConsumption`, falling back to `nvmlDeviceGetPowerUsage` | counter, or sampler on fallback | the NVIDIA device as NVML reports it | host CPU, host memory, anything off the card | Which of the two sources was used is reported, because the integral of sampled power is not the same measurement as a hardware energy counter. The counter is documented on Volta or newer. NVML states the figure is for "this GPU" without naming the internal rails, so the scope is reported as unstated rather than asserted to be whole board |
| `rpi_pmic` | `vcgencmd pmic_read_adc` | sampler | the PMIC supply rails that expose both a current and a voltage channel | USB current and anything else drawing directly on the 5V supply, power supply conversion loss, voltage-only channels | Per rail read structure made explicit: complete sweeps and raw channel reads are two separate reported rates. Pi 5 only |
| `android_fuel_gauge` | `/sys/class/power_supply/*/charge_counter` and `voltage_now`; `dumpsys batterystats` read as context | counter | the whole device at the battery terminals | nothing is excluded, which is the problem: the display, the modem and the radios are all included | Always emits `BATTERY_SIDE_READING` at warning severity. No option removes it. Refuses when the gauge took fewer than three distinct counter values, and when the counter rose, which means the device was charging |
| everything else | none | n/a | nothing | everything | Reports `UNSUPPORTED`. Does not estimate energy from CPU utilization and present it as a measurement |
| `model_estimate` | process CPU time times an assumed watts per busy core | sampler | nothing physical | everything | Off unless `allow_estimate` is passed. Sets `measurement.kind` to `ESTIMATE` in the record itself and can never reach `PASS`. The assumed watts figure has no hardware basis and the record says so |

### On the RAPL permission restriction

`energy_uj` is not world readable on a current kernel. Commit
`949dd0104c49`, "powercap: restrict energy meter to root access" (Len Brown,
10 November 2020, present in `v5.10-rc4` and backported to stable), changed the
attribute mode from `S_IWUSR | S_IRUGO` to `S_IWUSR | S_IRUSR` in response to
the PLATYPUS software power side channel, tracked as CVE-2020-8694 for the Intel
and kernel side, CVE-2020-8695 for the related Intel issue, and CVE-2020-12912
for the AMD counterpart. An unprivileged process therefore typically sees the
files and cannot read them, and `powerstamp` says exactly that and cites the
commit. The kernel powercap sysfs documentation still describes the attribute as
readable and has not been updated to match the code, so do not cite the
documentation for the permission.

## The uncertainty budget

The budget follows the structure of JCGM 100:2008, the GUM: one Type A component
evaluated from observed scatter, and Type B components evaluated from bounds on
effects the samples cannot show. A rectangular Type B bound of half width `a`
contributes `a / sqrt(3)`. The components combine in quadrature and are reported
at `k = 1` and at `k = 2`.

Type A, sample scatter. The standard error of the mean power is `s / sqrt(n)`,
and energy is mean power times duration, so the term on the energy is
`duration * s / sqrt(n)`. With fewer than two samples there is no scatter to
evaluate and the term is zero, which is an absent uncertainty and not a small
one. The record says so in `uncertainty.notes`.

Type B, region boundary. The start and the end of the region each fall somewhere
inside one sampling interval, so each boundary carries a rectangular error of
half width `P * dt / 2` and contributes `P * dt / (2 * sqrt(3))`. Two independent
boundaries combine to `P * dt * sqrt(2) / (2 * sqrt(3))`. This term is the direct
cost of a low sampling rate, and it is why the achieved rate belongs next to the
joule figure.

Type B, quantization. One read of an integer counter carries a rectangular error
of half width `q / 2` and contributes `q / sqrt(12)`; two reads give
`q * sqrt(2) / sqrt(12)`. `q` is 1 uJ for powercap, 1 mJ for the NVML counter,
one observed charge step at a nominal 3.85 V for the Android fuel gauge, and
zero for the Pi PMIC because Raspberry Pi does not publish the ADC resolution and
the eight printed decimal places are print precision rather than resolution.

What the budget deliberately excludes, named in the record under
`uncertainty.excludes` because omitting it silently would be the same failure the
tool exists to prevent:

- the absolute accuracy of the underlying counter or ADC, which no vendor in the
  matrix publishes for its energy path;
- any error from the measured region not being the only load on the machine;
- for a battery side reading, the share of the energy belonging to the display,
  the modem or the radios.

So it is a repeatability and discretisation budget, not a calibration budget.

## The refusal rule

A confident figure needs at least **30 complete samples**. Below that the
verdict is `INVALID` and the reason is `SAMPLE_COUNT_TOO_LOW`. Between 30 and
100 the figure is reported with `SAMPLE_COUNT_MARGINAL` at warning severity.

How 30 was chosen, both parts checkable:

1. The Type A term uses a sample standard deviation, and the relative standard
   error of a standard deviation is approximately `1 / sqrt(2 * (n - 1))`. That
   is about 13 percent at `n = 30`, about 7 percent at `n = 100`, and about 24
   percent at `n = 10`. At 10 samples the error bar is less trustworthy than the
   number it is supposed to qualify.
2. It forces the duration floor into the open. The floor is
   `min_samples / achieved_hz`, so at the 21.2 Hz measured on the Raspberry Pi 5
   the shortest run that can earn a confident verdict is about 1.42 s. A tool
   that accepts a 50 ms run on that hardware is reporting one or two samples and
   an error bar it invented.

The thresholds are published in every record under `measurement.refusal_rule`,
including the duration floor they imply at the rate actually achieved, so a
reader can check the rule against the run rather than take it on trust.

A sample means one complete read of every channel the backend covers, never one
hardware read. On the Pi PMIC a single sweep is 26 channel reads, and both rates
are reported separately: `rate.achieved_hz` for sweeps and `channel_read_hz` for
raw reads. Each powered rail contributes two channel reads, a voltage and a
current, so counting per rail reads instead of sweeps reports twice the real
rate, which is the specific error that put 43 to 44 Hz in an earlier draft of the
Raspberry Pi 5 work where the effective rate was 21.2 Hz.

## Related work

Each of the following was checked against its own source or documentation before
being described here. The four properties compared are: **A** reports the rate
actually achieved rather than the configured one, **B** reports explicit rail or
domain coverage, **C** reports a quantified uncertainty on the energy number,
**D** refuses to emit a confident number the measurement cannot support.

| Tool | A achieved rate | B rail coverage | C uncertainty | D refusal |
| --- | --- | --- | --- | --- |
| pyRAPL | no | no | no | no |
| pyJoules | no | no | no | partial |
| CodeCarbon | no | partial | no | no |
| scaphandre | no | no | no | no |
| powerstat | no | yes | partial | yes |
| turbostat | no | partial | no | no |
| `perf stat -e power/energy-pkg/` | not applicable | no | partial | no |
| Zeus | no | partial | no | no |
| nvidia-smi and NVML | not applicable | no | no | no |

Notes, tool by tool:

- **pyRAPL** selects devices but does not report which were readable, and
  discloses one qualitative caveat with no number.
- **pyJoules** is the only Python library with a partial D: it returns null
  values when the measurement period is too short. It is incomplete, because on
  counter overflow it adds `max_energy_range_uj` to a negative delta without
  refusing, and its own documentation notes the result is wrong if a counter
  overflows twice in one measurement. It states that devices have a
  hardware-dependent measurement frequency without stating that frequency for any
  device.
- **CodeCarbon** has the strongest B among the Python tools: it logs the chosen
  RAPL domain at INFO level. That is a log line, not a field in the emitted
  record. Its D is the opposite of a refusal: when RAPL is unavailable it falls
  back on constant power models for unsupported hardware and emits a
  confident-looking number.
- **scaphandre** reads package, per-socket and sometimes DRAM consumption without
  reporting which domains it managed to read, samples on demand with no fixed or
  reported rate, and derives per-process figures from CPU-time ratios.
- **powerstat** (Canonical) is the closest of the nine and the best prior art for
  B and D. It prints the RAPL domains it found, prints a per-domain column, states
  that the readings do not cover all the hardware in the device, and names its
  failure paths including the root-permission one. It refuses: it exits on
  "Number of readings too low", enforces a minimum count, marks an inaccurate
  summary value as not available and suffixes an inaccurate value, and reports
  skipped samples. Its C is partial: it gives the mean, standard deviation,
  minimum, maximum and geometric mean of the power samples, which is the
  dispersion of the samples rather than a propagated uncertainty on the energy
  total. It has no A at all: it prints the configured interval and divides by
  that nominal value, using elapsed wall time only for scheduling.
- **turbostat** names its domains per column, which is better than one opaque
  number, and exposes optional `usec` and `Time_Of_Day_Seconds` columns from
  which a caller could derive an interval. It still never reports an achieved
  rate, and `--force` runs on unsupported hardware.
- **`perf stat -e power/energy-pkg/`** reads a counter at the start and the end,
  so it has no sampling rate to achieve. `--repeat` gives a run-to-run standard
  deviation across repetitions, which is not an uncertainty on a single energy
  reading.
- **Zeus** is the most sophisticated of the nine and the most instructive near
  miss. Its `end_window` detects exactly the condition in D, warning that the
  measurement window was shorter than the GPU's energy counter update period, and
  then substitutes an approximation whose own docstring concedes it is still an
  approximation. Its coverage reporting is device-level, signalled by nullable
  fields, rather than rail-level. Its polling loop records neither the achieved
  rate nor the sample count.
- **nvidia-smi and NVML** expose a cumulative counter with no rail breakdown and
  no published uncertainty on the energy call. `NVML_ERROR_NOT_SUPPORTED` is
  capability detection, not a reliability refusal. The only published error bound
  anywhere in this survey is NVML's plus or minus 5 percent for
  `nvmlDeviceGetPowerUsage` on Fermi and Kepler, which is a power API on two old
  architectures and does not transfer to the energy counter.

**None of the nine does all four.** `powerstat` does B and D and a partial C, and
is the closest; nothing in the set reports an achieved sampling rate, and nothing
reports a quantified uncertainty on the energy number. So what `powerstamp` adds
is the combination: a measured rather than assumed sampling rate, explicit rail
and domain coverage in the emitted record rather than in a log line, an
uncertainty budget with its components and its exclusions named, and a refusal
that produces no confident number instead of an approximation.

Where `powerstamp` is behind: `powerstat` has far more mature RAPL handling and
a histogram view; `turbostat` reports frequency, idle residency and far more
besides; CodeCarbon does carbon accounting, which this tool does not attempt;
Zeus integrates with training loops and supports AMD GPUs, which this tool does
not. None of those is a reason to use this tool instead of those; the reason to
use this one is the provenance of the number.

A note on history: a prior pull request by this author to Zeus was closed. Zeus
is cited here as related work and nothing more.

## Related tools

`powerstamp` is one of three tools that emit the same `measurement-provenance/1`
envelope, so a consumer can read the provenance fields of any of them without
knowing which produced the record. The other two are `benchguard`, which decides
whether a measurement was valid from cgroup throttling, thermal counters, the
governor, major faults and competing load, and `membw`, which measures achieved
memory bandwidth against theoretical peak per core cluster. `benchguard` owns the
verdict vocabulary for the shared envelope. Neither is published, and this
section makes no claim that either ever will be.

## Limitations

Stated plainly, because a tool about measurement honesty cannot be coy about its
own gaps.

- **No calibration.** The uncertainty budget is a repeatability and
  discretisation budget. It does not include the absolute accuracy of any
  counter or ADC, because no vendor in the matrix publishes one for its energy
  path. A systematic error in the hardware passes straight through.
- **No attribution.** The energy of the measured region is the energy the machine
  drew during the region. Nothing isolates the caller's work from anything else
  running. The baseline subtraction helps with steady idle draw and nothing else,
  and whether it was applied is recorded.
- **Counter update periods are mostly unpublished.** Neither NVIDIA for the NVML
  energy counter nor Raspberry Pi for the PMIC ADC publishes an update period, so
  the region boundary term is bounded by the sampling interval rather than by the
  hardware's own refresh. Where the hardware is slower than the sampler, the real
  boundary uncertainty is larger than reported.
- **The Pi PMIC quantization term is missing, not zero.** It is reported as zero
  with a note saying why, rather than inferred from the printed decimal places.
- **The Android path is battery side and that cannot be fixed by software here.**
  It includes the display, the modem and the radios. Control for them or do not
  use the number.
- **The 30 sample threshold is a judgement.** The rationale is in this README and
  in `powerstamp/uncertainty.py`, the thresholds are published in every record,
  and a different field may want a different number. It is defended, not derived
  from first principles.
- **No RAPL MSR path and no `perf` PMU path.** Only powercap sysfs, so a system
  where powercap is absent but the MSR is readable reports `UNSUPPORTED`.
- **AMD GPUs are not supported.** There is no `amd-smi` or ROCm backend.
- **Apple Silicon is not supported.** There is no `powermetrics` or IOReport
  backend.
- **The sampler is a Python thread.** At high requested rates the achieved rate
  will fall short, which is reported as `RATE_SHORTFALL` rather than hidden.
- **The test suite runs against synthetic fixtures.** The RAPL, NVML, PMIC and
  Android paths are exercised through injected readers, a fake NVML module and a
  recorded `vcgencmd` capture. The logic is tested. Agreement with real hardware
  on a real machine is not yet established, and no claim is made that it is.

## Testing

```sh
pip install -e ".[test]"
python -m pytest -v
```

135 tests, all passing on Windows with Python 3.14. They cover counter rollover
including a rollover mid measurement and a rollover that cannot be
disambiguated, partial domain availability with the unreadable domains reported
as absent rather than zero, the root-only `energy_uj` path, runs too short for
the achieved rate, the `UNSUPPORTED` path, the opt-in `ESTIMATE` path and its
inability to reach `PASS`, the separation of sweep rate from channel read rate,
every arm of the uncertainty budget against its documented formula, baseline
subtraction including a baseline larger than the measurement, the envelope shape,
and the CLI.

## License

MIT. Copyright (c) 2026 Manu Nicholas Jacob.

## Citation

See `CITATION.cff`.

## Author

Manu Nicholas Jacob, Independent Researcher.
ORCID [0009-0007-6589-6572](https://orcid.org/0009-0007-6589-6572).
GitHub [@manunicholasjacob](https://github.com/manunicholasjacob).
