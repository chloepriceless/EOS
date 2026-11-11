//! `GeneticSimulation.simulate` and the evaluation of a genome
//! (`GeneticOptimization.evaluate_inner`, `_repair_ev_charge_at_full_soc`, `_evaluate_uncached`).
//!
//! An [`Evaluator`] holds the device states and caches of an evaluation; there is one per
//! compute thread. The data that stays constant during a run lives in [`RunConst`].

use crate::devices::battery::{Battery, BatterySpec, SimError};
use crate::devices::homeappliance::{ApplianceGene, HomeAppliance, StartDecoder};
use crate::devices::inverter::{GRID_IMPORT_LIMIT_TOLERANCE_WH, Inverter};
use crate::numpy::{self, DotKernel, NumericProfile, py_max, py_min};
use crate::selfconsumption::SelfConsumption;

/// Fitness on an error in the simulation (`except Exception: return (100000.0,)`).
pub const FAILED_FITNESS: f64 = 100000.0;

/// Decoding of a battery state (`decode_charge_discharge`).
#[derive(Debug, Clone, Default)]
pub struct DecodeTable {
    pub ac: Vec<f64>,
    pub dc: Vec<f64>,
    pub dis: Vec<f64>,
    pub exp: Vec<f64>,
    /// States outside the table (never produced by the optimizer): like `_decode_states`.
    pub outside_dc: f64,
}

impl DecodeTable {
    #[inline]
    pub fn get(&self, state: i64) -> (f64, f64, f64, f64) {
        if state >= 0 && (state as usize) < self.ac.len() {
            let s = state as usize;
            (self.ac[s], self.dc[s], self.dis[s], self.exp[s])
        } else {
            (0.0, self.outside_dc, 0.0, 0.0)
        }
    }
}

/// Terminal value of the energy in the battery at the end of the horizon.
#[derive(Debug, Clone)]
pub enum Terminal {
    /// Fixed price per Wh (`price_per_wh_battery`).
    Fixed(f64),
    /// `TerminalValueCurve` (AUTO): 0 for energy ≤ 0.
    Auto { x: Vec<f64>, y: Vec<f64> },
    /// `TailValueCurve` (TAIL).
    Tail { x: Vec<f64>, y: Vec<f64> },
}

#[derive(Debug, Clone)]
pub struct BreakEven {
    pub rt_eff: f64,
    pub factor: f64,
    pub best_prices: Vec<f64>,
    pub n: usize,
    pub lcos_per_wh_dc: f64,
}

#[derive(Debug, Clone)]
pub struct FullCharge {
    pub deadline_slot: i64,
    pub target: f64,
    pub penalty: f64,
}

#[derive(Debug, Clone)]
pub struct EvPenalty {
    pub penalty: f64,
    /// `parameters.ev.min_soc_percentage`, also the target `_ev_soc_value_credit` starts at.
    pub min_soc: f64,
    pub deadline_slot: Option<i64>,
    /// `_ev_soc_deadline_share`: share of the deadline slot's charge before the deadline.
    pub deadline_share: f64,
    /// `_ev_soc_value_steps` (non-empty), credited above the target.
    pub soc_value_steps: Option<Vec<(f64, f64)>>,
}

impl EvPenalty {
    /// `_ev_soc_value_credit(soc_percentage)`: credit for the EV energy above the target.
    ///
    /// Each kWh between the target and a step's SoC earns that step's amount; energy below the
    /// target, above the last step or above the maximum SoC earns nothing.
    pub fn soc_value_credit(&self, ev: &Battery, soc_percentage: f64) -> f64 {
        let Some(steps) = self.soc_value_steps.as_deref() else {
            return 0.0;
        };
        let kwh_per_percent = ev.s.capacity_wh / 100.0 / 1000.0;
        let mut lower = self.min_soc;
        let top = py_min(soc_percentage, ev.s.max_soc_percentage);
        let mut credit = 0.0;
        for &(step_soc, amount) in steps {
            if lower >= top {
                break;
            }
            let upper = py_min(step_soc, top);
            if upper > lower {
                credit += (upper - lower) * kwh_per_percent * amount;
            }
            lower = py_max(lower, step_soc);
        }
        credit
    }
}

