"""Optional native (Rust) backend for the GENETIC optimization.

``optimization.genetic.backend`` selects the implementation:

- ``python`` (default): ``GeneticOptimization`` as it is. Nothing in this module runs.
- ``native``: the optional package ``akkudoktoreos-native`` runs the whole optimization
  (``GeneticOptimization._optimize_ems_run``) in Rust. The result is the same
  ``GeneticSolution`` model. Whenever the native run is not possible - package missing, these
  GENETIC sources not verified for the installed package, a request the kernel does not
  support, or any error - the run falls back to Python and the reason is logged.
- ``compare``: both run; the Python solution is returned and every difference is logged.

Verification guard
------------------
The kernel is a port of the Python code in this package. It is used only for GENETIC sources it
was verified against: :func:`source_fingerprint` hashes the Python sources that define the
optimization (comments and docstrings ignored) and the package's ``certified.json`` maps known
fingerprints to the behaviour switches of the kernel. Any change of those sources disables the
native backend until the change is ported and the fingerprint certified; the ``compare`` CI job
reports it. ``EOS_NATIVE_GENETIC_UNCERTIFIED=1`` lets an uncertified fingerprint run with the
switches of EOS main - for that CI job only.

Numerical profile
-----------------
Python computes two operations with platform libraries whose rounding depends on the CPU: the
dot products of the self-consumption table (``np.dot`` -> OpenBLAS/Accelerate kernel) and, on
some builds, ``np.interp`` (fused multiply-add). With the same seed, Python therefore returns
different plans on different CPUs. The native kernel uses a fixed profile (sequential sums, no
fused multiply-add) in ``native`` mode: same input and seed, same plan on every CPU. In
``compare`` mode it reproduces this machine's Python profile when it can detect one
(:func:`reference_profile`); then the two results must be identical bit for bit. If the
profile cannot be reproduced (e.g. Accelerate on macOS, x86-64 OpenBLAS kernels), ``compare``
still runs and reports the differences as expected.
"""

from __future__ import annotations

import importlib
import io
import json
import math
import os
import random
import struct
import threading
import time
import tokenize
from dataclasses import dataclass, field
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Optional

import numpy as np
from loguru import logger

from akkudoktoreos.optimization.genetic.geneticsettings import GeneticBackend

if TYPE_CHECKING:
    from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
    from akkudoktoreos.optimization.genetic.geneticparams import (
        GeneticOptimizationParameters,
    )
    from akkudoktoreos.optimization.genetic.geneticsolution import GeneticSolution

NATIVE_PACKAGE = "akkudoktoreos_native"

# JSON layout spoken with the kernel (eos_genetic::run::INTERFACE_VERSION).
INTERFACE_VERSION = 1

# Environment switch for the compare CI job: run uncertified sources with EOS main semantics.
UNCERTIFIED_ENV = "EOS_NATIVE_GENETIC_UNCERTIFIED"

_PACKAGE_ROOT = Path(__file__).resolve().parents[2]

# Sources that define what a GENETIC run computes, relative to the akkudoktoreos package.
FINGERPRINT_SOURCES: tuple[str, ...] = (
    "optimization/genetic/genetic.py",
    "optimization/genetic/geneticdevices.py",
    "optimization/genetic/geneticparams.py",
    "optimization/genetic/geneticsettings.py",
    "optimization/genetic/geneticsolution.py",
    "optimization/genetic/fullcharge.py",
    "optimization/genetic/tailvalue.py",
    "optimization/genetic/terminalvalue.py",
    "devices/devicesabc.py",
    "devices/genetic/battery.py",
    "devices/genetic/homeappliance.py",
    "devices/genetic/inverter.py",
    "devices/settings/batterysettings.py",
    "prediction/interpolator.py",
    "utils/gridinterpolator.py",
)
FINGERPRINT_DATA: tuple[str, ...] = ("data/regular_grid_interpolator.pkl",)
SELF_CONSUMPTION_TABLE = "data/regular_grid_interpolator.pkl"


# --------------------------------------------------------------------------- guard


