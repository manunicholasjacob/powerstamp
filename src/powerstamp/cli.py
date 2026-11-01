"""Command line interface.

::

    powerstamp detect
    powerstamp run --sample-hz 20 -- python bench.py
    powerstamp reasons
    powerstamp schema

Exit codes
----------
0  PASS
1  INVALID (the measurement happened and cannot support a confident figure)
2  UNSUPPORTED (no backend)
3  UNKNOWN (an ESTIMATE, or a result that is neither pass nor refusal)
4  usage error, or the measured command itself failed and --propagate was set

A non-zero exit on INVALID is the point. A CI job that treats a refusal as a
success is back to publishing numbers the measurement cannot support.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import List, Optional

from . import __version__
from .backends import detect_backends
from .measure import DEFAULT_SAMPLE_HZ, EnergyResult, Powerstamp
from .schema import (
    ENVELOPE_TOP_LEVEL_KEYS,
    KINDS,
    REASON_CODES,
    SCHEMA_ID,
    SEVERITIES,
    VERDICTS,
)
from .uncertainty import (
    COVERAGE_FACTOR,
    JITTER_RSD_WARN,
    MARGINAL_SAMPLES,
    MIN_SAMPLES,
    RATE_SHORTFALL_FRACTION,
)

EXIT_PASS = 0
EXIT_INVALID = 1
EXIT_UNSUPPORTED = 2
EXIT_UNKNOWN = 3
EXIT_USAGE = 4

_EXIT_FOR_VERDICT = {
    "PASS": EXIT_PASS,
    "INVALID": EXIT_INVALID,
    "UNSUPPORTED": EXIT_UNSUPPORTED,
    "UNKNOWN": EXIT_UNKNOWN,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="powerstamp",
        description=(
            "Measure the energy of a piece of work and report it with its own "
            "measurement uncertainty, achieved sampling rate and rail coverage."
        ),
    )
    parser.add_argument("--version", action="version", version="powerstamp %s" % __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_detect = sub.add_parser("detect", help="report which backends are available and why not")
    p_detect.add_argument("--json", action="store_true", help="emit JSON")

    p_run = sub.add_parser("run", help="measure a command")
    p_run.add_argument(
        "--sample-hz",
        type=float,
        default=DEFAULT_SAMPLE_HZ,
        help="requested sampling rate; the achieved rate is what gets reported "
        "(default: %(default)s)",
    )
    p_run.add_argument("--backend", default=None, help="force a backend by name")
    p_run.add_argument(
        "--baseline-seconds",
        type=float,
        default=None,
        help="measure idle power for this long before the run and subtract it",
    )
    p_run.add_argument(
        "--baseline-watts",
        type=float,
        default=None,
        help="subtract this idle power instead of measuring one",
    )
    p_run.add_argument(
        "--allow-estimate",
        action="store_true",
        help="fall back to a figure labelled ESTIMATE when no backend exists; "
        "an ESTIMATE never earns a PASS verdict",
    )
    p_run.add_argument("--label", default=None, help="label recorded in the output")
    p_run.add_argument(
        "--output", "-o", default=None, help="write the JSON envelope to this path"
    )
    p_run.add_argument("--quiet", action="store_true", help="print nothing but the exit code")
    p_run.add_argument(
        "--json", action="store_true", help="print the JSON envelope to stdout"
    )
    p_run.add_argument(
        "--propagate",
        action="store_true",
        help="exit with code 4 if the measured command itself failed",
    )
    p_run.add_argument(
        "--allow-invalid",
        action="store_true",
        help="exit 0 even when the verdict is INVALID; the verdict in the JSON is "
        "unchanged",
    )
    p_run.add_argument(
        "argv",
        nargs=argparse.REMAINDER,
        help="the command to measure, after a bare --",
    )

    sub.add_parser("reasons", help="list every reason code this tool can emit")
    p_schema = sub.add_parser("schema", help="describe the output envelope")
    p_schema.add_argument("--json", action="store_true", help="emit JSON")
    return parser


def _cmd_detect(args: argparse.Namespace) -> int:
    probed = detect_backends()
    rows = [
        {
            "backend": b.name,
            "available": a.available,
            "detail": a.detail,
            "detected_hardware": a.detected_hardware,
            "reduction": b.reduction_kind,
            "coverage": b.coverage,
        }
        for b, a in probed
    ]
    if args.json:
        print(json.dumps({"backends": rows}, indent=2))
    else:
        for row in rows:
            mark = "available" if row["available"] else "unavailable"
            print("%-20s %-12s %s" % (row["backend"], mark, row["detail"]))
        if not any(r["available"] for r in rows):
            print("")
            print(
                "No backend is available, so powerstamp would report UNSUPPORTED. It "
                "does not estimate energy from CPU utilization and present it as a "
                "measurement."
            )
    return EXIT_PASS if any(r["available"] for r in rows) else EXIT_UNSUPPORTED


def _cmd_reasons(args: argparse.Namespace) -> int:
    for code in sorted(REASON_CODES):
        print("%-32s %s" % (code, REASON_CODES[code]))
    return EXIT_PASS


def _cmd_schema(args: argparse.Namespace) -> int:
    doc = {
        "schema": SCHEMA_ID,
        "top_level_keys": list(ENVELOPE_TOP_LEVEL_KEYS),
        "verdicts": list(VERDICTS),
        "severities": list(SEVERITIES),
        "measurement_kinds": list(KINDS),
        "reason_codes": REASON_CODES,
        "refusal_rule": {
            "min_samples": MIN_SAMPLES,
            "marginal_samples": MARGINAL_SAMPLES,
            "rate_shortfall_fraction": RATE_SHORTFALL_FRACTION,
            "jitter_rsd_warn": JITTER_RSD_WARN,
            "coverage_factor": COVERAGE_FACTOR,
        },
    }
    if args.json:
        print(json.dumps(doc, indent=2))
    else:
        print("schema: %s" % SCHEMA_ID)
        print("top level keys: %s" % ", ".join(ENVELOPE_TOP_LEVEL_KEYS))
        print("verdicts: %s" % ", ".join(VERDICTS))
        print("measurement kinds: %s" % ", ".join(KINDS))
        print("minimum samples for a confident figure: %d" % MIN_SAMPLES)
        print("")
        print("reason codes:")
        for code in sorted(REASON_CODES):
            print("  %-32s %s" % (code, REASON_CODES[code]))
    return EXIT_PASS


def _strip_separator(argv: List[str]) -> List[str]:
    if argv and argv[0] == "--":
        return argv[1:]
    return argv


def _cmd_run(args: argparse.Namespace) -> int:
    command = _strip_separator(list(args.argv or []))
    if not command:
        print("powerstamp run: no command given; put it after a bare --", file=sys.stderr)
        return EXIT_USAGE
    if args.baseline_seconds is not None and args.baseline_watts is not None:
        print(
            "powerstamp run: pass --baseline-seconds or --baseline-watts, not both",
            file=sys.stderr,
        )
        return EXIT_USAGE

    child_rc = {"rc": 0}

    def body() -> None:
        proc = subprocess.run(command)
        child_rc["rc"] = proc.returncode

    stamp = Powerstamp(
        sample_hz=args.sample_hz,
        backend=args.backend,
        baseline_s=args.baseline_seconds,
        baseline_w=args.baseline_watts,
        allow_estimate=args.allow_estimate,
        label=args.label or " ".join(command),
        metadata={"command": command},
    )
    with stamp:
        body()

    result = stamp.result
    assert result is not None

    payload = result.to_json()
    if args.output:
        with open(args.output, "w") as fh:
            fh.write(payload)
            fh.write("\n")
    if args.json:
        print(payload)
    elif not args.quiet:
        _print_human(result)

    if args.propagate and child_rc["rc"] != 0:
        return EXIT_USAGE

    verdict = result.verdict
    if verdict == "INVALID" and args.allow_invalid:
        return EXIT_PASS
    return _EXIT_FOR_VERDICT.get(verdict, EXIT_UNKNOWN)


def _print_human(result: EnergyResult) -> None:
    print(result.summary())
    if result.rate is not None and result.channel_read_hz is not None:
        print(
            "  rate: %.4g Hz complete samples, %.4g Hz raw channel reads "
            "(%.3g reads per sample)"
            % (
                result.rate.achieved_hz,
                result.channel_read_hz,
                result.mean_reads_per_sample or 0.0,
            )
        )
    if result.per_domain_j:
        print("  domains and rails measured:")
        for key in sorted(result.per_domain_j):
            print("    %-32s %.6g J" % (key, result.per_domain_j[key]))
    if result.unreadable:
        print("  not readable (absent, not zero):")
        for item in result.unreadable:
            print("    %s" % item)
    if result.reasons:
        print("  reasons:")
        for r in result.reasons:
            print("    [%-12s] %s: %s" % (r.severity, r.code, r.detail))


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "detect":
        return _cmd_detect(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "reasons":
        return _cmd_reasons(args)
    if args.command == "schema":
        return _cmd_schema(args)
    parser.print_help()
    return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