/// Everything an evaluation needs that does not change during the run.
#[derive(Debug, Clone)]
pub struct RunConst {
    pub ce: usize,
    pub slot_h: f64,
    pub load: Vec<f64>,
    pub pv: Vec<f64>,
    pub price: Vec<f64>,
    pub feed_in: Vec<f64>,
    pub direct_marketing: bool,
    pub optimize_ev: bool,
    pub optimize_dc_charge: bool,
    pub decode: DecodeTable,
    pub ev_rates: Vec<f64>,
    pub ev_zero_index: Option<usize>,
    /// EV charge rates the charger accepts, highest first, with 0.0 (`ev_rates_desc` of
    /// `simulate`): with a grid import limit, EV charging that does not fit is lowered to the
    /// next of them. From `ev_possible_charge_values` whenever the run has an EV.
    pub ev_limit_rates_desc: Vec<f64>,
    /// The inverter scaled to the two parts of a partly passed slot 0 (without and with the EV,
    /// `_split_ev_slot_energy`): only with a grid import limit, an EV and a run inside slot 0.
    pub ev_split_inverters: Option<(Inverter, Inverter)>,
    pub ev_first_charge_slot: usize,
    pub ev_charge_start_slot: usize,
    pub battery: Option<BatterySpec>,
    pub ev: Option<BatterySpec>,
    pub inverter: Option<Inverter>,
    pub appliances: Vec<HomeAppliance>,
    pub layout: Vec<ApplianceGene>,
    pub worst_case: bool,
    pub terminal: Terminal,
    pub break_even: Option<BreakEven>,
    pub full_charge: Option<FullCharge>,
    pub grid_penalty_per_wh: f64,
    pub ev_penalty: Option<EvPenalty>,
    /// `parameters.ev.min_soc_percentage`, if the EV parameters and the simulation have an EV.
    pub ev_extra_min_soc: Option<f64>,
    pub numeric: NumericProfile,
}

impl RunConst {
    pub fn genome_len(&self) -> usize {
        self.ce * if self.optimize_ev { 2 } else { 1 } + self.layout.len()
    }
}

/// Result of a simulation (`simulate()`); the series only if `record` was set.
#[derive(Debug, Clone, Default)]
pub struct SimResult {
    pub total_cost: f64,
    pub total_revenue: f64,
    pub total_losses: f64,
    pub load: Vec<f64>,
    pub feedin: Vec<f64>,
    pub import: Vec<f64>,
    pub costs: Vec<f64>,
    pub soc: Vec<f64>,
    pub revenue: Vec<f64>,
    pub ev_soc: Vec<f64>,
    pub losses: Vec<f64>,
    pub ha: Vec<f64>,
    pub price: Vec<f64>,
    pub tariff: Vec<f64>,
    /// `grid_limited_ac_charge_wh` (only with a grid import limit): slot → AC energy.
    pub grid_limited: Option<Vec<(usize, f64)>>,
    /// `peak_shaving_wh_per_hour` (only with a grid import limit).
    pub peak_shaving: Option<Vec<f64>>,
    /// `ev_grid_limited_charge_factors` (only with a grid import limit and an EV): slot → the
    /// EV charge factor executed where the limit lowered it.
    pub ev_grid_limited: Option<Vec<(usize, f64)>>,
}

impl SimResult {
    pub fn gesamtbilanz(&self) -> f64 {
        self.total_cost - self.total_revenue
    }
}

/// Result of an evaluation.
#[derive(Debug, Clone)]
pub struct Evaluation {
    pub fitness: f64,
    /// `extra_data` (total balance, losses, EV constraint); `None` on error.
    pub extra: Option<(f64, f64, f64)>,
}

/// Evaluation state (device states, caches, buffers).
pub struct Evaluator {
    pub bat: Option<Battery>,
    pub ev: Option<Battery>,
    pub sc: SelfConsumption,
    decoder: StartDecoder,
    appliance_cache: crate::numpy::FxHashMap<Vec<i64>, (Vec<i64>, Vec<Vec<usize>>, Vec<f64>)>,
    /// Control series of the last simulation (`simulation.ac_charge_hours` etc.).
    pub ac: Vec<f64>,
    pub dc: Vec<f64>,
    pub dis: Vec<f64>,
    pub exp: Vec<f64>,
    pub ev_charge: Vec<f64>,
    ac_read: Vec<f64>,
    ev_read: Vec<f64>,
    app_load: Vec<f64>,
    pub app_starts: Vec<Vec<usize>>,
    has_app_load: bool,
    // Result series (always filled, small)
    pub res: SimResult,
    pub evaluations: u64,
    scratch: Vec<f64>,
}

