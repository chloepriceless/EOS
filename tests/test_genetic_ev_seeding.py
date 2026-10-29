"""EV charging in GENETIC: seeding heuristic and energy-preserving shift mutation."""

import random
from typing import Any

import pytest
from deap import creator

from akkudoktoreos.config.config import ConfigEOS
from akkudoktoreos.core.coreabc import get_ems
from akkudoktoreos.optimization.genetic.genetic import GeneticOptimization
from akkudoktoreos.optimization.genetic.geneticparams import (
    GeneticOptimizationParameters,
)
from akkudoktoreos.optimization.genetic.geneticsolution import GeneticSolution
from akkudoktoreos.utils.datetimeutil import to_datetime

HOURS = 6
SLOTS = HOURS * 4
EXPENSIVE = 0.0004
CHEAP = 0.0001
CHEAP_SLOTS = list(range(16, 21))


def configure(config: ConfigEOS) -> None:
    config.merge_settings_from_dict(
        {
            "prediction": {"hours": 48},
            "optimization": {
                "algorithm": "GENETIC",
                "genetic": {
                    "horizon_hours": HOURS,
                    "tail_horizon_hours": 0,
                    "interval_sec": 900,
                    "terminal_value_mode": "FIXED",
                },
            },
            "feedintariff": {"direct_marketing_enabled": False},
        }
    )
    get_ems(init=True).set_start_datetime(to_datetime("2026-10-29T00:00:00"))


def parameters(**ev: Any) -> GeneticOptimizationParameters:
    """Expensive grid except for five cheap slots late in the horizon.

    The EV needs exactly five full-power slots (10 kWh to 50 %, 1 kWh per slot).
    """
    prices = [EXPENSIVE] * SLOTS
    for slot in CHEAP_SLOTS:
        prices[slot] = CHEAP
    return GeneticOptimizationParameters.model_validate(
        {
            "ems": {
                "pv_prognose_wh": [0.0] * SLOTS,
                "gesamtlast": [0.0] * SLOTS,
                "strompreis_euro_pro_wh": prices,
                "einspeiseverguetung_euro_pro_wh": [0.0] * SLOTS,
                "preis_euro_pro_wh_akku": 0.0,
            },
            "inverter": {"device_id": "inv", "max_power_wh": 10000},
            "forecast_interval_seconds": 900,
            "pv_battery": None,
            "ev": {
                "device_id": "car",
                "capacity_wh": 10000,
                "initial_soc_percentage": 0,
                "min_soc_percentage": 50,
                "max_soc_percentage": 100,
                "charging_efficiency": 1.0,
                "max_charge_power_w": 4000,
                "charge_rates": [0.0, 0.5, 1.0],
                **ev,
            },
        }
    )


def run(params: GeneticOptimizationParameters) -> tuple[GeneticOptimization, GeneticSolution]:
    optimizer = GeneticOptimization(fixed_seed=7)
    solution = optimizer.optimize_ems(params, ngen=1, individuals=12)
    return optimizer, solution


def stored_wh(optimizer: GeneticOptimization, ev_genes: list[int]) -> float:
    ev = optimizer.simulation.ev
    assert ev is not None
    return sum(
        optimizer.ev_possible_charge_values[gene]
        * ev.max_charge_power_w
        * optimizer.slot_duration_h
        * ev.charging_efficiency
        for gene in ev_genes
    )


def test_ev_seed_schedules_the_target_energy_in_the_cheapest_slots(config_eos: ConfigEOS) -> None:
    """The EV Battery has no minimum SoC; the seed must use the requested target."""
    configure(config_eos)
    optimizer, _ = run(parameters())
    schedule = optimizer._heuristic_ev_schedule(prefer_pv=False)

    assert len(schedule) == SLOTS
    assert stored_wh(optimizer, schedule) == pytest.approx(5000.0)
    charging = [slot for slot, gene in enumerate(schedule) if gene != 0]
    assert charging == CHEAP_SLOTS


