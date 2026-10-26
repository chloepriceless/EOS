"""Tests for the grid import power limit (peak shaving) of the GENETIC optimizer.

Commercial grid connections with interval metering pay a capacity price for
their highest 15-minute import. ``max_grid_import_power_w`` on the inverter
models that limit:

- AC (grid) charging of the battery is reduced first, so that load plus
  charging stays within the limit.
- The battery then covers load above the limit, within its discharge power,
  its minimum SoC and the inverter's AC capacity - also in slots where the plan
  does not release it for discharge, as a site controller enforcing the limit
  does.
- Import above the limit that the battery cannot avoid is simulated as is and
  adds a penalty to the fitness, so the optimizer learns to keep reserve and to
  charge earlier.

The limit is a power. Per slot it is an energy of
``max_grid_import_power_w x slot duration``. All expected values are worked
out by hand in the tests.
"""

from types import SimpleNamespace
from typing import Any, Optional, cast
from unittest.mock import Mock, patch

import numpy as np
import pytest
from pydantic import ValidationError

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems
from akkudoktoreos.devices.genetic.battery import (
    Battery,
    ElectricVehicleParameters,
    SolarPanelBatteryParameters,
)
from akkudoktoreos.devices.genetic.inverter import Inverter, InverterParameters
from akkudoktoreos.devices.settings.invertersettings import InverterCommonSettings
from akkudoktoreos.optimization.genetic.genetic import (
    GeneticOptimization,
    GeneticSimulation,
)
from akkudoktoreos.optimization.genetic.geneticparams import (
    GeneticEnergyManagementParameters,
    GeneticOptimizationParameters,
)
from akkudoktoreos.optimization.genetic.tailvalue import (
    _simulate_action,
    build_tail_value_curve,
)
from akkudoktoreos.optimization.genetic.terminalvalue import TerminalValueCurve
from akkudoktoreos.utils.datetimeutil import to_datetime

LIMIT_W = 8000.0
SLOTS = 4


def _devices(
    *,
    slot_duration_h: float = 1.0,
    limit_w: Optional[float] = LIMIT_W,
    capacity_wh: int = 10000,
    soc_percentage: int = 100,
    min_soc_percentage: int = 0,
    battery_power_w: float = 5000.0,
    charging_efficiency: float = 1.0,
    discharging_efficiency: float = 1.0,
    inverter_power_w: float = 10000.0,
    ac_to_dc_efficiency: float = 1.0,
    dc_to_ac_efficiency: float = 1.0,
    total_charge: bool = False,
) -> tuple[Inverter, Battery]:
    """Real battery and inverter; the battery is held (not released) in every slot."""
    battery = Battery(
        SolarPanelBatteryParameters(
            device_id="battery1",
            capacity_wh=capacity_wh,
            initial_soc_percentage=soc_percentage,
            min_soc_percentage=min_soc_percentage,
            max_soc_percentage=100,
            charging_efficiency=charging_efficiency,
            discharging_efficiency=discharging_efficiency,
            max_charge_power_w=battery_power_w,
        ),
        prediction_hours=SLOTS,
        slot_duration_h=slot_duration_h,
    )
    battery.reset()
    # reset() creates integer arrays; charge factors are fractions.
    battery.charge_array = np.zeros(SLOTS)
    battery.discharge_array = np.zeros(SLOTS, dtype=np.int_)
    inverter = Inverter(
        InverterParameters(
            device_id="inverter1",
            max_power_wh=inverter_power_w,
            battery_id="battery1",
            ac_to_dc_efficiency=ac_to_dc_efficiency,
            dc_to_ac_efficiency=dc_to_ac_efficiency,
            ac_charge_limits_total_charge=total_charge,
            max_grid_import_power_w=limit_w,
        ),
        battery=battery,
        slot_duration_h=slot_duration_h,
    )
    return inverter, battery