impl Evaluator {
    pub fn new(rc: &RunConst) -> Evaluator {
        let ce = rc.ce;
        Evaluator {
            bat: rc.battery.clone().map(|s| Battery::new(s, ce)),
            ev: rc.ev.clone().map(|s| Battery::new(s, ce)),
            sc: SelfConsumption::new(rc.numeric.dot),
            decoder: StartDecoder::default(),
            appliance_cache: Default::default(),
            ac: vec![0.0; ce],
            dc: vec![0.0; ce],
            dis: vec![0.0; ce],
            exp: vec![0.0; ce],
            ev_charge: vec![0.0; ce],
            ac_read: vec![0.0; ce],
            ev_read: vec![0.0; ce],
            app_load: vec![0.0; ce],
            app_starts: Vec::new(),
            has_app_load: false,
            res: SimResult {
                load: vec![0.0; ce],
                feedin: vec![0.0; ce],
                import: vec![0.0; ce],
                costs: vec![0.0; ce],
                soc: vec![0.0; ce],
                revenue: vec![0.0; ce],
                ev_soc: vec![0.0; ce],
                losses: vec![0.0; ce],
                ha: vec![0.0; ce],
                price: vec![0.0; ce],
                tariff: vec![0.0; ce],
                ..SimResult::default()
            },
            evaluations: 0,
            scratch: Vec::new(),
        }
    }

    /// Order of the dot product for the self-consumption table.
    pub fn dot_kernel(rc: &RunConst) -> DotKernel {
        rc.numeric.dot
    }

    /// `_apply_appliance_starts`: canonicalize genes, build load curves (sum of all appliances).
    fn apply_appliance_starts(&mut self, rc: &RunConst, genes: &mut [i64]) -> Result<(), SimError> {
        if rc.appliances.is_empty() {
            self.has_app_load = false;
            return Ok(());
        }
        let key: Vec<i64> = genes.to_vec();
        if let Some((canon, starts, curve)) = self.appliance_cache.get(&key) {
            genes.copy_from_slice(canon);
            self.app_starts.clone_from(starts);
            self.app_load.copy_from_slice(curve);
            self.has_app_load = true;
            return Ok(());
        }
        let starts = self.decoder.decode(&rc.layout, &rc.appliances, genes, rc.slot_h, rc.ce).map_err(|_| SimError)?;
        let mut total = vec![0.0f64; rc.ce];
        for (i, a) in rc.appliances.iter().enumerate() {
            let curve = a.build_load_curve(&starts[i]).map_err(|_| SimError)?;
            for (t, l) in total.iter_mut().zip(curve.iter()) {
                *t += *l;
            }
        }
        self.app_load.copy_from_slice(&total);
        self.app_starts.clone_from(&starts);
        self.has_app_load = true;
        if self.appliance_cache.len() < 4096 {
            self.appliance_cache.insert(key, (genes.to_vec(), starts, total));
        }
        Ok(())
    }

    /// `evaluate_inner(individual)` → simulation; genes are written back in canonical form.
    pub fn evaluate_inner(&mut self, rc: &RunConst, g: &mut [i64]) -> Result<(), SimError> {
        let ce = rc.ce;
        if let Some(e) = self.ev.as_mut() {
            e.reset();
        }
        if let Some(b) = self.bat.as_mut() {
            b.reset();
        }
        // Appliance genes (end of the genome)
        let n_app = rc.layout.len();
        if n_app > 0 || !rc.appliances.is_empty() {
            let off = g.len() - n_app;
            let (_, tail) = g.split_at_mut(off);
            self.apply_appliance_starts(rc, tail)?;
        }
        // Battery states
        for i in 0..ce {
            let (a, d, s, x) = rc.decode.get(g[i]);
            self.ac[i] = a;
            self.dc[i] = if rc.optimize_dc_charge { d } else { 1.0 };
            self.dis[i] = s;
            self.exp[i] = x;
        }
        // EV
        if rc.optimize_ev {
            if let Some(zero) = rc.ev_zero_index {
                let first = rc.ev_first_charge_slot;
                if first > 0 {
                    for slot in 0..first.min(ce) {
                        g[ce + slot] = zero as i64;
                    }
                }
            }
            let n = rc.ev_rates.len() as i64;
            for i in 0..ce {
                let mut idx = g[ce + i];
                if idx < 0 {
                    idx += n;
                }
                if idx < 0 || idx >= n {
                    return Err(SimError);
                }
                self.ev_charge[i] = rc.ev_rates[idx as usize];
            }
        } else {
            self.ev_charge.iter_mut().for_each(|v| *v = 0.0);
        }
        self.simulate(rc)
    }

