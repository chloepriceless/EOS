"""Smoke test of a DVhub-EOS image: GENETIC runs in the native kernel, without a Python fallback.

Run with the image's Python (build-image.sh does this):

    docker run --rm --entrypoint /opt/venv/bin/python -v <dir>:/smoke IMAGE /smoke/native-smoke.py

1. native_status(): the package is installed and certified for the image's GENETIC sources.
2. A short optimization with the configured backend (the image sets native) on synthetic inputs
   that use the DVhub features (grid import limit, EV charge start and deadline inside a slot, SoC
   value steps, a run inside a slot) must log "GENETIC native run" and no fallback.
Exit status 0 only if both hold.
"""

from __future__ import annotations

import math
import os
import sys
import tempfile

workdir = tempfile.mkdtemp(prefix="eos-native-smoke-")
os.environ.update(
    HOME=workdir,
    EOS_DIR=workdir,
    EOS_CONFIG_DIR=os.path.join(workdir, "config"),
    EOS_DATA_DIR=os.path.join(workdir, "data"),
)
os.makedirs(os.environ["EOS_CONFIG_DIR"], exist_ok=True)

from loguru import logger  # noqa: E402

messages: list[str] = []
logger.remove()
logger.add(lambda m: messages.append(m.record["message"]), level="INFO")

from akkudoktoreos.core.coreabc import get_config, get_ems  # noqa: E402
from akkudoktoreos.optimization.genetic import native  # noqa: E402
from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization  # noqa: E402
from akkudoktoreos.optimization.genetic.geneticparams import (  # noqa: E402
    GeneticOptimizationParameters,
)
from akkudoktoreos.utils.datetimeutil import to_datetime  # noqa: E402

status = native.native_status()
print(f"native_status: {status}")
print(f"fingerprint:   {native.source_fingerprint()}")
if not status.available:
    print("FAIL: native kernel not available:", status.reason)
    sys.exit(1)

config = get_config(init=True)
get_ems(init=True)
backend = os.environ.get("EOS_OPTIMIZATION__GENETIC__BACKEND", "")
config.merge_settings_from_dict(
    {
        "general": {"timezone": "Europe/Berlin"},
        "prediction": {"hours": 48},
        "optimization": {
            "algorithm": "GENETIC",
            "genetic": {
                "interval_sec": 900,
                "horizon_hours": 24,
                "tail_horizon_hours": 12,
                "individuals": 40,
                "generations": 10,
            },
        },
    }
)
if str(config.optimization.genetic.backend).lower().endswith("python"):
    print(f"FAIL: optimization.genetic.backend is {config.optimization.genetic.backend} "
          f"(EOS_OPTIMIZATION__GENETIC__BACKEND={backend!r})")
    sys.exit(1)

interval, per_hour, slots = 900, 4, 48 * 4
pv, load, price = [], [], []
for slot in range(slots):
    hour = (slot / per_hour) % 24
    sun = max(0.0, math.sin((hour - 6.0) / 14.0 * math.pi))
    pv.append(7000.0 * sun / per_hour)
    load.append((500.0 + 900.0 * (17 <= hour < 22)) / per_hour)
    price.append((0.24 + 0.12 * math.cos((hour - 19) / 24 * 2 * math.pi)) / 1000.0)
parameters = GeneticOptimizationParameters(
    ems={
        "pv_forecast_wh": pv,
        "total_load": load,
        "electricity_price_per_wh": price,
        "feed_in_tariff_per_wh": 7.9e-05,
        "price_per_wh_battery": 0.0,
    },
    pv_battery={
        "device_id": "battery1",
        "capacity_wh": 12000,
        "initial_soc_percentage": 40,
        "min_soc_percentage": 5,
        "max_charge_power_w": 5000,
        "charge_rates": [0.0, 0.25, 0.5, 1.0],
    },
    inverter={
        "device_id": "inverter1",
        "max_power_wh": 10000,
        "battery_id": "battery1",
        "max_grid_import_power_w": 6000.0,
    },
    ev={
        "device_id": "ev1",
        "capacity_wh": 60000,
        "charging_efficiency": 0.9,
        "max_charge_power_w": 11000,
        "initial_soc_percentage": 30,
        "min_soc_percentage": 70,
        "charge_rates": [0.0, 0.375, 0.5, 0.75, 1.0],
        "charge_start_datetime": "2026-09-16T10:20:00+02:00",
        "min_soc_deadline_datetime": "2026-09-17T06:40:00+02:00",
        "soc_value_steps": [[80.0, 0.25]],
    },
    forecast_interval_seconds=interval,
)
get_ems().set_start_datetime(to_datetime("2026-09-16T10:07:00+02:00", in_timezone="Europe/Berlin"))
solution = GeneticOptimization(fixed_seed=7).optimize_ems(parameters=parameters)

native_runs = [m for m in messages if "GENETIC native run" in m]
fallbacks = [m for m in messages if "native backend not used" in m]
for m in native_runs + fallbacks:
    print("log:", m)
if fallbacks or not native_runs:
    print("FAIL: the run did not use the native kernel")
    sys.exit(1)
if solution.result.peak_shaving_wh_per_hour is None:
    print("FAIL: the solution has no peak_shaving_wh_per_hour (grid import limit not applied)")
    sys.exit(1)
print("OK: native kernel used, no fallback")