def _ac_slot(
    inverter: Inverter, battery: Battery, load_wh: float, factor: float, pv_wh: float = 0.0
) -> tuple[float, float, float, float]:
    """Run one AC charge slot the way GeneticSimulation.simulate() does.

    Returns:
        Grid import of the slot including AC charging, AC charge energy, AC
        charge losses and the losses of process_energy().
    """
    battery.charge_array[0] = factor
    battery.discharge_array[0] = 0
    rate = inverter.ac_charge_factor(factor)
    inverter.begin_ac_charge_slot(0, rate)
    _, grid_import, losses, _ = inverter.process_energy(pv_wh, load_wh, 0)
    ac_wh, ac_losses = inverter.charge_battery_from_grid(0, rate, slot_grid_import_wh=grid_import)
    return grid_import + ac_wh, ac_wh, ac_losses, losses


# ---------------------------------------------------------------------------
# Battery covers the load above the limit
# ---------------------------------------------------------------------------


class TestPeakShavingDischarge:
    def test_held_battery_covers_load_above_limit(self):
        """12 kWh load in one hour, limit 8 kW: the held battery delivers 4 kWh."""
        inverter, battery = _devices()

        grid_export, grid_import, losses, self_consumption = inverter.process_energy(
            0.0, 12000.0, 0
        )

        assert grid_import == pytest.approx(8000.0)
        assert self_consumption == pytest.approx(4000.0)
        assert battery.soc_wh == pytest.approx(10000.0 - 4000.0)
        assert losses == pytest.approx(0.0)
        assert grid_export == 0.0

    @pytest.mark.parametrize("slot_duration_h", [1.0, 0.25])
    def test_energy_limit_is_power_times_slot_duration(self, slot_duration_h):
        """12 kW load against an 8 kW limit: 8 kW x slot duration from the grid."""
        inverter, battery = _devices(slot_duration_h=slot_duration_h)
        load_wh = 12000.0 * slot_duration_h

        _, grid_import, _, _ = inverter.process_energy(0.0, load_wh, 0)

        assert grid_import == pytest.approx(LIMIT_W * slot_duration_h)
        assert 10000.0 - battery.soc_wh == pytest.approx(4000.0 * slot_duration_h)

    def test_discharge_power_limit_and_losses_quarter_hour(self):
        """The battery delivers less than requested; losses follow the delivery.

        15-minute slot: load 3000 Wh (12 kW), limit 2000 Wh (8 kW), excess
        1000 Wh. Battery power 2000 W -> 500 Wh raw per slot. Delivered DC
        500 x 0.9 = 450 Wh, AC 450 x 0.95 = 427.5 Wh. Losses 500 - 427.5.
        """
        inverter, battery = _devices(
            slot_duration_h=0.25,
            battery_power_w=2000.0,
            discharging_efficiency=0.9,
            dc_to_ac_efficiency=0.95,
        )

        _, grid_import, losses, self_consumption = inverter.process_energy(0.0, 3000.0, 0)

        assert self_consumption == pytest.approx(427.5)
        assert grid_import == pytest.approx(3000.0 - 427.5)
        assert losses == pytest.approx(500.0 - 427.5)
        assert battery.soc_wh == pytest.approx(10000.0 - 500.0)

    def test_minimum_soc_is_kept(self):
        """Only the 500 Wh above the 20 % floor are available."""
        inverter, battery = _devices(soc_percentage=25, min_soc_percentage=20)

        _, grid_import, _, _ = inverter.process_energy(0.0, 12000.0, 0)

        assert grid_import == pytest.approx(11500.0)
        assert battery.soc_wh == pytest.approx(2000.0)

    def test_empty_battery_import_is_simulated_as_is(self):
        inverter, battery = _devices(soc_percentage=0)

        _, grid_import, _, _ = inverter.process_energy(0.0, 12000.0, 0)

        assert grid_import == pytest.approx(12000.0)
        assert battery.soc_wh == 0.0

    def test_inverter_ac_capacity_bounds_the_discharge(self):
        """A 3 kW inverter passes at most 3 kWh of the 4 kWh excess."""
        inverter, battery = _devices(inverter_power_w=3000.0, battery_power_w=10000.0)

        _, grid_import, _, _ = inverter.process_energy(0.0, 12000.0, 0)

        assert grid_import == pytest.approx(9000.0)
        assert battery.soc_wh == pytest.approx(7000.0)

    def test_released_battery_discharges_as_before(self):
        """A released battery serves the whole load up to its power, no extra."""
        limited, limited_battery = _devices(battery_power_w=5000.0)
        unlimited, unlimited_battery = _devices(battery_power_w=5000.0, limit_w=None)
        limited_battery.discharge_array[0] = 1
        unlimited_battery.discharge_array[0] = 1

        result = limited.process_energy(0.0, 12000.0, 0)
        reference = unlimited.process_energy(0.0, 12000.0, 0)

        assert result == reference
        assert result[1] == pytest.approx(7000.0)
        assert limited_battery.soc_wh == unlimited_battery.soc_wh == pytest.approx(5000.0)

    def test_released_battery_at_its_power_limit_is_not_discharged_twice(self):
        """The slot's discharge budget is shared: 3 kW battery, 12 kWh load."""
        inverter, battery = _devices(battery_power_w=3000.0)
        battery.discharge_array[0] = 1

        _, grid_import, _, _ = inverter.process_energy(0.0, 12000.0, 0)

        assert grid_import == pytest.approx(9000.0)
        assert battery.soc_wh == pytest.approx(7000.0)

    def test_other_battery_implementations_shave_too(self):
        """Batteries other than the plain Battery class discharge step by step."""

        class SubclassedBattery(Battery):
            pass

        inverter, battery = _devices(
            slot_duration_h=0.25,
            battery_power_w=2000.0,
            discharging_efficiency=0.9,
            dc_to_ac_efficiency=0.95,
        )
        subclassed = SubclassedBattery(battery.parameters, prediction_hours=SLOTS, slot_duration_h=0.25)
        subclassed.reset()
        inverter.battery = subclassed

        _, grid_import, losses, _ = inverter.process_energy(0.0, 3000.0, 0)

        assert grid_import == pytest.approx(3000.0 - 427.5)
        assert losses == pytest.approx(500.0 - 427.5)
        assert subclassed.soc_wh == pytest.approx(10000.0 - 500.0)

    def test_load_below_limit_keeps_held_battery_idle(self):
        inverter, battery = _devices()

        _, grid_import, _, _ = inverter.process_energy(0.0, 7000.0, 0)

        assert grid_import == pytest.approx(7000.0)
        assert battery.soc_wh == pytest.approx(10000.0)