    /// `GeneticSimulation.simulate(start_hour=0)`.
    fn simulate(&mut self, rc: &RunConst) -> Result<(), SimError> {
        let ce = rc.ce;
        let end_hour = ce.min(rc.load.len());
        let dm = rc.direct_marketing;
        let inv = rc.inverter.as_ref();
        let mut ac_possible = false;
        if let Some(b) = self.bat.as_mut() {
            ac_possible = match inv {
                Some(i) => i.ac_charging_possible(),
                None => true,
            };
            if !ac_possible {
                self.ac.iter_mut().for_each(|v| *v = 0.0);
            }
            for i in end_hour..ce {
                self.dc[i] = 0.0;
                self.ac[i] = 0.0;
                self.dis[i] = 0.0;
                self.exp[i] = 0.0;
            }
            for i in 0..ce {
                b.charge_array[i] = if self.ac[i] != 0.0 { self.ac[i] } else { self.dc[i] };
                let fi = if i < rc.feed_in.len() { rc.feed_in[i] } else { 0.0 };
                let released = self.dis[i] > 0.0 || (dm && self.exp[i] > 0.0 && fi > 0.0);
                b.discharge_array[i] = if released { 1.0 } else { 0.0 };
            }
        }
        if let Some(e) = self.ev.as_mut() {
            for i in end_hour..ce {
                self.ev_charge[i] = 0.0;
            }
            let cs = rc.ev_charge_start_slot;
            if cs > 0 {
                for i in 0..cs.min(ce) {
                    self.ev_charge[i] = 0.0;
                }
            }
            e.charge_array.copy_from_slice(&self.ev_charge);
        }
        self.ac_read.copy_from_slice(&self.ac);
        self.ev_read.copy_from_slice(&self.ev_charge);
        let max_grid_import = inv.and_then(|i| i.max_grid_import_wh);
        let mut grid_limited: Option<Vec<(usize, f64)>> = max_grid_import.map(|_| Vec::new());
        let mut peak_shaving: Option<Vec<f64>> = max_grid_import.map(|_| {
            let mut v = self.res.peak_shaving.take().unwrap_or_default();
            v.clear();
            v.resize(end_hour, 0.0);
            v
        });
        // With a grid import limit, EV charging gives way to it: a slot whose import exceeds
        // the limit is simulated again at the next lower EV charge rate, down to no charging.
        let ev_limited = max_grid_import.is_some() && self.ev.is_some();
        let ev_limit_wh = match max_grid_import {
            Some(lim) if ev_limited => lim + GRID_IMPORT_LIMIT_TOLERANCE_WH,
            _ => 0.0,
        };
        let mut ev_grid_limited: Option<Vec<(usize, f64)>> = if ev_limited { Some(Vec::new()) } else { None };
        // A run inside slot 0 charges the EV only in the part still ahead; with a limit, slot 0
        // is then simulated as a part without and a part with the EV (`_split_ev_slot_energy`).
        let mut ev_first_slot_extra = 0.0;
        let mut ev_first_slot_share = 1.0;
        if ev_limited {
            if let Some(e) = self.ev.as_ref() {
                ev_first_slot_share = e.s.first_slot_share;
                if 0.0 < ev_first_slot_share && ev_first_slot_share < 1.0 {
                    ev_first_slot_extra = 1.0 / ev_first_slot_share - 1.0;
                }
            }
        }
        let has_bat = self.bat.is_some();
        let lcos = self.bat.as_ref().map(|b| (b.s.discharging_efficiency, b.s.levelized_cost_of_storage_kwh));

        for hour in 0..end_hour {
            let idx = hour;
            let mut consumption = rc.load[hour];
            let mut slot_losses = 0.0;
            if self.has_app_load {
                let ha_load = self.app_load[hour];
                consumption += ha_load;
                self.res.ha[idx] = ha_load;
            } else {
                self.res.ha[idx] = 0.0;
            }
            // EV state before charging in this slot, kept only where the grid import limit may
            // lower the EV charging of the slot.
            let mut ev_slot_state: Option<(f64, f64, f64)> = None;
            let mut consumption_without_ev = consumption;
            // Import the EV would add if it charged all of slot 0 [Wh].
            let mut ev_import_extra = 0.0;
            let mut ev_charge_factor = 0.0;
            if let Some(e) = self.ev.as_mut() {
                self.res.ev_soc[idx] = (e.soc_wh / e.s.capacity_wh) * 100.0;
                ev_charge_factor = self.ev_read[hour];
                if ev_charge_factor > 0.0 {
                    if ev_limited {
                        ev_slot_state = Some(e.slot_state(hour));
                        consumption_without_ev = consumption;
                    }
                    let (s, l) = e.charge_energy(None, hour, ev_charge_factor)?;
                    consumption += s + l;
                    slot_losses += l;
                    if hour == 0 {
                        ev_import_extra = (s + l) * ev_first_slot_extra;
                    }
                    if s + l <= 0.0 {
                        // Nothing charged (EV full): nothing to lower.
                        ev_slot_state = None;
                    }
                }
            } else {
                self.res.ev_soc[idx] = 0.0;
            }
            if let Some(b) = self.bat.as_ref() {
                self.res.soc[idx] = (b.soc_wh / b.s.capacity_wh) * 100.0;
            } else {
                self.res.soc[idx] = 0.0;
            }
            let mut feedin = 0.0;
            let mut grid_import = 0.0;
            let mut losses = 0.0;
            let mut limit_import = 0.0;
            let mut acf = 0.0;
            if has_bat && ac_possible {
                acf = self.ac_read[hour];
                if let Some(i) = inv {
                    if acf != 0.0 {
                        acf = i.ac_charge_factor(self.bat.as_ref(), acf);
                    }
                }
            }
            let tariff = rc.feed_in[hour];
            if let Some(i) = inv {
                if acf > 0.0 {
                    i.begin_ac_charge_slot(self.bat.as_mut(), hour, acf);
                }
                let produced = rc.pv[hour];
                let export_factor = self.exp[hour];
                let allowed = dm && tariff > 0.0 && export_factor > 0.0;
                // The import never exceeds the load: only a slot whose load exceeds the limit
                // can need its EV charging lowered.
                let mut battery_slot_state: Option<(f64, f64, f64)> = None;
                if ev_slot_state.is_some() && consumption + ev_import_extra > ev_limit_wh {
                    if let Some(b) = self.bat.as_ref() {
                        battery_slot_state = Some(b.slot_state(hour));
                    }
                } else {
                    ev_slot_state = None;
                }
                let mut peak;
                (feedin, grid_import, losses, peak, limit_import) = slot_energy(
                    i,
                    rc,
                    self.bat.as_mut(),
                    &mut self.sc,
                    hour,
                    produced,
                    consumption_without_ev,
                    consumption,
                    ev_import_extra,
                    ev_first_slot_share,
                    allowed,
                    export_factor,
                )?;
                if let (Some(state), Some(e)) = (ev_slot_state, self.ev.as_mut()) {
                    if limit_import > ev_limit_wh {
                        // The grid import limit wins against EV charging: the slot is simulated
                        // again at the next lower charge rate until it fits, or without EV
                        // charging.
                        for &ev_rate in rc.ev_limit_rates_desc.iter() {
                            if ev_rate >= ev_charge_factor {
                                continue;
                            }
                            if let (Some(bs), Some(b)) = (battery_slot_state, self.bat.as_mut()) {
                                b.restore_slot_state(hour, bs);
                            }
                            e.restore_slot_state(hour, state);
                            e.charge_array[hour] = ev_rate;
                            consumption = consumption_without_ev;
                            slot_losses = 0.0;
                            ev_import_extra = 0.0;
                            if ev_rate > 0.0 {
                                let (s, l) = e.charge_energy(None, hour, ev_rate)?;
                                consumption += s + l;
                                slot_losses += l;
                                if hour == 0 {
                                    ev_import_extra = (s + l) * ev_first_slot_extra;
                                }
                            }
                            (feedin, grid_import, losses, peak, limit_import) = slot_energy(
                                i,
                                rc,
                                self.bat.as_mut(),
                                &mut self.sc,
                                hour,
                                produced,
                                consumption_without_ev,
                                consumption,
                                ev_import_extra,
                                ev_first_slot_share,
                                allowed,
                                export_factor,
                            )?;
                            if limit_import <= ev_limit_wh {
                                break;
                            }
                        }
                        if let Some(gl) = ev_grid_limited.as_mut() {
                            gl.push((idx, e.charge_array[hour]));
                        }
                    }
                }
                if let Some(ps) = peak_shaving.as_mut() {
                    ps[idx] = peak;
                }
            }
            if acf > 0.0 && has_bat {
                let mut grid_limited_slot = false;
                let (ac_energy, ac_losses);
                {
                    let b = self.bat.as_mut().ok_or(SimError)?;
                    if let Some(i) = inv {
                        if max_grid_import.is_none() {
                            let (a, l, _) = i.charge_battery_from_grid_executed(b, hour, acf, None)?;
                            ac_energy = a;
                            ac_losses = l;
                        } else {
                            let (a, l, executed) =
                                i.charge_battery_from_grid_executed(b, hour, acf, Some(limit_import))?;
                            ac_energy = a;
                            ac_losses = l;
                            if executed < acf {
                                grid_limited_slot = true;
                                self.ac[hour] = executed;
                                if executed <= 0.0 && self.dc[hour] == 0.0 {
                                    self.dc[hour] = 1.0;
                                }
                            }
                        }
                    } else {
                        let (stored, l) = b.charge_energy(None, hour, acf)?;
                        ac_energy = stored + l;
                        ac_losses = l;
                    }
                }
                consumption += ac_energy;
                grid_import += ac_energy;
                slot_losses += ac_losses;
                if grid_limited_slot {
                    if let Some(gl) = grid_limited.as_mut() {
                        gl.push((idx, ac_energy));
                    }
                }
            }
            if dm && tariff < 0.0 && feedin > 0.0 {
                slot_losses += feedin;
                feedin = 0.0;
            }
            self.res.feedin[idx] = feedin;
            self.res.import[idx] = grid_import;
            self.res.losses[idx] = slot_losses + losses;
            self.res.load[idx] = consumption;
            let price = rc.price[hour];
            self.res.price[idx] = price;
            self.res.tariff[idx] = tariff;
            let grid_cost = grid_import * price;
            let lcos_cost = match (self.bat.as_ref(), lcos) {
                (Some(b), Some((eff, l))) => b.discharged[hour] * eff * l / 1000.0,
                _ => 0.0,
            };
            self.res.costs[idx] = grid_cost + lcos_cost;
            self.res.revenue[idx] = feedin * tariff;
        }
        let n = end_hour;
        self.res.total_cost = numpy::nansum(&self.res.costs[..n]);
        self.res.total_losses = numpy::nansum(&self.res.losses[..n]);
        self.res.total_revenue = numpy::nansum(&self.res.revenue[..n]);
        self.res.grid_limited = grid_limited;
        self.res.peak_shaving = peak_shaving;
        self.res.ev_grid_limited = ev_grid_limited;
        Ok(())
    }

