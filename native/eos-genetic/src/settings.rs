//! Run configuration: `GeneticCommonSettings` (optimization.genetic) and the other
//! configuration values the optimizer reads (`prediction.hours`,
//! `feedintariff.direct_marketing_enabled`, timezone, device fallback values), plus
//! the start and observation time of the run (EMS).
//!
//! Mirror of `optimization/genetic/geneticsettings.py`. The snapshot semantics of
//! `optimize_ems()` (configuration frozen for the run) come for free in Rust:
//! the run gets its own copy.

use chrono::DateTime;
use chrono_tz::Tz;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// Default of `penalties["grid_import_limit_excess"]` [EUR/kWh].
pub const GRID_IMPORT_LIMIT_EXCESS_PENALTY_EURO_PER_KWH: f64 = 10.0;

/// `TerminalValueMode`.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
pub enum TerminalValueMode {
    #[default]
    #[serde(rename = "AUTO")]
    Auto,
    #[serde(rename = "FIXED")]
    Fixed,
}

fn default_interval_sec() -> i64 {
    3600
}
fn default_horizon_hours() -> i64 {
    24
}
fn default_individuals() -> Option<i64> {
    Some(300)
}
fn default_generations() -> Option<i64> {
    Some(400)
}
fn default_true() -> bool {
    true
}
fn default_measurement_max_age() -> i64 {
    300
}
fn default_tail_horizon_hours() -> i64 {
    48
}
fn default_terminal_window() -> i64 {
    24
}

/// Penalty values as in Python: number or (rarely) string.
pub fn default_penalties() -> Map<String, Value> {
    let mut m = Map::new();
    m.insert("ev_soc_miss".into(), Value::from(10));
    m.insert("ac_charge_break_even".into(), Value::from(1.0));
    m.insert("battery_full_charge_miss".into(), Value::from(10));
    m.insert("grid_import_limit_excess".into(), Value::from(GRID_IMPORT_LIMIT_EXCESS_PENALTY_EURO_PER_KWH));
    m
}

/// `GeneticCommonSettings`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct GeneticSettings {
    #[serde(default = "default_interval_sec", deserialize_with = "crate::params::de_int")]
    pub interval_sec: i64,
    #[serde(default = "default_horizon_hours", deserialize_with = "crate::params::de_int")]
    pub horizon_hours: i64,
    #[serde(default = "default_individuals", deserialize_with = "crate::params::de_opt_int")]
    pub individuals: Option<i64>,
    #[serde(default = "default_generations", deserialize_with = "crate::params::de_opt_int")]
    pub generations: Option<i64>,
    #[serde(default, deserialize_with = "crate::params::de_opt_int")]
    pub seed: Option<i64>,
    #[serde(default, deserialize_with = "crate::params::de_opt_int")]
    pub fitness_cache_max_entries: Option<i64>,
    #[serde(default, deserialize_with = "crate::params::de_opt_int")]
    pub workers: Option<i64>,
    #[serde(default = "default_true")]
    pub pin_workers: bool,
    #[serde(default = "default_measurement_max_age", deserialize_with = "crate::params::de_int")]
    pub measurement_max_age_seconds: i64,
    #[serde(default = "default_tail_horizon_hours", deserialize_with = "crate::params::de_int")]
    pub tail_horizon_hours: i64,
    #[serde(default)]
    pub terminal_value_mode: TerminalValueMode,
    #[serde(default)]
    pub terminal_value_euro_per_kwh: f64,
    #[serde(default = "default_terminal_window", deserialize_with = "crate::params::de_int")]
    pub terminal_value_window_hours: i64,
    #[serde(default = "default_penalties")]
    pub penalties: Map<String, Value>,
}

impl Default for GeneticSettings {
    fn default() -> Self {
        serde_json::from_value(Value::Object(Map::new())).expect("defaults")
    }
}

impl GeneticSettings {
    /// `horizon` (computed field): number of control slots.
    pub fn horizon(&self) -> i64 {
        if self.interval_sec == 0 {
            return 0;
        }
        ((self.horizon_hours * 3600) as f64 / self.interval_sec as f64) as i64
    }

    /// `float(penalties[key])`, `None` if the key is missing or cannot be converted
    /// (Python then falls back to the default via `except`).
    pub fn penalty_float(&self, key: &str) -> Option<f64> {
        match self.penalties.get(key)? {
            Value::Number(n) => n.as_f64(),
            Value::String(s) => s.trim().parse::<f64>().ok(),
            Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
            _ => None,
        }
    }
}

/// Which version of the Python GENETIC behaviour a run reproduces.
///
/// The kernel follows Python, never the other way round. Behaviour that is still under review
/// upstream is ported ahead but stays off unless the calling EOS has it; the caller picks the
/// set that matches its own Python sources (see `akkudoktoreos.optimization.genetic.native`).
/// All off is the behaviour of Akkudoktor-EOS main (7767028).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Semantics {
    /// Neutral DC charge permission stays enabled (upstream PR #1397).
    pub neutral_dc_permission: bool,
    /// Reported AC charge controls are the executed (inverter-limited) factors (PR #1401).
    pub executed_ac_controls: bool,
    /// EV seeding reads the requested target SoC and EV charging can shift in time.
    pub ev_charge_seeding: bool,
}

impl Semantics {
    /// Akkudoktor-EOS main.
    pub const EOS_MAIN: Semantics =
        Semantics { neutral_dc_permission: false, executed_ac_controls: false, ev_charge_seeding: false };
    /// Everything this kernel knows (main plus the pending changes above).
    pub const ALL: Semantics =
        Semantics { neutral_dc_permission: true, executed_ac_controls: true, ev_charge_seeding: true };
}

/// Everything a run needs besides the parameters.
#[derive(Debug, Clone)]
pub struct RunConfig {
    pub genetic: GeneticSettings,
    /// `config.prediction.hours`.
    pub prediction_hours: i64,
    /// `config.feedintariff.direct_marketing_enabled`.
    pub direct_marketing_enabled: bool,
    /// `config.general.timezone`.
    pub timezone: Tz,
    /// `ems.start_datetime`: start of slot 0 (local time, run timezone).
    pub start_datetime: DateTime<Tz>,
    /// `ems.observation_datetime`: time of the observation ("now").
    pub observation_datetime: DateTime<Tz>,
    /// Fallback values from `config.devices` when the parameters carry no charge rates
    /// (`optimize_ems`: first device of the list).
    pub fallback_battery_charge_rates: Option<Vec<f64>>,
    pub fallback_battery_grid_export_rates: Option<Vec<f64>>,
    pub fallback_ev_charge_rates: Option<Vec<f64>>,
    /// Result of this service's last solution, for `_resolve_start_solution_datetime`
    /// (same genome → its start time).
    pub last_solution: Option<(Vec<f64>, DateTime<Tz>)>,
    /// Order of the dot product in the self-consumption table (see `numpy::DotKernel`).
    pub dot_kernel: crate::numpy::DotKernel,
    /// Compute threads for the fitness (1 = serial). The result does not depend on it.
    pub threads: usize,
    /// `optimize_ems(worst_case=...)`: maximise instead of minimise the balance.
    pub worst_case: bool,
    /// Python behaviour to reproduce.
    pub semantics: Semantics,
}