# ---------------------------------------------------------------------------
# AC charging is reduced first
# ---------------------------------------------------------------------------


class TestAcChargeReduction:
    def test_ac_charge_fills_up_to_the_limit(self):
        """Load 6 kWh, limit 8 kWh: 2 kWh AC instead of 5000 / 0.95 = 5263 Wh.

        DC into the battery 2000 x 0.95 = 1900 Wh, stored 1900 x 0.9 = 1710 Wh.
        Losses: 100 Wh conversion + 190 Wh battery.
        """
        inverter, battery = _devices(
            soc_percentage=0, ac_to_dc_efficiency=0.95, charging_efficiency=0.9
        )

        grid, ac_wh, ac_losses, _ = _ac_slot(inverter, battery, load_wh=6000.0, factor=1.0)

        assert ac_wh == pytest.approx(2000.0)
        assert grid == pytest.approx(8000.0)
        assert battery.soc_wh == pytest.approx(1710.0)
        assert ac_losses == pytest.approx(2000.0 - 1710.0)

    def test_quarter_hour_ac_charge_fills_up_to_the_limit(self):
        """15 min: load 1500 Wh, limit 2000 Wh, full-power charge 1250 Wh -> 500 Wh."""
        inverter, battery = _devices(slot_duration_h=0.25, soc_percentage=0)

        grid, ac_wh, _, _ = _ac_slot(inverter, battery, load_wh=1500.0, factor=1.0)

        assert ac_wh == pytest.approx(500.0)
        assert grid == pytest.approx(2000.0)
        assert battery.soc_wh == pytest.approx(500.0)

    def test_load_above_limit_stops_ac_charge_and_discharges(self):
        """Load 9 kWh in an AC slot: no grid charging, the battery shaves 1 kWh."""
        inverter, battery = _devices(soc_percentage=50)

        grid, ac_wh, ac_losses, _ = _ac_slot(inverter, battery, load_wh=9000.0, factor=1.0)

        assert ac_wh == 0.0
        assert ac_losses == 0.0
        assert grid == pytest.approx(8000.0)
        assert battery.soc_wh == pytest.approx(5000.0 - 1000.0)

    def test_total_charge_model_is_capped_too(self):
        """Inverters whose AC setpoint caps the total charge respect the limit."""
        inverter, battery = _devices(soc_percentage=0, total_charge=True)

        grid, ac_wh, _, _ = _ac_slot(inverter, battery, load_wh=6000.0, factor=1.0)

        assert ac_wh == pytest.approx(2000.0)
        assert grid == pytest.approx(8000.0)

    def test_pv_charge_in_the_same_slot_counts_against_the_charge_budget(self):
        """PV took 3 kWh of the 5 kWh budget; the grid may add only 2 kWh of it.

        With a 1 kW limit the AC charge is capped further to 1 kWh.
        """
        inverter, battery = _devices(soc_percentage=0, limit_w=1000.0)

        grid, ac_wh, _, _ = _ac_slot(inverter, battery, load_wh=0.0, factor=1.0, pv_wh=3000.0)

        assert ac_wh == pytest.approx(1000.0)
        assert grid == pytest.approx(1000.0)
        assert battery.soc_wh == pytest.approx(4000.0)

    def test_limit_that_does_not_bind_changes_nothing(self):
        limited, limited_battery = _devices(soc_percentage=0, limit_w=1e9)
        unlimited, unlimited_battery = _devices(soc_percentage=0, limit_w=None)

        assert _ac_slot(limited, limited_battery, 2000.0, 1.0) == _ac_slot(
            unlimited, unlimited_battery, 2000.0, 1.0
        )
        assert limited_battery.soc_wh == unlimited_battery.soc_wh