    /// `_repair_ev_charge_at_full_soc`: true if a gene was changed.
    fn repair_ev(&self, rc: &RunConst, g: &mut [i64]) -> bool {
        if !rc.optimize_ev || rc.ev_rates.is_empty() {
            return false;
        }
        let Some(zero) = rc.ev_zero_index else {
            return false;
        };
        let ce = rc.ce;
        let result_slots = ce.min(rc.load.len()).min(ce);
        let mut changed = false;
        let n = rc.ev_rates.len() as i64;
        for offset in 0..result_slots {
            if self.res.ev_soc[offset] >= 100.0 - 1e-9 {
                let mut idx = g[ce + offset];
                if idx < 0 {
                    idx += n;
                }
                if idx >= 0 && idx < n && rc.ev_rates[idx as usize] > 0.0 {
                    g[ce + offset] = zero as i64;
                    changed = true;
                }
            }
        }
        changed
    }

    /// `_evaluate_uncached`: fitness of a genome (genes are canonicalized).
    pub fn evaluate(&mut self, rc: &RunConst, g: &mut [i64]) -> Evaluation {
        self.evaluations += 1;
        let ok = (|| -> Result<(), SimError> {
            self.evaluate_inner(rc, g)?;
            if self.repair_ev(rc, g) {
                self.evaluate_inner(rc, g)?;
            }
            Ok(())
        })();
        if ok.is_err() {
            return Evaluation { fitness: FAILED_FITNESS, extra: None };
        }
        let r = &self.res;
        let gesamtbilanz_euro = r.total_cost - r.total_revenue;
        let mut gesamtbilanz = gesamtbilanz_euro * if rc.worst_case { -1.0 } else { 1.0 };
        let ev_term = match (rc.ev_extra_min_soc, self.ev.as_ref()) {
            (Some(min_soc), Some(e)) => min_soc - e.current_soc_percentage(),
            _ => 0.0,
        };
        let extra = (gesamtbilanz_euro, r.total_losses, ev_term);
        if let Some(b) = self.bat.as_ref() {
            gesamtbilanz += -terminal_credit(rc, b);
        }
        if let (Some(be), Some(b), Some(inv)) = (rc.break_even.as_ref(), self.bat.as_ref(), rc.inverter.as_ref()) {
            let upper = self.ac.len().min(be.n);
            for hour in 0..upper {
                let ac_factor = self.ac[hour];
                if ac_factor <= 0.0 {
                    continue;
                }
                let charge_price = rc.price[hour];
                if charge_price <= 0.0 {
                    continue;
                }
                let break_even_price = charge_price / be.rt_eff + be.lcos_per_wh_dc / inv.reference_dc_to_ac_efficiency;
                let best_uncovered_price = be.best_prices[hour];
                if best_uncovered_price < break_even_price {
                    let limited =
                        r.grid_limited.as_ref().and_then(|gl| gl.iter().find(|(s, _)| *s == hour).map(|(_, v)| *v));
                    let ac_wh = match limited {
                        Some(v) => v,
                        None => {
                            let dc_wh = b.s.max_charge_power_w * rc.slot_h * ac_factor;
                            dc_wh / py_max(inv.ac_to_dc_efficiency, 1e-9)
                        }
                    };
                    let excess_cost_per_wh = break_even_price - best_uncovered_price;
                    gesamtbilanz += ac_wh * excess_cost_per_wh * be.factor;
                }
            }
        }
        if let (Some(fc), Some(b)) = (rc.full_charge.as_ref(), self.bat.as_ref()) {
            let reached =
                max_soc_until(&r.soc[..rc.ce.min(rc.load.len())], fc.deadline_slot, b.current_soc_percentage());
            if reached < fc.target {
                gesamtbilanz += (fc.target - reached) * fc.penalty;
            }
        }
        if let Some(lim) = rc.inverter.as_ref().and_then(|i| i.max_grid_import_wh) {
            let n = rc.ce.min(rc.load.len());
            let mut excess = std::mem::take(&mut self.scratch);
            excess.clear();
            excess.extend(r.import[..n].iter().map(|v| v - lim).filter(|d| *d > GRID_IMPORT_LIMIT_TOLERANCE_WH));
            let excess_wh = numpy::sum(&excess);
            self.scratch = excess;
            if excess_wh > 0.0 {
                gesamtbilanz += excess_wh * rc.grid_penalty_per_wh;
            }
        }
        if let (Some(evp), Some(e)) = (rc.ev_penalty.as_ref(), self.ev.as_ref()) {
            let total = rc.ce.min(rc.load.len()) as i64;
            // `_ev_soc_at_deadline`.
            let ev_soc = match evp.deadline_slot {
                None => e.current_soc_percentage(),
                Some(deadline) => {
                    let index = deadline;
                    if index >= total {
                        e.current_soc_percentage()
                    } else {
                        let mut soc = r.ev_soc[index.max(0) as usize];
                        let share = evp.deadline_share;
                        if share > 0.0 && index >= 0 {
                            // The SoC series holds the SoC at the start of each slot; after the
                            // last slot the vehicle holds its final SoC.
                            let soc_after = if index + 1 < total {
                                r.ev_soc[(index + 1) as usize]
                            } else {
                                e.current_soc_percentage()
                            };
                            soc += share * (soc_after - soc);
                        }
                        soc
                    }
                }
            };
            if ev_soc < evp.min_soc {
                gesamtbilanz += (evp.min_soc - ev_soc).abs() * evp.penalty;
            }
            // Energy above the target is worth what the caller says it saves later, at the same
            // moment the target is checked.
            if evp.soc_value_steps.is_some() {
                gesamtbilanz -= evp.soc_value_credit(e, ev_soc);
            }
        }
        Evaluation { fitness: gesamtbilanz, extra: Some(extra) }
    }
}

