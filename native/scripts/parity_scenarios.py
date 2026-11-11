#!/usr/bin/env python3
"""Compare the native GENETIC kernel with Python on generated scenarios.

Builds random but reproducible GENETIC runs that cover the inputs the kernel ports: hourly and
15-minute slots, runs on and inside a slot boundary, direct marketing, AC charge limits, an
efficiency curve, a grid import limit (none, tight, unreachable), an EV with charge start,
deadline (inside a slot, passed, beyond the horizon), maximum duration, SoC value steps and
appliances. Each scenario runs in compare mode (``native.compare_run``): Python and the kernel
with the same seed, solutions compared bit for bit.

    python native/scripts/parity_scenarios.py --count 200 --seed 1 --out scenarios.json

The report has the layout of ``genetic_compare_plugin`` (``summary`` with ``identical``,
``different``, ``failed``, fingerprint and kernel version), so ``certify.py`` reads it too.
Exit status 1 if any scenario differs or fails natively.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import random
import sys
import time
from typing import Any, Optional

TIMEZONE = "Europe/Berlin"
DAYS = ("2026-03-28", "2026-06-21", "2026-09-16", "2026-10-24", "2026-12-15")


def _iso(moment: dt.datetime) -> str:
    return moment.isoformat()


def scenario(seed: int) -> dict[str, Any]:
    """Settings, start time and parameters of one scenario (plain JSON)."""
    from zoneinfo import ZoneInfo

    rng = random.Random(seed)
    interval = rng.choice((3600, 900))
    per_hour = 3600 // interval
    zone = ZoneInfo(TIMEZONE)
    day = dt.date.fromisoformat(rng.choice(DAYS))
    hour = rng.randrange(24)
    # Half of the runs start inside a slot (a client re-planning every few minutes).
    minute = 0 if rng.random() < 0.5 else rng.randrange(1, 60 if interval == 3600 else 15)
    if interval == 900 and minute == 0 and rng.random() < 0.5:
        minute = rng.choice((15, 30, 45))
    start = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
    slot_start = start.replace(minute=start.minute - start.minute % (interval // 60))
    horizon = rng.choice((12, 24, 36))
    tail = rng.choice((0, 0, 12))
    # Forecast series start at local midnight of the run's day (EOS trims them to now).
    prediction_hours = 72
    midnight = dt.datetime(day.year, day.month, day.day, tzinfo=zone)
    slots = prediction_hours * per_hour
    direct_marketing = rng.random() < 0.3

    pv, load, price, feed_in = [], [], [], []
    pv_peak = rng.uniform(2000.0, 14000.0)
    base_load = rng.uniform(250.0, 900.0)
    for slot in range(slots):
        local = (midnight + dt.timedelta(seconds=slot * interval)).astimezone(zone)
        h = local.hour + local.minute / 60.0
        sun = max(0.0, math.sin((h - 6.0) / 14.0 * math.pi))
        pv.append(round(pv_peak * sun * rng.uniform(0.4, 1.0) / per_hour, 3))
        peak = rng.uniform(1500.0, 7000.0) if rng.random() < 0.08 else 0.0
        load.append(
            round((base_load + 800.0 * (17 <= h < 22) + rng.uniform(0, 400) + peak) / per_hour, 3)
        )
        price.append(
            round((0.24 + 0.14 * math.cos((h - 19) / 24 * 2 * math.pi) + rng.uniform(-0.03, 0.03))
                  / 1000.0, 9)
        )
        feed_in.append(round((0.079 - 0.12 * (11 <= h < 15) * rng.random()) / 1000.0, 9))

    battery_rates = rng.choice(
        ([0.0, 0.25, 0.5, 1.0], [0.0, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0], [0.0, 1.0])
    )
    data: dict[str, Any] = {
        "ems": {
            "pv_forecast_wh": pv,
            "total_load": load,
            "electricity_price_per_wh": price,
            "feed_in_tariff_per_wh": feed_in if rng.random() < 0.7 else 7.9e-05,
            "price_per_wh_battery": 0.0,
        },
        "pv_battery": {
            "device_id": "battery1",
            "capacity_wh": rng.choice((5000, 10000, 15000, 24000)),
            "initial_soc_percentage": rng.randrange(5, 95),
            "min_soc_percentage": rng.choice((0, 5, 10)),
            "max_charge_power_w": rng.choice((2500, 5000, 8000)),
            "charge_rates": battery_rates,
            "grid_export_rates": rng.choice(([0.5, 1.0], [1.0])),
        },
        "inverter": {
            "device_id": "inverter1",
            "max_power_wh": rng.choice((5000, 10000, 15000)),
            "battery_id": "battery1",
        },
        "forecast_interval_seconds": interval,
        "ev": None,
    }
    inverter = data["inverter"]
    if rng.random() < 0.3:
        inverter["max_ac_charge_power_w"] = rng.choice((1500.0, 3000.0, 6000.0))
    if rng.random() < 0.3:
        inverter["ac_charge_limits_total_charge"] = True
    if rng.random() < 0.2:
        inverter["dc_to_ac_efficiency_curve"] = [[0.05, 0.86], [0.2, 0.95], [0.6, 0.97], [1.0, 0.96]]
    limit_kind = rng.choice(("none", "none", "tight", "tight", "generous", "unreachable"))
    if limit_kind == "tight":
        inverter["max_grid_import_power_w"] = float(rng.choice((2500, 4000, 6000, 8000)))
    elif limit_kind == "generous":
        inverter["max_grid_import_power_w"] = float(rng.choice((15000, 25000)))
    elif limit_kind == "unreachable":
        # Below the house load: the limit can not be kept even without any charging.
        inverter["max_grid_import_power_w"] = float(rng.choice((0, 150, 300)))

    ev_kind = "none"
    deadline_kind = None
    if rng.random() < 0.75:
        ev_kind = "ev"
        capacity = rng.choice((40000, 60000, 80000))
        initial = rng.randrange(10, 90)
        target = rng.choice((initial - 5, initial + 10, 70, 80, 100))
        target = max(0, min(100, target))
        ev: dict[str, Any] = {
            "device_id": "ev1",
            "capacity_wh": capacity,
            "charging_efficiency": rng.choice((0.88, 0.9, 0.95)),
            "max_charge_power_w": rng.choice((3700, 7400, 11000)),
            "initial_soc_percentage": initial,
            "min_soc_percentage": target,
            "max_soc_percentage": rng.choice((100, 95, max(target, 90))),
            "charge_rates": rng.choice(
                (None, [0.0, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0], [0.0, 0.5, 1.0], [0.0, 1.0])
            ),
        }
        deadline_kind = rng.choice(("none", "inside", "inside", "boundary", "passed", "beyond"))
        if deadline_kind == "inside":
            ev["min_soc_deadline_datetime"] = _iso(
                start + dt.timedelta(minutes=rng.randrange(5, horizon * 60 - 5))
            )
        elif deadline_kind == "boundary":
            ev["min_soc_deadline_datetime"] = _iso(
                slot_start + dt.timedelta(seconds=interval * rng.randrange(1, horizon * per_hour))
            )
        elif deadline_kind == "passed":
            ev["min_soc_deadline_datetime"] = _iso(start - dt.timedelta(minutes=rng.randrange(1, 300)))
        elif deadline_kind == "beyond":
            ev["min_soc_deadline_datetime"] = _iso(start + dt.timedelta(hours=horizon + 3))
        if rng.random() < 0.3:
            ev["min_soc_max_duration_h"] = round(rng.uniform(0.5, horizon + 2.0), 2)
        start_kind = rng.choice(("none", "none", "past", "current_slot", "future"))
        if start_kind == "past":
            ev["charge_start_datetime"] = _iso(start - dt.timedelta(minutes=rng.randrange(1, 120)))
        elif start_kind == "current_slot":
            remaining = (slot_start + dt.timedelta(seconds=interval) - start).total_seconds() / 60
            if remaining > 1:
                ev["charge_start_datetime"] = _iso(
                    start + dt.timedelta(minutes=rng.randrange(1, int(remaining)))
                )
        elif start_kind == "future":
            ev["charge_start_datetime"] = _iso(
                start + dt.timedelta(minutes=rng.randrange(10, horizon * 60))
            )
        if rng.random() < 0.35:
            first = min(100.0, max(float(target) + rng.choice((5.0, 10.0)), 1.0))
            steps = [[first, round(rng.uniform(0.05, 0.4), 3)]]
            if first + 10.0 <= 100.0 and rng.random() < 0.5:
                steps.append([first + 10.0, round(steps[0][1] * rng.uniform(0.2, 1.0), 3)])
            ev["soc_value_steps"] = steps
        data["ev"] = ev
    if rng.random() < 0.25:
        data["home_appliances"] = [
            {"device_id": "dishwasher1", "consumption_wh": 1800, "duration_h": 2},
        ]

    settings = {
        "interval_sec": interval,
        "horizon_hours": horizon,
        "tail_horizon_hours": tail,
        "terminal_value_mode": rng.choice(("AUTO", "AUTO", "FIXED")),
        "individuals": rng.choice((40, 60, 80)),
        "generations": rng.choice((15, 25, 35)),
        # Every penalty explicit (10.0 = the default of grid_import_limit_excess).
        "penalties": {"ev_soc_miss": 10, "ac_charge_break_even": 1, "grid_import_limit_excess": 10.0},
    }
    if inverter.get("max_grid_import_power_w") is not None and rng.random() < 0.3:
        settings["penalties"]["grid_import_limit_excess"] = rng.choice((2.0, 10.0, 50.0))
    return {
        "seed": seed,
        "start": _iso(start),
        "settings": settings,
        "prediction_hours": prediction_hours,
        "direct_marketing": direct_marketing,
        "parameters": data,
        "tags": {
            "interval": interval,
            "inside_slot": start != slot_start,
            "grid_limit": limit_kind,
            "ev": ev_kind,
            "deadline": deadline_kind if data["ev"] else None,
            "max_duration": bool(data["ev"] and data["ev"].get("min_soc_max_duration_h")),
            "charge_start": bool(data["ev"] and data["ev"].get("charge_start_datetime")),
            "soc_value_steps": bool(data["ev"] and data["ev"].get("soc_value_steps")),
            "direct_marketing": direct_marketing,
            "appliance": "home_appliances" in data,
        },
    }


def run_scenario(case: dict[str, Any], ev_rates: Optional[list[float]] = None) -> dict[str, Any]:
    """Run one scenario in compare mode; returns the report entry."""
    from akkudoktoreos.core.cache import CacheEnergyManagementStore
    from akkudoktoreos.core.coreabc import get_config, get_ems
    from akkudoktoreos.optimization.genetic import native
    from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
    from akkudoktoreos.optimization.genetic.geneticparams import (
        GeneticOptimizationParameters,
    )
    from akkudoktoreos.utils.datetimeutil import to_datetime

    config = get_config()
    # Each scenario from the defaults: runtime settings deep-merge, so an optional setting of an
    # earlier scenario (e.g. a penalty) must not leak into this one.
    config.reset_settings()
    config.merge_settings_from_dict(
        {
            "general": {"timezone": TIMEZONE},
            "prediction": {"hours": case["prediction_hours"]},
            "feedintariff": {"direct_marketing_enabled": case["direct_marketing"]},
            "optimization": {
                "algorithm": "GENETIC",
                "self_consumption_interpolator": "numpy",
                "genetic": {**case["settings"], "backend": "python"},
            },
            "devices": {
                "max_electric_vehicles": 1,
                "electric_vehicles": {
                    "ev1": {"charge_rates": ev_rates or [0.0, 0.25, 0.5, 0.75, 1.0]}
                },
            },
        }
    )
    entry: dict[str, Any] = {"test": f"scenario[{case['seed']}]", "patched": False,
                             "tags": case["tags"], "report": None}
    try:
        parameters = GeneticOptimizationParameters(**case["parameters"])
    except Exception as exc:  # noqa: BLE001 - an invalid generated input is skipped
        entry["invalid"] = f"{type(exc).__name__}: {exc}"[:300]
        return entry
    get_ems().set_start_datetime(to_datetime(case["start"], in_timezone=TIMEZONE))
    CacheEnergyManagementStore().clear()
    original = GeneticOptimization._optimize_ems_run

    def compare(self: Any, parameters: Any, start_hour: Any, worst_case: bool, ngen: Any,
                individuals: Any) -> Any:
        def python_run() -> Any:
            return original(self, parameters, start_hour, worst_case, ngen, individuals)

        return native.compare_run(self, python_run, parameters, start_hour, worst_case, ngen,
                                  individuals)

    native.last_compare_report = None
    GeneticOptimization._optimize_ems_run = compare  # type: ignore[method-assign]
    try:
        GeneticOptimization(fixed_seed=case["seed"]).optimize_ems(parameters=parameters)
    except Exception as exc:  # noqa: BLE001 - Python itself failed: not a kernel question
        entry["python_error"] = f"{type(exc).__name__}: {exc}"[:300]
        return entry
    finally:
        GeneticOptimization._optimize_ems_run = original  # type: ignore[method-assign]
    report = native.last_compare_report
    if report is not None:
        entry["report"] = {
            "identical": report.identical,
            "differences": len(report.differences),
            "first": report.differences[:8],
            "ignored": len(report.ignored_differences),
            "native_error": report.native_error,
            "native_unsupported": report.native_unsupported,
            "profile": report.profile,
            "profile_reproduces_python": report.profile_reproduces_python,
            "python_s": report.python_s,
            "native_s": report.native_s,
            "python_net_euro": report.python_net_euro,
            "native_net_euro": report.native_net_euro,
        }
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--seed", type=int, default=1, help="seed of the first scenario")
    parser.add_argument("--out", default="")
    parser.add_argument("--only", type=int, nargs="*", help="run only these scenario seeds")
    args = parser.parse_args()

    os.environ.setdefault("EOS_CONFIG_DIR", os.path.join(os.getcwd(), ".parity-config"))
    from loguru import logger

    logger.remove()
    logger.add(sys.stderr, level="ERROR")
    from akkudoktoreos.core.coreabc import get_config, get_ems
    from akkudoktoreos.optimization.genetic import native

    get_config(init=True)
    get_ems(init=True)
    native.reset_caches()
    seeds = args.only or list(range(args.seed, args.seed + args.count))
    entries = []
    started = time.perf_counter()
    for seed in seeds:
        entry = run_scenario(scenario(seed))
        entries.append(entry)
        report = entry["report"]
        state = (
            entry.get("invalid") and "invalid"
            or entry.get("python_error") and "python-error"
            or report is None and "no-report"
            or report["identical"] and "identical"
            or report["native_error"] and ("unsupported" if report["native_unsupported"] else "FAILED")
            or "DIFFERENT"
        )
        timing = "" if not report or report["native_error"] else (
            f" python {report['python_s']:.2f} s native {report['native_s']:.2f} s"
        )
        print(f"scenario {seed}: {state}{timing} {entry['tags']}", flush=True)
        if state in ("DIFFERENT", "FAILED"):
            print("   ", (report["first"] or [report["native_error"]])[:4], flush=True)

    kernel = native._import_native()
    summary: dict[str, Any] = {
        "runs": len(entries), "identical": 0, "unsupported": 0, "patched": 0, "no_report": 0,
        "invalid": 0, "python_error": 0, "different": [], "failed": [],
        "fingerprint": native.source_fingerprint(),
        "kernel_version": getattr(kernel, "__version__", None),
        "dot": "fixed-order (interpolator)",
        "seconds": time.perf_counter() - started,
    }
    python_s = native_s = 0.0
    for entry in entries:
        report = entry["report"]
        if entry.get("invalid"):
            summary["invalid"] += 1
        elif entry.get("python_error"):
            summary["python_error"] += 1
        elif report is None:
            summary["no_report"] += 1
        elif report["identical"]:
            summary["identical"] += 1
            python_s += report["python_s"]
            native_s += report["native_s"]
        elif report["native_error"] and report["native_unsupported"]:
            summary["unsupported"] += 1
        elif report["native_error"]:
            summary["failed"].append(entry)
        else:
            summary["different"].append(entry)
    summary["python_s_identical"] = python_s
    summary["native_s_identical"] = native_s
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump({"summary": summary, "reports": entries}, handle, indent=1)
    print(
        f"\nscenarios: {summary['runs']} runs, {summary['identical']} identical, "
        f"{summary['unsupported']} not supported natively, {len(summary['different'])} different, "
        f"{len(summary['failed'])} failed natively, {summary['invalid']} invalid inputs, "
        f"{summary['python_error']} Python errors; Python {python_s:.1f} s, native {native_s:.1f} s "
        "(identical runs)"
    )
    return 1 if summary["different"] or summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