# ---------------------------------------------------------------------------
# GeneticSimulation
# ---------------------------------------------------------------------------


def _simulate(
    config_eos: ConfigEOS,
    *,
    load: list[float],
    ac: Optional[list[float]] = None,
    discharge: Optional[list[float]] = None,
    ev_charge: Optional[list[float]] = None,
    pv: Optional[list[float]] = None,
    limit_w: Optional[float] = LIMIT_W,
    **device_kwargs: Any,
) -> tuple[dict[str, Any], GeneticSimulation]:
    config_eos.merge_settings_from_dict(
        {"prediction": {"hours": 48}, "optimization": {"genetic": {"horizon_hours": 24}}}
    )
    slots = len(load)
    inverter, battery = _devices(limit_w=limit_w, **device_kwargs)
    battery = Battery(battery.parameters, prediction_hours=slots)
    inverter = Inverter(inverter.parameters, battery=battery)
    ev = None
    if ev_charge is not None:
        ev = Battery(
            ElectricVehicleParameters(
                device_id="ev1",
                capacity_wh=60000,
                charging_efficiency=1.0,
                discharging_efficiency=1.0,
                max_charge_power_w=7000,
                initial_soc_percentage=0,
                min_soc_percentage=100,
            ),
            prediction_hours=slots,
        )
    sim = GeneticSimulation()
    sim.prepare(
        GeneticEnergyManagementParameters(
            pv_forecast_wh=pv or [0.0] * slots,
            electricity_price_per_wh=[0.0003] * slots,
            feed_in_tariff_per_wh=0.00008,
            price_per_wh_battery=0.0,
            total_load=load,
        ),
        optimization_hours=slots,
        prediction_hours=slots,
        inverter=inverter,
        ev=ev,
    )
    sim.reset()
    sim.ac_charge_hours = np.array(ac or [0.0] * slots, dtype=float)
    sim.dc_charge_hours = np.ones(slots)
    sim.bat_discharge_hours = np.array(discharge or [0.0] * slots, dtype=float)
    if ev_charge is not None:
        sim.ev_charge_hours = np.array(ev_charge, dtype=float)
    return sim.simulate(0), sim


