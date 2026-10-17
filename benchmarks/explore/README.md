# Rule-set exploration: second load port and branches in bundles

Measures, on real binaries run in MPACT, how much STARBUG would gain from

- **a second load port** backed by a two-bank data memory (upper/lower halves
  or interleaved), and
- **allowing a branch/jump to end a bundle**, either sharing lane 0 with the
  memory port or on a lane of its own.

```
./run_explore.sh                 # six Embench-IoT benchmarks, ~1 minute
./run_explore.sh crc32 edn       # a subset
```

Needs the simulators built from this fork (they carry the experiment flags
below), Embench fetched by `../embench/run_embench.sh`, and a checkout of
[starbug-llvm](https://github.com/leomarek/starbug-llvm) next to the repo.
Output goes to `../../../build/explore/`: `results.md` for the summary and
`<bench>/hot_blocks.md` for the per-loop explanation.

## Rule sets

| | memory ops per bundle | branch/jump |
|---|---|---|
| **R0** current contract | 1 (lane 0) | never bundled |
| **R1** + load | 1 + one extra **load** (port B) | never bundled |
| **R2** + branch | 1 | may end a bundle; takes lane 0 (competes with memory) |
| **R2b** + branch, own lane | 1 | may end a bundle on its own lane |
| **R3** = R1 + R2 | 1 + 1 load | as R2 |
| **R3b** = R1 + R2b | 1 + 1 load | as R2b |

Always: at most 4 per bundle; no RAW/WAW/WAR inside a bundle; a store never
shares a bundle with another memory op; calls, CSR/system and FP ops are never
bundled; bundles stay within one basic block. A branch is always the *last*
member and every other member comes from before it, so the bundle is correct
on both paths and on a scalar core that ignores the hint.

## How it works

1. GCC `-O2` compiles each benchmark (and Embench's `beebsc.c` helpers) to
   assembly.
2. `starbug_bundle.py` packs every basic block under each rule set with a list
   scheduler and records *why* each instruction landed in its bundle.
3. Each build is checked from the binary by `rule_check.py` (and R0 also by
   starbug-llvm's RTL checker), then run:
   - scalar on `rv32g_sim --pc_histogram` for per-block execution counts
   - R0–R3b on `rv32_vliw_sim`; builds with two loads per bundle also under
     each `--dmem_banks` model
4. Upper/lower halves are also tried with **bank-aware placement**: a profile
   run logs which address pairs are loaded together (`--bank_pair_log`),
   `place_banks.py` moves whole global objects into the other half to break
   the most frequent same-bank pairs, and the relinked binary is measured.
5. `report.py` writes the tables and cross-checks the bundler: predicted slots
   (block counts × slots per block) must equal the simulator's count.

## Simulator flags added for this (both `rv32g_sim` and `rv32_vliw_sim`)

| flag | effect |
|---|---|
| `--pc_histogram=FILE` | per-address execution counts (`0xADDR COUNT`) |
| `--dmem_banks=none\|split:ADDR\|interleave:BIT` | two-bank data memory for bundles: a second access to the same bank in one bundle costs a stall cycle (counted in `mcycle`); counters `num_multi_mem_bundles`, `num_bank_conflicts`, `num_bank_stall_cycles` |
| `--bank_pair_log=FILE` | address pairs of every two-memory-op bundle |

`split:0x80000000` puts the stack (0x0–0x8000) in one bank and all globals and
heap (0x8000xxxx) in the other, the layout a data memory gets with globals at
the bottom and the stack at the top.

## Limits

- Slots are MPACT issue slots: no load-use or multiply latency, no cache.
  Gains are upper bounds for the RTL.
- Basic-block scheduling of ordinary `-O2` code, no unrolling or software
  pipelining, like last year's compiler. Hand-tuned or unrolled code exposes
  more parallelism.
- WAR inside a bundle is forbidden (the paper's contract). The RTL reads
  registers before writing them, so allowing it is a possible further gain;
  nettle-sha256 is the benchmark most limited by it.
