#!/bin/bash
# Rule-set experiment: how much would (a) a second load port on a two-bank
# data memory and (b) branches inside bundles help STARBUG?
#
# For each Embench-IoT benchmark:
#   1. compile its sources to -O2 assembly
#   2. bundle every basic block under scalar / R0 / R1 / R2 / R3
#      (starbug_bundle.py; rule sets documented there)
#   3. build, check every bundle against its rule set (rule_check.py, plus
#      starbug-llvm's RTL checker for R0), and run:
#        scalar  rv32g_sim with --pc_histogram (block execution counts)
#        R0..R3  rv32_vliw_sim; R1/R3 also under each banked-memory model
#   4. report.py writes results.md and <bench>/hot_blocks.md
#
#   ./run_explore.sh                     # the six default benchmarks
#   ./run_explore.sh crc32 edn           # a subset
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
SIMDIR="$(cd "$HERE/../.." && pwd)"
OUT="${OUT:-$SIMDIR/../build/explore}"
EMBENCH_DIR="${EMBENCH_DIR:-$SIMDIR/../build/embench/embench-iot}"
RVGCC="${RVGCC:-$HOME/riscv/rv32/bin/riscv32-unknown-elf-gcc}"
RVOBJDUMP="${RVGCC%gcc}objdump"
RVNM="${RVGCC%gcc}nm"
STARBUG_LLVM="${STARBUG_LLVM:-$SIMDIR/../starbug-llvm}"
CFLAGS="-O2 -std=gnu17 -march=rv32imafdc -mabi=ilp32d -DGLOBAL_SCALE_FACTOR=1 -DWARMUP_HEAT=0"
LDFLAGS="-specs=semihost.specs -T $HERE/../harness/link.ld"
RULES="scalar R0 R1 R2 R3 R2b R3b"
# Banked-memory models. Globals/heap live at 0x8000xxxx and the stack at
# 0x0000-0x8000, so split:0x80000000 is "stack in one half, all global data
# in the other" -- what upper/lower halves give a data memory whose globals
# sit at the bottom and stack at the top. interleave:N uses address bit N.
BANKS="none split:0x80000000 interleave:2 interleave:6"