class TestGeneticSimulation:
    def test_ac_slot_with_peak_load_imports_at_most_the_limit(self, config_eos):
        """Slot 1 plans AC charging during a 9 kWh load: no charge, 1 kWh shaved."""
        result, sim = _simulate(
            config_eos,
            load=[1000.0, 9000.0, 1000.0],
            ac=[0.0, 1.0, 0.0],
            soc_percentage=50,
        )

        np.testing.assert_allclose(result["Netzbezug_Wh_pro_Stunde"], [1000.0, 8000.0, 1000.0])
        assert result["akku_soc_pro_stunde"][2] == pytest.approx(40.0)
        assert result["Kosten_Euro_pro_Stunde"][1] == pytest.approx(8000.0 * 0.0003)
        # The reported plan shows the AC charge that was simulated: none.
        assert sim.ac_charge_hours is not None
        assert list(sim.ac_charge_hours) == [0.0, 0.0, 0.0]

    def test_ac_charge_is_filled_up_to_the_limit(self, config_eos):
        result, sim = _simulate(
            config_eos, load=[6000.0, 1000.0], ac=[1.0, 0.0], soc_percentage=0
        )

        np.testing.assert_allclose(result["Netzbezug_Wh_pro_Stunde"], [8000.0, 1000.0])
        assert result["akku_soc_pro_stunde"][1] == pytest.approx(20.0)
        # 2000 Wh of the 5000 Wh charge budget: the reported factor is 0.4.
        assert sim.ac_charge_hours is not None
        assert sim.ac_charge_hours[0] == pytest.approx(0.4)

    def test_total_charge_model_reports_the_executed_total_factor(self, config_eos):
        """PV 1 kWh plus 2 kWh grid within a 2 kW limit: total 3 of 5 kWh -> 0.6."""
        result, sim = _simulate(
            config_eos,
            load=[0.0, 1000.0],
            ac=[1.0, 0.0],
            soc_percentage=0,
            total_charge=True,
            pv=[1000.0, 0.0],
            limit_w=2000.0,
        )

        assert result["Netzbezug_Wh_pro_Stunde"][0] == pytest.approx(2000.0)
        assert result["akku_soc_pro_stunde"][1] == pytest.approx(30.0)
        assert sim.ac_charge_hours is not None
        assert sim.ac_charge_hours[0] == pytest.approx(0.6)

    def test_reported_factor_is_kept_when_the_limit_does_not_bind(self, config_eos):
        _, sim = _simulate(config_eos, load=[1000.0, 1000.0], ac=[0.5, 0.0], soc_percentage=0)

        assert sim.ac_charge_hours is not None
        assert list(sim.ac_charge_hours) == [0.5, 0.0]

    def test_ev_charging_counts_towards_the_limit(self, config_eos):
        """Load 4 kWh plus 7 kWh EV charging: the battery shaves 3 kWh."""
        result, sim = _simulate(
            config_eos, load=[4000.0, 1000.0], ev_charge=[1.0, 0.0], soc_percentage=100
        )

        assert result["Netzbezug_Wh_pro_Stunde"][0] == pytest.approx(8000.0)
        assert sim.battery is not None
        assert result["akku_soc_pro_stunde"][1] == pytest.approx(70.0)

    def test_without_limit_the_load_is_imported(self, config_eos):
        result, _ = _simulate(
            config_eos,
            load=[1000.0, 9000.0, 1000.0],
            ac=[0.0, 1.0, 0.0],
            soc_percentage=50,
            limit_w=None,
        )

        assert result["Netzbezug_Wh_pro_Stunde"][1] == pytest.approx(9000.0 + 5000.0)


# ---------------------------------------------------------------------------
# Fitness penalty
# ---------------------------------------------------------------------------