def _normalized_source(text: str) -> bytes:
    """Tokens of a Python source without comments, blank lines and docstrings.

    Formatting, comments and docstrings do not change what the optimization computes, so they
    do not change the fingerprint. Docstrings are string statements that start a logical line
    and end it.
    """
    tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    skip = {tokenize.COMMENT, tokenize.NL, tokenize.ENCODING}
    significant = [tok for tok in tokens if tok.type not in skip]
    out: list[str] = []
    statement_start = True
    for index, tok in enumerate(significant):
        following = significant[index + 1] if index + 1 < len(significant) else None
        if (
            tok.type == tokenize.STRING
            and statement_start
            and following is not None
            and following.type in (tokenize.NEWLINE, tokenize.ENDMARKER)
        ):
            continue
        if tok.type in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            statement_start = True
            if out and out[-1] == "\n" and tok.type == tokenize.NEWLINE:
                continue
            out.append(
                {tokenize.NEWLINE: "\n", tokenize.INDENT: "<I>", tokenize.DEDENT: "<D>"}[tok.type]
            )
            continue
        statement_start = False
        out.append(tok.string)
    return "\x00".join(out).encode("utf-8")


def compute_fingerprint(root: Path = _PACKAGE_ROOT) -> str:
    """Fingerprint of the GENETIC sources below ``root`` (the ``akkudoktoreos`` package)."""
    digest = sha256()
    for relative in FINGERPRINT_SOURCES:
        digest.update(relative.encode("utf-8") + b"\x00")
        digest.update(_normalized_source((root / relative).read_text(encoding="utf-8")))
        digest.update(b"\x00")
    for relative in FINGERPRINT_DATA:
        digest.update(relative.encode("utf-8") + b"\x00")
        digest.update(sha256((root / relative).read_bytes()).digest())
    return "sha256:" + digest.hexdigest()


@lru_cache(maxsize=1)
def source_fingerprint() -> str:
    """Fingerprint of the installed GENETIC sources (cached per process)."""
    return compute_fingerprint()


@lru_cache(maxsize=1)
def _table_sha256() -> str:
    return sha256((_PACKAGE_ROOT / SELF_CONSUMPTION_TABLE).read_bytes()).hexdigest()


@dataclass(frozen=True)
class NativeStatus:
    """Whether the native backend can run, and with which behaviour switches."""

    available: bool
    reason: str = ""
    version: Optional[str] = None
    semantics: dict[str, bool] = field(default_factory=dict)
    certified: bool = False


def _import_native() -> Optional[ModuleType]:
    try:
        return importlib.import_module(NATIVE_PACKAGE)
    except ImportError:
        return None


_status_lock = threading.Lock()
_status_cache: Optional[NativeStatus] = None


def reset_status_cache() -> None:
    """Forget the cached package status (tests, or after installing the package)."""
    global _status_cache
    with _status_lock:
        _status_cache = None
    source_fingerprint.cache_clear()
    _table_sha256.cache_clear()
    _reference_profile_cache.clear()


def native_status() -> NativeStatus:
    """Status of the native backend for this process (cached)."""
    global _status_cache
    with _status_lock:
        if _status_cache is None:
            _status_cache = _probe_status()
        return _status_cache