[ -d "$EMBENCH_DIR/src" ] || { echo "run benchmarks/embench/run_embench.sh first (fetches Embench)" >&2; exit 1; }
benches=("$@")
[ ${#benches[@]} -eq 0 ] && benches=(matmult-int edn crc32 huffbench nsichneu nettle-sha256)

sim() { # sim <simulator> <elf> <tag> [extra flags...]
  local s=$1 elf=$2 tag=$3; shift 3
  local d; d="$(dirname "$elf")/runs"; mkdir -p "$d/$tag"
  (cd "$SIMDIR" && timeout 1800 "./bazel-bin/riscv/$s" --semihost_arm \
     --output_dir="$d/$tag" "$@" "$elf" > "$d/$tag/stdout.txt" 2>/dev/null)
}

for b in "${benches[@]}"; do
  d="$OUT/$b"; rm -rf "$d"; mkdir -p "$d/src"
  echo "== $b"
  # beebsc.c (Embench's rand/malloc helpers) is called from inside some timed
  # loops (crc32 draws a random byte per iteration), so it is bundled too.
  srcs=("$EMBENCH_DIR/src/$b"/*.c "$EMBENCH_DIR/support/beebsc.c")
  for i in "${!srcs[@]}"; do
    $RVGCC $CFLAGS -I"$EMBENCH_DIR/support" -I"$EMBENCH_DIR/src/$b" \
      -fno-asynchronous-unwind-tables -fno-unwind-tables -g0 \
      -S -o "$d/src/$i.s" "${srcs[$i]}" || exit 1
  done
  for r in $RULES; do
    mkdir -p "$d/$r"
    for i in "${!srcs[@]}"; do
      python3 "$HERE/starbug_bundle.py" "$d/src/$i.s" "$d/$r/$i.s" --rules "$r" --file-id "$i" || exit 1
    done
    $RVGCC $CFLAGS -I"$EMBENCH_DIR/support" $LDFLAGS -o "$d/$r/$b.elf" \
      "$HERE/../embench/embench_main.c" "$d/$r"/[0-9]*.s -lm \
      2>&1 | grep -v 'RWX' | grep -v '^$'
    [ -e "$d/$r/$b.elf" ] || { echo "$b $r: BUILD FAILED" >&2; continue; }
    if [ "$r" = scalar ]; then
      sim rv32g_sim "$d/$r/$b.elf" g --pc_histogram="$d/$r/pc_histogram.txt"
      $RVNM "$d/$r/$b.elf" | grep ' Lsb_' > "$d/$r/block_labels.txt"
    else
      sim rv32g_sim "$d/$r/$b.elf" g
      python3 "$HERE/rule_check.py" "$d/$r/$b.elf" --rules "$r" --objdump "$RVOBJDUMP" \
        --starbug-llvm "$STARBUG_LLVM" > "$d/$r/rule_check.txt" 2>&1
      [ "$r" = R0 ] && python3 "$STARBUG_LLVM/tools/starbug_bundle_check.py" "$d/$r/$b.elf" \
        --objdump "$RVOBJDUMP" --warnings > "$d/$r/rtl_check.txt" 2>&1
      for bk in $BANKS; do
        case "$r:$bk" in R0:none|R2:none|R2b:none|R1:*|R3:*|R3b:*) ;; *) continue ;; esac
        sim rv32_vliw_sim "$d/$r/$b.elf" "vliw_${bk//[:]/_}" --dmem_banks="$bk"
      done
    fi
    "$RVOBJDUMP" -d "$d/$r/$b.elf" > "$d/$r/$b.dis"
  done
done

# Upper/lower halves with profile-guided data placement (R1, R3, R3b):
# rebuild with -fdata-sections so each global is its own section, log which
# address pairs are loaded together, move objects between halves to break
# the most frequent same-bank pairs, relink and measure.
PLACE_RULES="R1 R3 R3b"
for b in "${benches[@]}"; do
  d="$OUT/$b"
  srcs=("$EMBENCH_DIR/src/$b"/*.c "$EMBENCH_DIR/support/beebsc.c")
  mkdir -p "$d/src_ds"
  for i in "${!srcs[@]}"; do
    $RVGCC $CFLAGS -fdata-sections -I"$EMBENCH_DIR/support" -I"$EMBENCH_DIR/src/$b" \
      -fno-asynchronous-unwind-tables -fno-unwind-tables -g0 \
      -S -o "$d/src_ds/$i.s" "${srcs[$i]}" || exit 1
  done
  for r in $PLACE_RULES; do
    p="$d/place/$r"; mkdir -p "$p"
    for i in "${!srcs[@]}"; do
      python3 "$HERE/starbug_bundle.py" "$d/src_ds/$i.s" "$p/$i.s" --rules "$r" --file-id "$i" || exit 1
      $RVGCC $CFLAGS -c -o "$p/$i.o" "$p/$i.s" || exit 1
    done
    $RVGCC $CFLAGS -I"$EMBENCH_DIR/support" $LDFLAGS -o "$p/profile.elf" "$HERE/../embench/embench_main.c" "$p"/[0-9]*.o -lm 2>&1 | grep -v RWX | grep -v '^$'
    sim rv32_vliw_sim "$p/profile.elf" profile --dmem_banks=split:0x80000000 --bank_pair_log="$p/pairs.txt"
    python3 "$HERE/place_banks.py" "$p/pairs.txt" "$p/profile.elf" "$p"/[0-9]*.o \
      --ld "$HERE/../harness/link.ld" --out-ld "$p/link_placed.ld" \
      --nm "$RVNM" --objdump "$RVOBJDUMP" > "$p/placement.txt" || exit 1
    $RVGCC $CFLAGS -I"$EMBENCH_DIR/support" -specs=semihost.specs -T "$p/link_placed.ld" -o "$p/$b.elf" \
      "$HERE/../embench/embench_main.c" "$p"/[0-9]*.o -lm 2>&1 | grep -v RWX | grep -v '^$'
    sim rv32_vliw_sim "$p/$b.elf" vliw_split_placed --dmem_banks=split:0x80000000
    python3 "$HERE/rule_check.py" "$p/$b.elf" --rules "$r" --objdump "$RVOBJDUMP" \
      --starbug-llvm "$STARBUG_LLVM" > "$p/rule_check.txt" 2>&1
  done
done

python3 "$HERE/report.py" "$OUT" "${benches[@]}"
