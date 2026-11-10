# Native kernels (optional)

This directory holds an optional Rust implementation of the GENETIC optimizer and its Python
binding. EOS does not need it: without the package, or with the default configuration, nothing
here is used.

| Path | What it is |
|---|---|
| `eos-genetic/` | Rust port of `akkudoktoreos.optimization.genetic` (simulation, GA, terminal and tail value, solution). No Python, no I/O. |
| `akkudoktoreos-native/` | PyO3 binding, built into the wheel `akkudoktoreos-native` (Python package `akkudoktoreos_native`) with maturin. One `abi3` wheel per platform covers CPython 3.11+. |
| `scripts/build-wheels.sh` | Builds Linux wheels for x86_64, aarch64 and armv7 (glibc and musl) with maturin and zig. |
| `scripts/genetic_compare_plugin.py` | pytest plugin: runs every GENETIC optimization of a test session in `compare` mode and lists differences. |
| `scripts/certify.py` | Adds the fingerprint of the current GENETIC sources to `certified.json` after a clean compare run. |

## Using it

```sh
pip install akkudoktoreos-native        # or a wheel built with scripts/build-wheels.sh
```

```yaml
optimization:
  genetic:
    backend: native   # python (default) | native | compare
```

- `python` – the Python optimizer, unchanged.
- `native` – the whole run (`GeneticOptimization._optimize_ems_run`) in Rust, returning the same
  `GeneticSolution`. Falls back to Python, with one warning per reason, when the package is
  missing, does not know these GENETIC sources, or rejects the request (for example appliance
  `time_windows`, which the kernel does not support yet).
- `compare` – runs both, returns the Python result, logs whether they are identical, and keeps
  the last report in `akkudoktoreos.optimization.genetic.native.last_compare_report`.

## Verification guard

The kernel is a port; Python is the reference. `native.source_fingerprint()` hashes the Python
files that define a GENETIC run (comments and docstrings ignored) plus the self-consumption
table. The package's `certified.json` lists the fingerprints it was verified against, with the
behaviour switches the kernel needs for them (`neutral_dc_permission`, `executed_ac_controls`,
`ev_charge_seeding` – ports of changes still under review upstream; all off for main). Any change
of those sources disables `native` until the port follows and the new fingerprint is certified.
`EOS_NATIVE_GENETIC_UNCERTIFIED=1` runs uncertified sources with main's switches – meant for the
compare CI job, which answers exactly that question.

## Numerical profile

With the same seed, Python returns different plans on different CPUs. Two operations round
differently per platform:

- `np.dot` in `SelfConsumptionProbabilityInterpolator._expected_direct_consumption` goes to BLAS
  (OpenBLAS picks a kernel per CPU: NEOVERSEN1, CORTEXA53, Haswell, …; Accelerate on macOS).
- `np.interp` may be compiled with fused multiply-add on some builds.

The kernel can reproduce the OpenBLAS kernels it knows (`sequential`, `cortexa53`,
`neoversen1`) and `interp` with or without FMA:

- `native` mode always uses the **release profile** (sequential dot product, no FMA). The same
  input and seed give the same plan on x86_64, aarch64 and armv7, glibc or musl.
- `compare` mode probes this process' NumPy (`native.reference_profile()`) and uses the matching
  profile. Then the two results must be bit-identical. If no profile matches (Accelerate, x86-64
  OpenBLAS kernels), compare still runs and reports differences as expected.
- Tests that compare `native` with `python` put Python on the release profile (a sequential dot
  product in the interpolator, NumPy grid interpolation); see `tests/test_geneticnative.py`.

## Building

```sh
# development build into the active virtual environment
cd native/akkudoktoreos-native && maturin develop --release
# Linux wheels (needs rustup targets, maturin >= 1.9, ziglang)
native/scripts/build-wheels.sh native/dist
```

Rust 1.87 or newer. Release builds use `opt-level=3`, fat LTO, one codegen unit. Panics never
cross into Python as `PanicException`; they become `NativeRunError` and EOS falls back.

## Checking a change

```sh
pytest tests/test_geneticnative.py
PYTHONPATH=native/scripts GENETIC_COMPARE_OUT=compare.json \
  pytest -p genetic_compare_plugin tests/test_genetic*.py
python native/scripts/certify.py compare.json   # only if the summary shows 0 different
```

## Not supported natively (falls back to Python)

- appliance `time_windows` / `shared_time_windows`,
- infinite forecast values (NaN gaps are fine),
- anything the kernel rejects while validating the request.

License: Apache-2.0, like EOS. The kernel was ported from EOS (see `eos-genetic/NOTICE`).