def _probe_status() -> NativeStatus:
    native = _import_native()
    if native is None:
        return NativeStatus(False, f"package '{NATIVE_PACKAGE}' is not installed")
    version = str(getattr(native, "__version__", "unknown"))
    if getattr(native, "INTERFACE_VERSION", None) != INTERFACE_VERSION:
        return NativeStatus(
            False,
            f"{NATIVE_PACKAGE} {version} speaks interface "
            f"{getattr(native, 'INTERFACE_VERSION', None)}, EOS needs {INTERFACE_VERSION}",
            version,
        )
    if getattr(native, "SELF_CONSUMPTION_TABLE_SHA256", None) != _table_sha256():
        return NativeStatus(
            False,
            f"{NATIVE_PACKAGE} {version} bundles another self-consumption table",
            version,
        )
    fingerprint = source_fingerprint()
    semantics = native.certified_semantics(fingerprint)
    if semantics is not None:
        return NativeStatus(True, "", version, dict(semantics), certified=True)
    if os.environ.get(UNCERTIFIED_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        return NativeStatus(
            True,
            f"GENETIC sources {fingerprint} are not certified; {UNCERTIFIED_ENV} is set",
            version,
            {name: False for name in native.SEMANTICS},
            certified=False,
        )
    return NativeStatus(
        False,
        f"{NATIVE_PACKAGE} {version} was not verified against these GENETIC sources ({fingerprint})",
        version,
    )


_logged_reasons: set[str] = set()


def _log_fallback(reason: str) -> None:
    """Log why a run uses Python: once as warning, repeats at debug level."""
    message = "GENETIC native backend not used, running Python: {}"
    if reason in _logged_reasons:
        logger.debug(message, reason)
    else:
        _logged_reasons.add(reason)
        logger.warning(message, reason)


# --------------------------------------------------------------------------- numerical profile

RELEASE_PROFILE: dict[str, Any] = {"dot": "sequential", "interp_fma": False}


def sequential_dot(a: Any, b: Any) -> float:
    """Dot product summed left to right without fused multiply-add (the release profile).

    ``np.dot`` gives the same sum only up to the BLAS kernel's summation order.
    """
    total = 0.0
    for x, y in zip(np.asarray(a, dtype=float).tolist(), np.asarray(b, dtype=float).tolist()):
        total += x * y
    return total


def expected_direct_consumption_release_profile(
    interpolator: Any, mean_load_power_w: float, pv_power_w: float
) -> float:
    """``SelfConsumptionProbabilityInterpolator._expected_direct_consumption`` on the release profile.

    The same computation with :func:`sequential_dot` instead of ``np.dot``. Patched in by the
    tests (and the compare CI job) so that Python and the kernel's release profile must agree
    bit for bit on every CPU.
    """
    mean_load_power_w = max(float(mean_load_power_w), 0.0)
    pv_power_w = max(float(pv_power_w), 0.0)
    if mean_load_power_w == 0.0 or pv_power_w == 0.0:
        return 0.0
    load_levels_w, probabilities = interpolator._load_distribution(mean_load_power_w)
    modeled_mean_load_w = sequential_dot(probabilities, load_levels_w)
    if modeled_mean_load_w <= 0.0:
        return 0.0
    normalized_load_levels_w = load_levels_w * (mean_load_power_w / modeled_mean_load_w)
    expected_direct_power_w = sequential_dot(
        probabilities, np.minimum(normalized_load_levels_w, pv_power_w)
    )
    return min(max(expected_direct_power_w, 0.0), min(mean_load_power_w, pv_power_w))


_reference_profile_cache: dict[str, Optional[dict[str, Any]]] = {}


def reference_profile(native: Any = None) -> Optional[dict[str, Any]]:
    """Numerical profile that reproduces this process' Python arithmetic, if there is one.

    Probes ``np.interp`` (fused multiply-add or not) and the expected direct consumption of the
    self-consumption interpolator (table backend plus ``np.dot`` kernel) against the kernel's
    variants. ``None`` when no variant matches every probe.
    """
    from akkudoktoreos.prediction.interpolator import get_eos_load_interpolator
    from akkudoktoreos.utils.gridinterpolator import (
        configured_grid_interpolator_backend,
    )

    native = native if native is not None else _import_native()
    if native is None:
        return None
    key = str(configured_grid_interpolator_backend())
    if key in _reference_profile_cache:
        return _reference_profile_cache[key]

    rng = random.Random(20261110)  # noqa: S311 - fixed probes, not security relevant
    fma: Optional[bool] = None
    for candidate in (False, True):
        ok = True
        for _ in range(400):
            xp = sorted(rng.uniform(0.0, 1e4) for _ in range(rng.randint(2, 40)))
            if len(set(xp)) != len(xp):
                continue
            fp = [rng.uniform(-50.0, 50.0) for _ in xp]
            x = rng.uniform(xp[0], xp[-1])
            if float(np.interp(x, xp, fp)) != native.interp(x, xp, fp, candidate):
                ok = False
                break
        if ok:
            fma = candidate
            break

    interpolator = get_eos_load_interpolator()
    dot_kernel: Optional[str] = None
    for kernel in native.DOT_KERNELS:
        ok = True
        for _ in range(300):
            load_w = rng.uniform(50.0, 6000.0)
            pv_w = rng.uniform(1.0, 9000.0)
            expected = interpolator._expected_direct_consumption(load_w, pv_w)
            if expected != native.expected_direct_consumption(kernel, load_w, pv_w):
                ok = False
                break
        if ok:
            dot_kernel = kernel
            break

    profile = None if fma is None or dot_kernel is None else {"dot": dot_kernel, "interp_fma": fma}
    _reference_profile_cache[key] = profile
    return profile


# --------------------------------------------------------------------------- request / result


class NativeUnsupported(Exception):
    """The run cannot be done natively; the message says why."""


def _json_ready(value: Any) -> Any:
    """``value`` with NaN as ``None`` (the kernel reads ``null`` in a series as NaN).

    Python keeps NaN forecast values (a gap ends the usable forecast); JSON has no NaN.
    Infinite values have no JSON form the kernel reads, so such runs stay in Python.
    """
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if math.isinf(value):
            raise NativeUnsupported("the request contains an infinite value")
        return value
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def _isoformat(value: Any) -> str:
    return value.isoformat()


def _timezone_name(value: Any) -> Optional[str]:
    name = getattr(value, "timezone_name", None)
    if name:
        return str(name)
    tzinfo = getattr(value, "tzinfo", None)
    name = getattr(tzinfo, "name", None)
    return str(name) if name else None


def build_request(
    optimization: GeneticOptimization,
    parameters: GeneticOptimizationParameters,
    worst_case: bool,
    ngen: Optional[int],
    individuals: Optional[int],
    semantics: dict[str, bool],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """The kernel request for one run, from the run's configuration snapshot."""
    config = optimization.config
    timezone = config.general.timezone
    if timezone is None:
        raise NativeUnsupported("general.timezone is not set")
    ems = optimization.ems
    start = ems.start_datetime
    if start is None:
        raise NativeUnsupported("energy management start time is not set")

    def first_device(devices: Any) -> Any:
        if not devices:
            return None
        return next(iter(devices.values()), None)

    battery = first_device(config.devices.batteries)
    vehicle = first_device(config.devices.electric_vehicles)

    last_solution = None
    previous = ems.genetic_solution()
    if (
        previous is not None
        and previous.start_solution is not None
        and previous.start_solution_datetime is not None
    ):
        last_solution = {
            "start_solution": [float(v) for v in previous.start_solution],
            "start_solution_datetime": _isoformat(previous.start_solution_datetime),
        }

    observation = getattr(ems, "observation_datetime", None)
    return {
        "interface_version": INTERFACE_VERSION,
        "parameters": parameters.model_dump(mode="json"),
        "settings": optimization._genetic_cfg.model_dump(mode="json"),
        "prediction_hours": int(optimization._prediction_hours_cfg),
        "direct_marketing_enabled": bool(optimization._direct_marketing_enabled()),
        "timezone": str(timezone),
        "start_datetime": _isoformat(start),
        "start_datetime_timezone": _timezone_name(start),
        "observation_datetime": None if observation is None else _isoformat(observation),
        "fallback_battery_charge_rates": None if battery is None else battery.charge_rates,
        "fallback_battery_grid_export_rates": None
        if battery is None
        else battery.grid_export_rates,
        "fallback_ev_charge_rates": None if vehicle is None else vehicle.charge_rates,
        "last_solution": last_solution,
        "seed": optimization.fix_seed,
        "generations": ngen,
        "individuals": individuals,
        "worst_case": bool(worst_case),
        "threads": optimization._configured_evaluation_workers(),
        "semantics": semantics,
        "numeric_profile": profile,
    }


def _solution_parameters(
    optimization: GeneticOptimization, parameters: GeneticOptimizationParameters
) -> GeneticOptimizationParameters:
    """The parameters as ``_optimize_ems_run`` reports them in the solution.

    The normalization onto the slot grid, plus the placeholder time windows that
    ``HomeAppliance`` writes into appliance parameters without windows.
    """
    from akkudoktoreos.devices.genetic.homeappliance import HomeAppliance

    normalized = optimization._parameters_for_slot_grid(
        optimization._parameters_for_config(parameters)
    )
    for appliance_parameters in normalized.resolved_home_appliances():
        HomeAppliance(
            parameters=appliance_parameters,
            optimization_hours=optimization._genetic_cfg.horizon_hours,
            prediction_hours=optimization.control_end_slot,
            slot_duration_h=optimization.slot_duration_h,
        )
    return normalized


@dataclass
class NativeRun:
    """A finished native run."""

    solution: GeneticSolution
    diagnostics: dict[str, Any]
    elapsed_s: float


def run_native(
    optimization: GeneticOptimization,
    parameters: GeneticOptimizationParameters,
    worst_case: bool,
    ngen: Optional[int],
    individuals: Optional[int],
    status: NativeStatus,
    profile: dict[str, Any],
) -> NativeRun:
    """Run the optimization in the kernel; raises on any failure."""
    from akkudoktoreos.optimization.genetic.geneticsolution import GeneticSolution

    native = _import_native()
    if native is None:
        raise NativeUnsupported(f"package '{NATIVE_PACKAGE}' is not installed")
    request = build_request(
        optimization, parameters, worst_case, ngen, individuals, status.semantics, profile
    )
    started = time.perf_counter()
    try:
        response = json.loads(native.run_genetic(json.dumps(_json_ready(request), allow_nan=False)))
    except native.NativeRunError as exc:
        code, message = (exc.args + ("", ""))[:2] if exc.args else ("error", "")
        raise NativeUnsupported(f"kernel rejected the run ({code}): {message}") from exc
    elapsed = time.perf_counter() - started
    data = response["solution"]
    data["parameters"] = _solution_parameters(optimization, parameters)
    solution = GeneticSolution(**data)
    return NativeRun(solution, response.get("diagnostics", {}), elapsed)


# --------------------------------------------------------------------------- comparison


def _same_float(a: float, b: float) -> bool:
    if math.isnan(a) and math.isnan(b):
        return True
    return struct.pack("<d", a) == struct.pack("<d", b)


def diff_values(
    left: Any, right: Any, path: str = "", out: Optional[list[str]] = None
) -> list[str]:
    """Paths where two JSON-like values differ (floats bit for bit)."""
    if out is None:
        out = []
    if isinstance(left, bool) or isinstance(right, bool):
        if left is not right:
            out.append(f"{path}: {left!r} != {right!r}")
    elif isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if isinstance(left, int) and isinstance(right, int):
            if left != right:
                out.append(f"{path}: {left!r} != {right!r}")
        elif not _same_float(float(left), float(right)):
            out.append(f"{path}: {left!r} != {right!r}")
    elif isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right), key=str):
            if key not in left or key not in right:
                out.append(f"{path}.{key}: only in {'python' if key in left else 'native'}")
            else:
                diff_values(left[key], right[key], f"{path}.{key}", out)
    elif isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        if len(left) != len(right):
            out.append(f"{path}: length {len(left)} != {len(right)}")
        for index, (a, b) in enumerate(zip(left, right)):
            diff_values(a, b, f"{path}[{index}]", out)
    elif left != right:
        out.append(f"{path}: {left!r} != {right!r}")
    return out