/// One slot through the inverter: `process_energy`, or `_split_ev_slot_energy` for slot 0 whose
/// EV charging falls into the part still ahead (`ev_import_extra > 0`).
///
/// Returns (feed-in, import, losses, `peak_shaving_ac_wh`, import the grid import limit is
/// checked against).
#[allow(clippy::too_many_arguments)]
fn slot_energy(
    inv: &Inverter,
    rc: &RunConst,
    bat: Option<&mut Battery>,
    sc: &mut SelfConsumption,
    hour: usize,
    produced: f64,
    consumption_without_ev: f64,
    consumption: f64,
    ev_import_extra: f64,
    ev_share: f64,
    allowed: bool,
    export_factor: f64,
) -> Result<(f64, f64, f64, f64, f64), SimError> {
    if ev_import_extra > 0.0 {
        if let Some((without_ev, with_ev)) = rc.ev_split_inverters.as_ref() {
            return split_ev_slot_energy(
                (without_ev, with_ev),
                bat,
                sc,
                hour,
                produced,
                consumption_without_ev,
                consumption - consumption_without_ev,
                ev_share,
                allowed,
                export_factor,
            );
        }
        return Err(SimError);
    }
    let (fi, gi, l, _, peak) = inv.process_energy(bat, sc, produced, consumption, hour, allowed, export_factor)?;
    Ok((fi, gi, l, peak, gi))
}