def _fitness(
    config_eos: ConfigEOS,
    simulation_result: dict[str, Any],
    inverter: Optional[Inverter],
    penalties: Optional[dict[str, Any]] = None,
) -> float:
    """Evaluate a fixed simulation result; only the penalties are added."""
    config_eos.optimization.genetic.penalties = penalties or {
        "ev_soc_miss": 10,
        "ac_charge_break_even": 1.0,
    }
    optimizer = GeneticOptimization.__new__(GeneticOptimization)
    optimizer.optimize_ev = False
    optimizer.simulation = cast(
        GeneticSimulation, SimpleNamespace(battery=None, inverter=inverter, ev=None)
    )

    class _Individual(list):
        pass

    with patch.object(optimizer, "evaluate_inner", return_value=simulation_result):
        fitness = optimizer.evaluate(
            _Individual([0] * 4),
            parameters=Mock(ems=Mock(price_per_wh_battery=0.0), ev=None),
            start_hour=0,
            worst_case=False,
        )
    return fitness[0]


def _result(grid_import: list[float], balance: float = 1.5) -> dict[str, Any]:
    return {
        "Gesamtbilanz_Euro": balance,
        "Gesamt_Verluste": 0.0,
        "Netzbezug_Wh_pro_Stunde": np.array(grid_import, dtype=float),
    }


class TestFitnessPenalty:
    def test_excess_import_is_penalised_per_kwh(self, config_eos):
        """15 min, 8 kW: excess 1000 + 0 + 572.5 Wh at the default 10 EUR/kWh."""
        inverter, _ = _devices(slot_duration_h=0.25)

        fitness = _fitness(config_eos, _result([3000.0, 1500.0, 2572.5]), inverter)

        assert fitness == pytest.approx(1.5 + 1.5725 * 10.0)

    def test_penalty_is_configurable(self, config_eos):
        inverter, _ = _devices(slot_duration_h=0.25)

        fitness = _fitness(
            config_eos,
            _result([3000.0, 1500.0, 2572.5]),
            inverter,
            penalties={"grid_import_limit_excess": 2.0},
        )

        assert fitness == pytest.approx(1.5 + 1.5725 * 2.0)

    def test_no_penalty_without_limit_or_without_excess(self, config_eos):
        unlimited, _ = _devices(limit_w=None)
        limited, _ = _devices()

        assert _fitness(config_eos, _result([12000.0, 9000.0]), unlimited) == 1.5
        assert _fitness(config_eos, _result([8000.0, 7999.0]), limited) == 1.5

    def test_penalty_follows_the_energy_the_battery_delivered(self, config_eos):
        """A power-limited battery shaves 427.5 Wh of 1000 Wh: 572.5 Wh are penalised."""
        result, sim = _simulate(
            config_eos,
            load=[3000.0],
            slot_duration_h=1.0,
            limit_w=2000.0,
            battery_power_w=500.0,
            discharging_efficiency=0.9,
            dc_to_ac_efficiency=0.95,
        )
        assert result["Netzbezug_Wh_pro_Stunde"][0] == pytest.approx(3000.0 - 427.5)

        fitness = _fitness(config_eos, result, sim.inverter)

        assert fitness == pytest.approx(result["Gesamtbilanz_Euro"] + 0.5725 * 10.0)


# ---------------------------------------------------------------------------
# Tail lookahead
# ---------------------------------------------------------------------------


