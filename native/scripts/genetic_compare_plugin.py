"""pytest plugin: run every GENETIC optimization of a test session in ``compare`` mode.

Each ``GeneticOptimization.optimize_ems`` call runs Python (the result the test sees, so the
tests behave as usual) and the native kernel, and records the comparison. At the end the
plugin writes all reports to ``$GENETIC_COMPARE_OUT`` (JSON) and prints a summary.

    PYTHONPATH=native/scripts pytest -p genetic_compare_plugin tests/test_genetic*.py

``EOS_NATIVE_GENETIC_UNCERTIFIED=1`` is set, so the kernel also runs on sources that are not
certified yet - that is the question this run answers.

``GENETIC_COMPARE_DOT=sequential`` replaces the BLAS dot product of the self-consumption
interpolator with a sequential sum, so the kernel can reproduce Python on any CPU (needed on
x86-64, whose OpenBLAS kernels the kernel does not reproduce). Without it, compare uses the
profile it detects and only CPUs with a known kernel (e.g. Neoverse N1, Cortex-A53/A55) give
bit-identical results. Tests that replace parts of the Python
optimizer (``patch.object(opt, "optimize", ...)``, broken worker pools) differ by design; they
are listed under ``patched``.
"""

from __future__ import annotations

import json
import os
from typing import Any

REPORTS: list[dict[str, Any]] = []


def pytest_configure(config: Any) -> None:
    os.environ["EOS_NATIVE_GENETIC_UNCERTIFIED"] = "1"
    from unittest import mock

    from akkudoktoreos.optimization.genetic import genetic as genetic_module
    from akkudoktoreos.optimization.genetic import native
    from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization

    native.reset_status_cache()
    if os.environ.get("GENETIC_COMPARE_DOT", "") == "sequential":
        from akkudoktoreos.prediction.interpolator import (
            SelfConsumptionProbabilityInterpolator,
        )

        SelfConsumptionProbabilityInterpolator._expected_direct_consumption = (  # type: ignore[method-assign]
            native.expected_direct_consumption_release_profile
        )
    original = GeneticOptimization.optimize_ems
    pristine = {
        "optimize": GeneticOptimization.optimize,
        "_evaluation_worker": genetic_module._evaluation_worker,
        "_evaluation_worker_init": genetic_module._evaluation_worker_init,
    }

    def patched(self: Any) -> bool:
        if isinstance(getattr(self, "optimize", None), mock.Mock):
            return True
        return any(getattr(genetic_module, name) is not fn for name, fn in pristine.items() if name != "optimize")

    def optimize_ems(self: Any, parameters: Any, start_hour: Any = None, worst_case: bool = False,
                     ngen: Any = None, individuals: Any = None) -> Any:
        cfg = self.config.optimization.genetic
        backend = cfg.backend
        cfg.backend = "compare"
        native.last_compare_report = None
        try:
            return original(self, parameters, start_hour, worst_case, ngen, individuals)
        finally:
            cfg.backend = backend
            report = native.last_compare_report
            REPORTS.append({
                "test": os.environ.get("PYTEST_CURRENT_TEST", "?").rsplit(" ", 1)[0],
                "patched": patched(self),
                "report": None if report is None else {
                    "identical": report.identical,
                    "differences": len(report.differences),
                    "first": report.differences[:8],
                    "ignored": len(report.ignored_differences),
                    "native_error": report.native_error,
                    "profile": report.profile,
                    "profile_reproduces_python": report.profile_reproduces_python,
                    "python_s": report.python_s,
                    "native_s": report.native_s,
                    "python_net_euro": report.python_net_euro,
                    "native_net_euro": report.native_net_euro,
                },
            })

    GeneticOptimization.optimize_ems = optimize_ems  # type: ignore[method-assign]


def summarize(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts per outcome; ``different`` lists the unpatched runs that differ."""
    summary: dict[str, Any] = {"runs": len(reports), "identical": 0, "unsupported": 0,
                               "patched": 0, "no_report": 0, "different": []}
    for entry in reports:
        report = entry["report"]
        if report is None:
            summary["no_report"] += 1
        elif report["identical"]:
            summary["identical"] += 1
        elif entry["patched"]:
            summary["patched"] += 1
        elif report["native_error"]:
            summary["unsupported"] += 1
        else:
            summary["different"].append(entry)
    return summary


def pytest_unconfigure(config: Any) -> None:
    summary = summarize(REPORTS)
    out = os.environ.get("GENETIC_COMPARE_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            json.dump({"summary": summary, "reports": REPORTS}, handle, indent=1)
    print(
        "\nGENETIC compare: {runs} runs, {identical} identical, {unsupported} not supported natively, "
        "{patched} with patched Python, {no_report} without report, {n} different".format(
            n=len(summary["different"]), **{k: v for k, v in summary.items() if k != "different"}
        )
    )
    for entry in summary["different"]:
        print("  DIFFERENT", entry["test"], entry["report"]["first"][:3])