def compare_solutions(python: GeneticSolution, native: GeneticSolution) -> list[str]:
    """Differences between two solutions (``model_dump`` paths)."""
    return diff_values(python.model_dump(mode="json"), native.model_dump(mode="json"))


# With a bounded fitness cache, Python's hit/miss counters depend on the number of
# evaluation workers (a batch looks up all genomes before it stores any); the kernel
# counts like one worker. The plan does not depend on it.
CACHE_COUNTERS = ".fitness_history.fitness_cache."


def _counters_depend_on_workers(optimization: GeneticOptimization) -> bool:
    limit = optimization._genetic_cfg.fitness_cache_max_entries
    return limit is not None and limit > 0 and optimization._configured_evaluation_workers() > 1


@dataclass
class CompareReport:
    """Outcome of a ``compare`` run."""

    identical: bool
    differences: list[str]
    profile: dict[str, Any]
    profile_reproduces_python: bool
    python_s: float
    native_s: float
    python_net_euro: float
    native_net_euro: float
    native_error: str = ""
    # Differences that are expected for this configuration and not counted (see CACHE_COUNTERS).
    ignored_differences: list[str] = field(default_factory=list)


last_compare_report: Optional[CompareReport] = None


def _net_euro(solution: GeneticSolution) -> float:
    credited = solution.terminal_value.credited_euro if solution.terminal_value else 0.0
    return float(solution.result.total_balance) - float(credited)