def test_ev_seed_does_not_plan_beyond_the_maximum_soc(config_eos: ConfigEOS) -> None:
    configure(config_eos)
    optimizer, _ = run(parameters(min_soc_percentage=50, max_soc_percentage=30))
    schedule = optimizer._heuristic_ev_schedule(prefer_pv=False)

    assert stored_wh(optimizer, schedule) == pytest.approx(3000.0)


def test_small_run_charges_the_ev_only_in_the_cheap_slots(config_eos: ConfigEOS) -> None:
    """With a working seed even a one-generation run finds the cheap window."""
    configure(config_eos)
    _, solution = run(parameters())

    assert solution.ev_charge_hours_float is not None
    charging = [slot for slot, rate in enumerate(solution.ev_charge_hours_float) if rate > 0.0]
    assert charging == CHEAP_SLOTS
    assert solution.result.ev_soc_per_hour[-1] >= 50.0


def ev_window(optimizer: GeneticOptimization) -> range:
    end = optimizer.control_end_slot - optimizer.fixed_eauto_hours
    if optimizer._ev_soc_deadline_slot is not None:
        end = min(end, optimizer._ev_soc_deadline_slot)
    return range(optimizer._control_start_slot(), end)


@pytest.mark.parametrize("deadline", [None, "2026-10-29T04:00:00"])
def test_ev_shift_mutation_moves_charging_without_changing_its_energy(
    config_eos: ConfigEOS, deadline: Any
) -> None:
    configure(config_eos)
    optimizer, _ = run(parameters(min_soc_deadline_datetime=deadline))
    n = optimizer.control_end_slot
    window = ev_window(optimizer)
    if deadline is not None:
        assert window.stop == 16

    rng = random.Random(3)
    battery = [rng.randrange(optimizer._battery_state_layout().total_states) for _ in range(n)]
    ev_genes = [rng.choice([0, 0, 0, 1, 2]) for _ in range(n)]
    individual = creator.Individual(battery + ev_genes)

    random.seed(11)
    moved = 0
    for _ in range(300):
        before = list(individual)
        if optimizer._mutate_ev_shift(individual):
            moved += 1
            assert individual != before
        else:
            assert individual == before
        # The battery genes and the EV genes outside the window stay as they are.
        assert individual[:n] == battery
        outside = [slot for slot in range(n) if slot not in window]
        assert [individual[n + slot] for slot in outside] == [ev_genes[slot] for slot in outside]
        # Only the order changes, so the planned energy stays the same.
        assert sorted(individual[n + slot] for slot in window) == sorted(
            ev_genes[slot] for slot in window
        )
    assert moved > 200


def test_ev_shift_mutation_without_planned_charging_changes_nothing(config_eos: ConfigEOS) -> None:
    configure(config_eos)
    optimizer, _ = run(parameters())
    n = optimizer.control_end_slot
    individual = creator.Individual([0] * (2 * n))

    assert not optimizer._mutate_ev_shift(individual)
    assert individual == [0] * (2 * n)


def test_ev_shift_mutation_ignores_genes_outside_the_rate_list(config_eos: ConfigEOS) -> None:
    """A warm start from a run with more charge rates must not crash a mutation."""
    configure(config_eos)
    optimizer, _ = run(parameters())
    n = optimizer.control_end_slot
    individual = creator.Individual([0] * n + [10] * n)

    random.seed(5)
    assert not optimizer._mutate_ev_shift(individual)
    assert individual == [0] * n + [10] * n
    for _ in range(50):
        optimizer._mutate_ev_controls(individual)


def test_out_of_range_warm_start_does_not_abort_the_run(config_eos: ConfigEOS) -> None:
    configure(config_eos)
    params = parameters()
    params.start_solution = [0.0] * SLOTS + [10.0] * SLOTS
    _, solution = run(params)

    assert solution.ev_charge_hours_float is not None
