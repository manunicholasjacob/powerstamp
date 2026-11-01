"""The UNSUPPORTED path, and the opt-in ESTIMATE path.

The rule being tested: when no backend exists, powerstamp reports UNSUPPORTED
and no joule figure. It never derives energy from CPU utilization and presents
it as a measurement. If a caller opts into a model based figure, the record
itself says ESTIMATE and the verdict can never be PASS.
"""

from __future__ import annotations

from conftest import NeverAvailableBackend
from powerstamp import Powerstamp
from powerstamp.backends import select_backend
from powerstamp.backends.unsupported import ModelEstimateBackend, UnsupportedBackend
from powerstamp.schema import KIND_ESTIMATE, KIND_NONE


# ---------------------------------------------------------------------------
# No backend
# ---------------------------------------------------------------------------


def test_select_backend_with_nothing_available_returns_unsupported():
    backend, avail, rejected = select_backend(
        candidates=[NeverAvailableBackend("no powercap"), NeverAvailableBackend("no NVML")]
    )
    assert isinstance(backend, UnsupportedBackend)
    assert avail.available is False
    assert len(rejected) == 2
    # The detail says what was tried, not just that it failed.
    assert "no powercap" in avail.detail
    assert "no NVML" in avail.detail


def test_unsupported_result_has_no_joule_figure():
    with Powerstamp(
        sample_hz=50, candidates=[NeverAvailableBackend()], label="nothing"
    ) as ps:
        pass
    result = ps.result
    assert result is not None
    assert result.verdict == "UNSUPPORTED"
    assert result.joules is None
    assert result.watts_mean is None
    assert result.kind == KIND_NONE
    assert result.is_measurement is False

    envelope = result.to_dict()
    assert envelope["verdict"] == "UNSUPPORTED"
    assert envelope["measurement"]["joules"] is None
    assert envelope["unreadable"] == ["all energy domains (no backend)"]


def test_unsupported_explains_why_it_does_not_estimate():
    backend = UnsupportedBackend()
    red = backend.reduce([])
    assert "not a measurement" in red.extra["why_no_estimate"]
    codes = [c for c, _s, _d in red.reasons]
    assert codes == ["NO_BACKEND"]


def test_requesting_a_missing_backend_by_name_is_unsupported():
    backend, avail, _rejected = select_backend(
        prefer="never_available", candidates=[NeverAvailableBackend("not here")]
    )
    assert isinstance(backend, UnsupportedBackend)
    assert avail.available is False


def test_requesting_a_nonexistent_backend_name_is_unsupported():
    backend, avail, _rejected = select_backend(prefer="no_such_backend", candidates=[])
    assert isinstance(backend, UnsupportedBackend)
    assert "no backend named" in avail.detail


# ---------------------------------------------------------------------------
# The ESTIMATE path is opt in and labelled in the data
# ---------------------------------------------------------------------------


def test_estimate_is_off_by_default():
    backend, avail, _rejected = select_backend(
        candidates=[NeverAvailableBackend()], allow_estimate=False
    )
    assert isinstance(backend, UnsupportedBackend)


def test_estimate_is_selected_only_when_asked_for():
    backend, avail, _rejected = select_backend(
        candidates=[NeverAvailableBackend()], allow_estimate=True
    )
    assert isinstance(backend, ModelEstimateBackend)
    assert avail.available is True
    assert "not a measurement" in avail.detail


def test_estimate_is_labelled_in_the_data_structure():
    with Powerstamp(
        sample_hz=100,
        candidates=[NeverAvailableBackend()],
        allow_estimate=True,
        label="estimated",
    ) as ps:
        total = 0
        for i in range(200000):
            total += i

    result = ps.result
    assert result is not None
    assert result.kind == KIND_ESTIMATE
    assert result.is_measurement is False

    envelope = result.to_dict()
    block = envelope["measurement"]
    # The flag is a field, not prose.
    assert block["kind"] == "ESTIMATE"
    assert block["is_measurement"] is False
    assert block["assumption_has_no_hardware_basis"] is True
    assert "ESTIMATE_NOT_MEASUREMENT" in [r["code"] for r in envelope["reasons"]]


def test_an_estimate_never_earns_a_pass_verdict():
    from powerstamp.schema import decide_verdict

    # No invalidating reasons at all, and it still is not a PASS.
    assert decide_verdict([], KIND_ESTIMATE, supported=True) == "UNKNOWN"


def test_estimate_verdict_through_the_engine_is_never_pass():
    with Powerstamp(
        sample_hz=200,
        candidates=[NeverAvailableBackend()],
        allow_estimate=True,
    ) as ps:
        total = 0
        for i in range(500000):
            total += i
    assert ps.result is not None
    assert ps.result.verdict in ("UNKNOWN", "INVALID")
    assert ps.result.verdict != "PASS"


def test_a_named_unavailable_backend_reports_backend_unavailable():
    """The platform being wrong and the request being wrong are different."""
    from powerstamp import Powerstamp

    with Powerstamp(
        sample_hz=50, backend="never_available", candidates=[NeverAvailableBackend()]
    ) as ps:
        pass
    assert ps.result is not None
    assert ps.result.verdict == "UNSUPPORTED"
    assert "BACKEND_UNAVAILABLE" in ps.result.reason_codes()
    assert "NO_BACKEND" not in ps.result.reason_codes()


def test_nothing_available_reports_no_backend_not_backend_unavailable():
    from powerstamp import Powerstamp

    with Powerstamp(sample_hz=50, candidates=[NeverAvailableBackend()]) as ps:
        pass
    assert ps.result is not None
    assert "NO_BACKEND" in ps.result.reason_codes()
    assert "BACKEND_UNAVAILABLE" not in ps.result.reason_codes()