class TestTail:
    def test_tail_action_respects_the_limit(self):
        inverter, battery = _devices(soc_percentage=100)
        battery.charge_array = np.zeros(1)
        battery.discharge_array = np.zeros(1, dtype=np.int_)

        hold = _simulate_action(
            bat=battery,
            inv=inverter,
            energy_wh=10000.0,
            action=(1, 0, 0.0, 0.0),
            price=0.0003,
            load=12000.0,
            pv=0.0,
            tariff=0.0,
            direct_marketing=False,
        )
        charge = _simulate_action(
            bat=battery,
            inv=inverter,
            energy_wh=0.0,
            action=(1, 0, 1.0, 0.0),
            price=0.0003,
            load=6000.0,
            pv=0.0,
            tariff=0.0,
            direct_marketing=False,
        )

        assert hold["grid_import_wh"] == pytest.approx(8000.0)
        assert hold["next_state_wh"] == pytest.approx(6000.0)
        assert charge["ac_grid_charge_wh"] == pytest.approx(2000.0)
        assert charge["grid_import_wh"] == pytest.approx(8000.0)

    @staticmethod
    def _curve(limit_w: Optional[float], penalty_euro_per_wh: float):
        inverter, battery = _devices(limit_w=limit_w)
        return build_tail_value_curve(
            battery=battery,
            inverter=inverter,
            prices_euro_per_wh=np.array([0.0003]),
            load_wh=np.array([12000.0]),
            pv_wh=np.array([0.0]),
            feed_in_euro_per_wh=np.array([0.0]),
            continuation=TerminalValueCurve(),
            charge_rates=[1.0],
            export_rates=[1.0],
            direct_marketing=False,
            grid_import_penalty_euro_per_wh=penalty_euro_per_wh,
        )

    def test_tail_values_reserve_for_a_peak(self):
        """One 12 kWh tail hour, 8 kW limit, 0.01 EUR/Wh penalty.

        Empty: 12 kWh at 0.3 EUR/kWh plus 4 kWh excess = -3.6 - 40.
        2 kWh stored: 10 kWh import, 2 kWh excess = -3.0 - 20.
        Full: discharge 5 kWh (battery power), 7 kWh import = -2.1.
        """
        limited = self._curve(LIMIT_W, 0.01)
        unlimited = self._curve(None, 0.01)

        assert limited.value(0.0) == pytest.approx(-43.6)
        assert limited.value(2000.0) == pytest.approx(-23.0)
        assert limited.value(10000.0) == pytest.approx(-2.1)
        assert unlimited.value(0.0) == pytest.approx(-3.6)
        assert unlimited.value(10000.0) == pytest.approx(-2.1)

    def test_tail_plan_reports_the_penalised_slot(self):
        plan = self._curve(LIMIT_W, 0.01).diagnostic_plan(0.0, 0.0)

        assert plan[0].grid_import_wh == pytest.approx(12000.0)
        assert plan[0].slot_value_euro == pytest.approx(-43.6)


# ---------------------------------------------------------------------------
# Configuration and request path
# ---------------------------------------------------------------------------


class TestConfiguration:
    def test_device_setting_reaches_the_genetic_parameters(self):
        settings = InverterCommonSettings(
            device_id="inverter1", max_power_w=10000, max_grid_import_power_w=8000
        )

        assert settings.to_genetic_param().max_grid_import_power_w == 8000

    def test_default_is_unlimited(self):
        settings = InverterCommonSettings(device_id="inverter1", max_power_w=10000)

        assert settings.max_grid_import_power_w is None
        assert settings.to_genetic_param().max_grid_import_power_w is None
        assert InverterParameters(device_id="i", max_power_wh=1).max_grid_import_power_w is None

    @pytest.mark.parametrize("value", [-1.0, float("nan")])
    def test_invalid_values_are_rejected(self, value):
        with pytest.raises(ValidationError):
            InverterParameters(device_id="i", max_power_wh=1, max_grid_import_power_w=value)
        with pytest.raises(ValidationError):
            InverterCommonSettings(device_id="i", max_power_w=1, max_grid_import_power_w=value)

    def test_optimize_request_accepts_the_limit(self):
        parameters = GeneticOptimizationParameters.model_validate(
            {
                "ems": {
                    "pv_forecast_wh": [0.0],
                    "electricity_price_per_wh": [0.0003],
                    "feed_in_tariff_per_wh": 0.0,
                    "price_per_wh_battery": 0.0,
                    "total_load": [0.0],
                },
                "pv_battery": None,
                "ev": None,
                "inverter": {
                    "device_id": "inverter1",
                    "max_power_wh": 10000,
                    "max_grid_import_power_w": 8000,
                },
            }
        )

        assert parameters.inverter is not None
        assert parameters.inverter.max_grid_import_power_w == 8000


# ---------------------------------------------------------------------------
# Real optimizer runs
# ---------------------------------------------------------------------------

BASE_SLOTS = 12
PEAK_SLOTS = 4