# --------------------------------------------------------------------------- dispatch


def optimize_ems(
    optimization: GeneticOptimization,
    backend: GeneticBackend,
    parameters: GeneticOptimizationParameters,
    start_hour: Optional[int],
    worst_case: bool,
    ngen: Optional[int],
    individuals: Optional[int],
) -> GeneticSolution:
    """``GeneticOptimization.optimize_ems`` with the configured backend (snapshot in place)."""

    def python_run() -> GeneticSolution:
        return optimization._optimize_ems_run(parameters, start_hour, worst_case, ngen, individuals)

    if backend == GeneticBackend.PYTHON:
        return python_run()
    status = native_status()
    if not status.available:
        _log_fallback(status.reason)
        return python_run()
    if start_hour is not None and start_hour != optimization.ems.start_datetime.hour:
        # Python raises the error for an unsynchronized start hour.
        return python_run()

    if backend == GeneticBackend.NATIVE:
        try:
            run = run_native(
                optimization, parameters, worst_case, ngen, individuals, status, RELEASE_PROFILE
            )
        except Exception as exc:  # noqa: BLE001 - every failure falls back to Python
            _log_fallback(str(exc) or type(exc).__name__)
            return python_run()
        logger.info(
            "GENETIC native run ({} {}) took {:.2f} s.",
            NATIVE_PACKAGE,
            status.version,
            run.elapsed_s,
        )
        return run.solution

    # COMPARE
    global last_compare_report
    profile = reference_profile()
    reproduces = profile is not None
    native_run: Optional[NativeRun] = None
    native_error = ""
    try:
        native_run = run_native(
            optimization,
            parameters,
            worst_case,
            ngen,
            individuals,
            status,
            profile if profile is not None else RELEASE_PROFILE,
        )
    except Exception as exc:  # noqa: BLE001 - reported, Python result is returned
        native_error = str(exc) or type(exc).__name__
    started = time.perf_counter()
    solution = python_run()
    python_s = time.perf_counter() - started
    if native_run is None:
        last_compare_report = CompareReport(
            identical=False,
            differences=[],
            profile=profile or RELEASE_PROFILE,
            profile_reproduces_python=reproduces,
            python_s=python_s,
            native_s=0.0,
            python_net_euro=_net_euro(solution),
            native_net_euro=math.nan,
            native_error=native_error,
        )
        logger.warning("GENETIC compare: native run failed: {}", native_error)
        return solution
    differences = compare_solutions(solution, native_run.solution)
    ignored: list[str] = []
    if _counters_depend_on_workers(optimization):
        ignored = [d for d in differences if d.startswith(CACHE_COUNTERS)]
        differences = [d for d in differences if not d.startswith(CACHE_COUNTERS)]
    report = CompareReport(
        identical=not differences,
        differences=differences,
        profile=profile or RELEASE_PROFILE,
        profile_reproduces_python=reproduces,
        python_s=python_s,
        native_s=native_run.elapsed_s,
        python_net_euro=_net_euro(solution),
        native_net_euro=_net_euro(native_run.solution),
        ignored_differences=ignored,
    )
    last_compare_report = report
    summary = (
        "GENETIC compare: {} (profile {}, python {:.2f} s, native {:.2f} s, "
        "net python {:.6f} EUR, native {:.6f} EUR)"
    )
    if report.identical:
        logger.info(
            summary,
            "identical",
            report.profile,
            report.python_s,
            report.native_s,
            report.python_net_euro,
            report.native_net_euro,
        )
    elif not reproduces:
        logger.info(
            summary + "; this CPU's Python arithmetic cannot be reproduced, so differences "
            "are expected. First: {}",
            f"{len(differences)} differences",
            report.profile,
            report.python_s,
            report.native_s,
            report.python_net_euro,
            report.native_net_euro,
            differences[:5],
        )
    else:
        logger.warning(
            summary + ". First: {}",
            f"{len(differences)} differences",
            report.profile,
            report.python_s,
            report.native_s,
            report.python_net_euro,
            report.native_net_euro,
            differences[:5],
        )
    return solution