/// `_split_ev_slot_energy`: slot 0 as a part without the EV, then a part with it; power budgets
/// of inverter and battery and the grid import limit scaled to each part.
#[allow(clippy::too_many_arguments)]
fn split_ev_slot_energy(
    parts: (&Inverter, &Inverter),
    mut bat: Option<&mut Battery>,
    sc: &mut SelfConsumption,
    hour: usize,
    energy_produced: f64,
    consumption_without_ev_wh: f64,
    ev_wh: f64,
    ev_share: f64,
    allowed: bool,
    export_factor: f64,
) -> Result<(f64, f64, f64, f64, f64), SimError> {
    let (battery_slot_duration_h, charge_limit_wh, mut discharged_wh, mut charged_wh) = match bat.as_deref() {
        Some(b) => (b.s.slot_duration_h, b.limit[hour], b.discharged[hour], b.charged[hour]),
        None => (0.0, 0.0, 0.0, 0.0),
    };
    let mut totals = [0.0f64; 4];
    let mut peak_shaving_ac_wh = 0.0;
    let mut part_import_wh = 0.0;
    let mut outcome: Result<(), SimError> = Ok(());
    for (part, ev_part_wh, inv) in [(1.0 - ev_share, 0.0, parts.0), (ev_share, ev_wh, parts.1)] {
        if part <= 0.0 {
            continue;
        }
        if let Some(b) = bat.as_deref_mut() {
            b.s.slot_duration_h = battery_slot_duration_h * part;
            b.limit[hour] = charge_limit_wh * part;
            let soc = b.soc_wh;
            b.restore_slot_state(hour, (soc, 0.0, 0.0));
        }
        let flows = match inv.process_energy(
            bat.as_deref_mut(),
            sc,
            energy_produced * part,
            consumption_without_ev_wh * part + ev_part_wh,
            hour,
            allowed,
            export_factor,
        ) {
            Ok(f) => f,
            Err(e) => {
                outcome = Err(e);
                break;
            }
        };
        totals[0] += flows.0;
        totals[1] += flows.1;
        totals[2] += flows.2;
        totals[3] += flows.3;
        peak_shaving_ac_wh += flows.4;
        if let Some(b) = bat.as_deref() {
            discharged_wh += b.discharged[hour];
            charged_wh += b.charged[hour];
        }
        part_import_wh = flows.1 / part;
    }
    if let Some(b) = bat.as_deref_mut() {
        b.s.slot_duration_h = battery_slot_duration_h;
        b.limit[hour] = charge_limit_wh;
        let soc = b.soc_wh;
        b.restore_slot_state(hour, (soc, discharged_wh, charged_wh));
    }
    outcome?;
    Ok((totals[0], totals[1], totals[2], peak_shaving_ac_wh, part_import_wh))
}

