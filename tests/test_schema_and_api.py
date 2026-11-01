"""The stable envelope, the public API, the baseline, and the CLI."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from conftest import FakeConstantPowerBackend
from powerstamp import (
    EnergyResult,
    Powerstamp,
    __version__,
    available_backend_names,
    detect_backends,
    measure,
    powerstamp,
)
from powerstamp.schema import (
    ENVELOPE_TOP_LEVEL_KEYS,
    REASON_CODES,
    SCHEMA_ID,
    SEVERITIES,
    VERDICTS,
    Envelope,
    Reason,
    decide_verdict,
    reason,
)


def _passing_result():
    import time

    backend = FakeConstantPowerBackend(watts=10.0, rails=2)
    with Powerstamp(sample_hz=200, candidates=[backend], label="unit") as ps:
        time.sleep(1.0)
    assert ps.result is not None
    return ps.result


# ---------------------------------------------------------------------------
# Envelope shape
# ---------------------------------------------------------------------------


def test_envelope_top_level_keys_are_exactly_the_shared_set():
    result = _passing_result()
    env = result.to_dict()
    assert list(env.keys()) == list(ENVELOPE_TOP_LEVEL_KEYS)


def test_envelope_identifies_the_schema_and_the_tool():
    env = _passing_result().to_dict()
    assert env["schema"] == SCHEMA_ID == "measurement-provenance/1"
    assert env["tool"] == {"name": "powerstamp", "version": __version__}


def test_taken_at_utc_is_iso_8601_with_a_z_suffix():
    env = _passing_result().to_dict()
    stamp = env["taken_at_utc"]
    assert stamp.endswith("Z")
    assert "T" in stamp
    from datetime import datetime

    datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ")


def test_platform_block_has_the_four_required_fields():
    env = _passing_result().to_dict()
    assert sorted(env["platform"]) == ["machine", "python", "release", "system"]
    assert all(isinstance(v, str) for v in env["platform"].values())


def test_verdict_is_from_the_shared_vocabulary():
    env = _passing_result().to_dict()
    assert env["verdict"] in VERDICTS


def test_reasons_entries_have_code_severity_detail():
    env = _passing_result().to_dict()
    assert env["reasons"]
    for item in env["reasons"]:
        assert sorted(item) == ["code", "detail", "severity"]
        assert item["code"] in REASON_CODES
        assert item["severity"] in SEVERITIES
        assert item["detail"].strip()


def test_unreadable_is_a_list_of_strings():
    env = _passing_result().to_dict()
    assert isinstance(env["unreadable"], list)
    assert all(isinstance(x, str) for x in env["unreadable"])


def test_measurement_block_carries_the_required_fields():
    env = _passing_result().to_dict()
    block = env["measurement"]
    for key in (
        "kind",
        "is_measurement",
        "backend",
        "reduction",
        "joules",
        "watts_mean",
        "rate",
        "uncertainty",
        "refusal_rule",
        "per_domain_j",
        "baseline",
    ):
        assert key in block, key
    assert block["rate"]["achieved_hz"] > 0
    assert block["rate"]["sample_count"] > 0
    assert block["uncertainty"]["expanded_uncertainty_j"] > 0


def test_the_envelope_round_trips_through_json():
    env = _passing_result().to_dict()
    assert json.loads(json.dumps(env)) == env


def test_duration_is_a_float_at_the_top_level():
    env = _passing_result().to_dict()
    assert isinstance(env["duration_s"], float)
    assert env["duration_s"] > 0


# ---------------------------------------------------------------------------
# Vocabulary guards
# ---------------------------------------------------------------------------


def test_an_unregistered_reason_code_is_rejected():
    with pytest.raises(ValueError):
        Reason(code="MADE_UP_CODE", severity="info", detail="x")


def test_an_unknown_severity_is_rejected():
    with pytest.raises(ValueError):
        Reason(code="NO_BACKEND", severity="catastrophic", detail="x")


def test_an_unknown_verdict_is_rejected_at_serialisation():
    env = Envelope(tool_version="0.1.0", duration_s=1.0, verdict="GREAT")
    with pytest.raises(ValueError):
        env.to_dict()


def test_reason_defaults_its_detail_to_the_registry():
    r = reason("NO_BACKEND", "invalidating")
    assert r.detail == REASON_CODES["NO_BACKEND"]


def test_verdict_rule_table():
    ok = []
    bad = [reason("NO_SAMPLES", "invalidating")]
    warn = [reason("PACKAGE_ONLY_COVERAGE", "warning")]
    assert decide_verdict(ok, "MEASUREMENT", supported=True) == "PASS"
    assert decide_verdict(warn, "MEASUREMENT", supported=True) == "PASS"
    assert decide_verdict(bad, "MEASUREMENT", supported=True) == "INVALID"
    assert decide_verdict(ok, "MEASUREMENT", supported=False) == "UNSUPPORTED"
    assert decide_verdict(bad, "MEASUREMENT", supported=False) == "UNSUPPORTED"
    assert decide_verdict(ok, "ESTIMATE", supported=True) == "UNKNOWN"
    assert decide_verdict(ok, "NONE", supported=True) == "UNKNOWN"


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------


def test_context_manager_result_is_available_after_the_block():
    result = _passing_result()
    assert isinstance(result, EnergyResult)
    assert result.label == "unit"


def test_a_refusal_is_a_result_not_an_exception():
    with Powerstamp(sample_hz=0.5, candidates=[FakeConstantPowerBackend()]) as ps:
        pass
    assert ps.result is not None
    assert ps.result.verdict == "INVALID"


def test_the_block_still_raises_the_callers_exception():
    with pytest.raises(RuntimeError):
        with Powerstamp(sample_hz=100, candidates=[FakeConstantPowerBackend()]):
            raise RuntimeError("the caller's error must not be swallowed")


def test_measure_returns_the_value_and_the_result():
    import time

    def work(n):
        time.sleep(0.4)
        return n * 2

    value, result = measure(
        work, 21, sample_hz=200, candidates=[FakeConstantPowerBackend()]
    )
    assert value == 42
    assert result.label == "work"
    assert result.rate is not None


def test_decorator_preserves_the_return_value_and_records_results():
    import time

    @powerstamp(sample_hz=200, candidates=[FakeConstantPowerBackend()])
    def work(a, b=1):
        time.sleep(0.4)
        return a + b

    assert work(2, b=3) == 5
    assert work.last_powerstamp is not None
    assert work.last_powerstamp.label == "work"
    assert len(work.powerstamp_results) == 1
    assert work(1) == 2
    assert len(work.powerstamp_results) == 2
    assert work.__name__ == "work"


def test_decorator_on_result_callback_fires():
    seen = []

    @powerstamp(
        sample_hz=200,
        candidates=[FakeConstantPowerBackend()],
        on_result=seen.append,
    )
    def work():
        import time

        time.sleep(0.3)

    work()
    assert len(seen) == 1
    assert isinstance(seen[0], EnergyResult)


def test_bare_decorator_form_works():
    @powerstamp
    def work():
        return 7

    assert work() == 7
    assert work.last_powerstamp is not None


def test_sample_hz_must_be_positive():
    with pytest.raises(ValueError):
        Powerstamp(sample_hz=0)


def test_baseline_seconds_and_watts_are_mutually_exclusive():
    with pytest.raises(ValueError):
        Powerstamp(baseline_s=1.0, baseline_w=1.0)


def test_detect_backends_returns_every_real_backend():
    probed = detect_backends()
    names = [b.name for b, _a in probed]
    assert names == ["rapl", "nvml", "rpi_pmic", "android_fuel_gauge"]
    for _backend, avail in probed:
        assert isinstance(avail.detail, str) and avail.detail


def test_available_backend_names_includes_the_escape_hatches():
    names = available_backend_names()
    assert "unsupported" in names
    assert "model_estimate" in names


def test_summary_is_a_single_line():
    result = _passing_result()
    line = result.summary()
    assert "\n" not in line
    assert "PASS" in line
    assert "Hz achieved" in line


# ---------------------------------------------------------------------------
# Baseline subtraction
# ---------------------------------------------------------------------------


def test_baseline_not_subtracted_is_recorded():
    result = _passing_result()
    assert result.baseline_subtracted is False
    assert "BASELINE_NOT_SUBTRACTED" in result.reason_codes()
    assert result.to_dict()["measurement"]["baseline"]["subtracted"] is False


def test_a_provided_baseline_is_subtracted_and_recorded():
    import time

    backend = FakeConstantPowerBackend(watts=10.0)
    with Powerstamp(
        sample_hz=200, candidates=[backend], baseline_w=4.0, label="net"
    ) as ps:
        time.sleep(1.0)
    result = ps.result
    assert result is not None
    assert result.baseline_subtracted is True
    assert result.baseline_w == 4.0
    assert "BASELINE_SUBTRACTED" in result.reason_codes()
    # 10 W measured minus a 4 W baseline leaves about 6 J over a second.
    assert result.joules == pytest.approx(6.0, rel=0.25)
    assert result.joules_before_baseline == pytest.approx(10.0, rel=0.25)
    block = result.to_dict()["measurement"]["baseline"]
    assert block["subtracted"] is True
    assert block["source"] == "provided by the caller"


def test_a_measured_baseline_is_recorded_as_measured():
    import time

    backend = FakeConstantPowerBackend(watts=10.0)
    with Powerstamp(
        sample_hz=200, candidates=[backend], baseline_s=0.4, label="measured-base"
    ) as ps:
        time.sleep(1.0)
    result = ps.result
    assert result is not None
    assert result.baseline_subtracted is True
    assert result.baseline_w == pytest.approx(10.0, rel=0.3)
    assert "measured over" in result.baseline_source
    # Subtracting a baseline equal to the load leaves roughly nothing, which
    # is correct for this fixture.
    assert result.joules is not None


def test_a_baseline_larger_than_the_measurement_is_invalidating():
    import time

    backend = FakeConstantPowerBackend(watts=5.0)
    with Powerstamp(sample_hz=200, candidates=[backend], baseline_w=50.0) as ps:
        time.sleep(0.5)
    result = ps.result
    assert result is not None
    assert result.verdict == "INVALID"
    assert "BASELINE_EXCEEDS_MEASUREMENT" in result.reason_codes()
    # The unsubtracted figure is what gets reported, not a negative number.
    assert result.joules is not None
    assert result.joules > 0
    assert result.baseline_subtracted is False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(*args):
    """Run the CLI in a child process.

    The child gets ``src`` on PYTHONPATH so the suite passes whether or not
    the package has been installed into the environment.
    """
    import os

    import powerstamp

    src_dir = os.path.dirname(os.path.dirname(os.path.abspath(powerstamp.__file__)))
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src_dir + (os.pathsep + existing if existing else "")
    return subprocess.run(
        [sys.executable, "-m", "powerstamp.cli"] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def test_cli_detect_runs():
    proc = _cli("detect", "--json")
    assert proc.returncode in (0, 2)
    doc = json.loads(proc.stdout)
    assert [row["backend"] for row in doc["backends"]] == [
        "rapl",
        "nvml",
        "rpi_pmic",
        "android_fuel_gauge",
    ]


def test_cli_detect_human_output_explains_the_unsupported_path():
    proc = _cli("detect")
    assert proc.returncode in (0, 2)
    if proc.returncode == 2:
        assert "does not estimate energy from CPU utilization" in proc.stdout


def test_cli_schema_lists_the_envelope_and_the_reason_codes():
    proc = _cli("schema", "--json")
    assert proc.returncode == 0
    doc = json.loads(proc.stdout)
    assert doc["schema"] == SCHEMA_ID
    assert doc["top_level_keys"] == list(ENVELOPE_TOP_LEVEL_KEYS)
    assert doc["refusal_rule"]["min_samples"] == 30
    assert set(doc["reason_codes"]) == set(REASON_CODES)


def test_cli_reasons_lists_every_code():
    proc = _cli("reasons")
    assert proc.returncode == 0
    for code in REASON_CODES:
        assert code in proc.stdout


def test_cli_run_without_a_command_is_a_usage_error():
    proc = _cli("run")
    assert proc.returncode == 4
    assert "no command given" in proc.stderr


def test_cli_run_on_an_unsupported_machine_exits_two(tmp_path):
    out = tmp_path / "result.json"
    proc = _cli(
        "run",
        "--backend",
        "rapl",
        "--sample-hz",
        "50",
        "--output",
        str(out),
        "--quiet",
        "--",
        sys.executable,
        "-c",
        "pass",
    )
    # On this machine RAPL is absent, so the verdict is UNSUPPORTED. On a
    # Linux host with a readable counter the same command would measure.
    assert proc.returncode in (0, 1, 2)
    doc = json.loads(out.read_text())
    assert doc["schema"] == SCHEMA_ID
    assert doc["verdict"] in VERDICTS
    if doc["verdict"] == "UNSUPPORTED":
        assert doc["measurement"]["joules"] is None
        assert doc["unreadable"]


def test_cli_version():
    proc = _cli("--version")
    assert proc.returncode == 0
    assert __version__ in proc.stdout