def _optimize(
    config_eos: ConfigEOS, limit_w: Optional[float], seed: int = 42, peak_in_tail: bool = False
):
    """Four quarter-hour hours: 1 kW base load, then one hour at 12 kW.

    Flat prices make grid charging a pure loss, so without a limit the
    optimizer leaves the empty battery alone and imports the 12 kW peak.
    With ``peak_in_tail`` the four hours are all base load and the peak hour
    follows in a four-hour tail.
    """
    config_eos.merge_settings_from_dict(
        {
            "prediction": {"hours": 48},
            "optimization": {
                "algorithm": "GENETIC",
                "genetic": {
                    "horizon_hours": 4,
                    "tail_horizon_hours": 4 if peak_in_tail else 0,
                    "interval_sec": 900,
                    "terminal_value_mode": "AUTO" if peak_in_tail else "FIXED",
                    "terminal_value_euro_per_kwh": 0.0,
                },
            },
        }
    )
    get_ems(init=True).set_start_datetime(to_datetime("2026-10-26T00:00:00"))
    if peak_in_tail:
        load = [250.0] * 16 + [3000.0] * PEAK_SLOTS + [250.0] * 12
    else:
        load = [250.0] * BASE_SLOTS + [3000.0] * PEAK_SLOTS
    slots = len(load)
    parameters = GeneticOptimizationParameters.model_validate(
        {
            "ems": {
                "pv_forecast_wh": [0.0] * slots,
                "total_load": load,
                "electricity_price_per_wh": [0.0003] * slots,
                "feed_in_tariff_per_wh": 0.00008,
                "price_per_wh_battery": 0.0,
            },
            "pv_battery": {
                "device_id": "battery1",
                "capacity_wh": 10000,
                "max_charge_power_w": 5000,
                "charging_efficiency": 0.95,
                "discharging_efficiency": 0.95,
                "initial_soc_percentage": 0,
            },
            "inverter": {
                "device_id": "inverter1",
                "max_power_wh": 10000,
                "battery_id": "battery1",
                "ac_to_dc_efficiency": 0.95,
                "dc_to_ac_efficiency": 0.95,
                "max_grid_import_power_w": limit_w,
            },
            "ev": None,
            "forecast_interval_seconds": 900,
        }
    )
    optimizer = GeneticOptimization(fixed_seed=seed)
    return optimizer.optimize_ems(parameters, ngen=40, individuals=40)


class TestOptimization:
    def test_plan_charges_before_the_peak_and_stays_within_the_limit(self, config_eos):
        unlimited = _optimize(config_eos, None)
        limited = _optimize(config_eos, LIMIT_W)

        unlimited_import = unlimited.result.grid_consumption_wh_per_hour
        limited_import = limited.result.grid_consumption_wh_per_hour
        assert max(unlimited_import) == pytest.approx(3000.0)
        assert max(limited_import) <= LIMIT_W * 0.25 + 1e-6
        assert sum(limited.ac_charge[:BASE_SLOTS]) > 0.0

    def test_plan_keeps_reserve_for_a_peak_beyond_the_control_horizon(self, config_eos):
        """The peak lies in the tail: only the tail penalty makes charging worth it."""
        unlimited = _optimize(config_eos, None, peak_in_tail=True)
        limited = _optimize(config_eos, LIMIT_W, peak_in_tail=True)

        assert unlimited.result.battery_soc_per_hour[-1] == pytest.approx(0.0)
        assert sum(unlimited.ac_charge) == 0.0
        # Shaving 4 x 1000 Wh AC needs 4000 / 0.95 / 0.95 = 4432 Wh stored.
        assert limited.terminal_value.battery_energy_wh >= 4000.0 - 1e-6
        assert sum(limited.ac_charge) > 0.0

    def test_limit_that_never_binds_gives_the_identical_plan(self, config_eos):
        """None and a limit above every import give bit-identical results."""
        unlimited = _optimize(config_eos, None)
        never_binding = _optimize(config_eos, 1e9)

        assert never_binding.ac_charge == unlimited.ac_charge
        assert never_binding.discharge_allowed == unlimited.discharge_allowed
        assert never_binding.start_solution == unlimited.start_solution
        assert never_binding.result.model_dump() == unlimited.result.model_dump()
        assert never_binding.fitness_history == unlimited.fitness_history
