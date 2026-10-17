# STARBUG benchmarks

Kernels for measuring HINT-delimited VLIW bundling (`c.li x0, N`) on the MPACT
simulators. `./measure.sh [all|fir|dp|matmult]` builds every variant, runs each
ELF on both `rv32g_sim` (hint = no-op) and `rv32_vliw_sim` (hint = bundle), and
checks correctness and RTL legality.

| Benchmark | Source | Status |
|---|---|---|
| `matmult/` | Embench-IoT `matmult-int` (20×20 int32) | correct, RTL-legal |
| `dp/` | dot product | correct, **bundles violate the RTL contract** |
| `fir_filter/` | FIR filter | **wrong answer** in bundled variants, RTL-illegal |

## Embench-IoT suite profile

`embench/run_embench.sh` fetches Embench-IoT (pinned commit), builds all 19
programs unmodified at `-O2`, runs them on `rv32g_sim` with a cycle-counting
driver (`embench/embench_main.c`), and prints each program's dynamic
instruction-class mix plus resource-only speedup ceilings under the current
lane rules and three what-if rule changes (`embench/embench_profile.py`
explains the model). The whole suite runs in about 10 seconds. These are scalar
programs with no bundle hints: the output is a workload characterisation, not a
STARBUG measurement.

## What a benchmark needs

A folder `benchmarks/<name>/` containing:

1. **`<name>.h`**: the kernel prototype. Use `uint32_t`/fixed sizes, not
   `uint64_t` lengths (on rv32 those arrive split across two registers).
2. **`<name>_scalar.c`**: the reference implementation, compiled at `-O2`. Its
   output defines "correct" and its cycle count is the 1.00× baseline. Write it
   the way a competent C programmer would, so the baseline isn't handicapped
   (see the comment in `matmult/matmult_scalar.c`).
3. **`gen_data.py` → `data.S`**: inputs, a zeroed output array, and ideally the
   expected output (`matrix_expected` in matmult) from the original suite, so
   the result is checked against a known answer, not only against scalar.
4. **`<name>_vliw.S`**: the bundled kernel. Start from `-O2` compiler output or
   generate it (see `matmult/gen_vliw.py`), never from `-O0` output. Every
   bundle must obey the STARBUG RTL contract (paper Table 1):
   - `1 <= N <= 4` (larger N: the hardware ignores the hint entirely)
   - slot *i* runs on lane *i*; loads, stores, branches, jumps and CSR ops only
     in **slot 0**, and at most **one** memory op per bundle
   - no RAW/WAW (and preferably no WAR) between instructions in one bundle
   - keep branches **out of bundles**: a taken lane-0 branch with worker ops
     behind it behaves differently on scalar and STARBUG cores
   - a bundle must not straddle a 64-byte I-cache line, or the RTL quietly runs
     it unbundled (reported as `warn`)
5. **`<name>_unrolled.S`** (recommended): the same instruction stream as the
   VLIW file with the hints removed. Then *unrolled vs scalar* is what the
   rewrite bought and *vliw vs unrolled* is what bundling bought.

Then hook it up:

6. **`harness/bench_main.c`**: add a `#elif defined(BENCH_<NAME>)` block
   (externs, output array, length, kernel call, optional `BENCH_EXPECTED`).
7. **`measure.sh`**: add `<name>_DIR`, `<name>_VARIANTS` and the name to
   `ALL_BENCHES`. The first variant listed is the reference.

A benchmark is **done** when `measure.sh` shows `PASS` (and `known:PASS`) for
every variant and `ok` in the `rtl` column for the VLIW variant.

## Reading the numbers

MPACT charges one cycle per top-level instruction, so a bundle costs 1 no
matter its width, with no latencies, stalls or cache-line effects. The
`vliw cyc` column is an **issue-slot count** and an upper bound on what the
RTL achieves; it is not a cycle prediction. Use the `rtl` column to make sure
the binary could run on the hardware at all.

The `rtl` column needs a checkout of
[starbug-llvm](https://github.com/leomarek/starbug-llvm) next to this repo (or
`STARBUG_LLVM=/path`); it shows `n/a` otherwise.

## Toolchain notes

- `-march=rv32imafdc -mabi=ilp32d`: the prebuilt Linux rv32 toolchain ships
  only the ilp32d newlib. `measure.sh` rewrites each `.S` file's
  `.attribute arch` line to match.
- Link with `harness/link.ld`: the per-benchmark `link.ld` files omit
  `.init_array`, so `printf` silently prints nothing.
- Run with `--semihost_arm`.