/// `_terminal_value_credit`.
pub fn terminal_credit(rc: &RunConst, b: &Battery) -> f64 {
    let mut energy_wh = b.current_energy_content();
    if let Some(inv) = rc.inverter.as_ref() {
        energy_wh *= inv.reference_dc_to_ac_efficiency;
    }
    // An empty curve becomes `Fixed(price_per_wh_battery)` at construction (like Python).
    match &rc.terminal {
        Terminal::Auto { x, y } => {
            if energy_wh <= 0.0 {
                0.0
            } else {
                numpy::interp(energy_wh, x, y, rc.numeric.interp_fma)
            }
        }
        Terminal::Tail { x, y } => numpy::interp(energy_wh, x, y, rc.numeric.interp_fma),
        Terminal::Fixed(p) => energy_wh * p,
    }
}

/// `_battery_max_soc_until_full_charge_deadline` (start index 0).
pub fn max_soc_until(soc_per_slot: &[f64], deadline_slot: i64, current_soc: f64) -> f64 {
    let index = deadline_slot;
    let upto = (index.max(0) + 1) as usize;
    let mut reached: Vec<f64> = soc_per_slot.iter().take(upto).copied().collect();
    if index >= soc_per_slot.len() as i64 {
        reached.push(current_soc);
    }
    let finite: Vec<f64> = reached.into_iter().filter(|v| v.is_finite()).collect();
    if finite.is_empty() {
        return current_soc;
    }
    // Python's max(): first maximum
    let mut m = finite[0];
    for &v in &finite[1..] {
        if v > m {
            m = v;
        }
    }
    m
}
