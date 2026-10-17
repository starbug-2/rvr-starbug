#!/bin/bash
# Build every benchmark variant against harness/bench_main.c and run each on
# BOTH simulators, reporting kernel-only cycles measured via csrr mcycle.
#
#   ./measure.sh              # every benchmark
#   ./measure.sh matmult      # just one (fir | dp | matmult)
#
# Why both simulators: the same ELF on rv32g_sim (where c.li x0 is a nop) and
# on rv32_vliw_sim (where it forms a bundle) is the only clean way to isolate
# what bundling buys. Everything else is held constant.
#
# Columns:
#   g_sim / vliw   kernel cycles on each simulator (== issue slots; MPACT charges
#                  one cycle per top-level instruction, so a bundle costs 1)
#   absorbed       g_sim - vliw = instructions folded into bundles
#   speedup        scalar's vliw cycles / this variant's vliw cycles
#   correct        checksum vs the first (scalar) variant, plus the benchmark's
#                  own known-answer check when it has one (verify=)
#   rtl            STARBUG RTL bundle contract, from starbug-llvm's
#                  tools/starbug_bundle_check.py: "ok" or "N unsafe", plus
#                  "W warn" for bundles the RTL would run unbundled. MPACT does
#                  not enforce the contract, so an unsafe binary's speedup is not
#                  achievable on the hardware.
#
# Environment overrides: RVGCC, RVOBJDUMP, STARBUG_LLVM (checkout of
# github.com/leomarek/starbug-llvm; the rtl column shows n/a without it).
set -uo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"   # .../rvr-starbug/benchmarks
SIMDIR="$(dirname "$ROOT")"
OUT="${OUT:-$SIMDIR/../build/measure}"
LD_SCRIPT="$ROOT/harness/link.ld"
HARNESS="$ROOT/harness/bench_main.c"
RVGCC="${RVGCC:-$HOME/riscv/rv32/bin/riscv32-unknown-elf-gcc}"
RVOBJDUMP="${RVOBJDUMP:-${RVGCC%gcc}objdump}"
STARBUG_LLVM="${STARBUG_LLVM:-$SIMDIR/../starbug-llvm}"
CHECKER="$STARBUG_LLVM/tools/starbug_bundle_check.py"
ARCH_ATTR='rv32i2p1_m2p0_a2p1_f2p2_d2p2_c2p0'
CFLAGS="-O2 -march=rv32imafdc -mabi=ilp32d"
LDFLAGS="-specs=semihost.specs -T $LD_SCRIPT"

mkdir -p "$OUT/protos"

# Each benchmark: <name>_DIR is its folder under benchmarks/, <name>_VARIANTS
# is "<label>:<source file> ...". The first variant is the reference (scalar C).
fir_DIR=fir_filter
fir_VARIANTS="scalar:fir_filter_scalar.c vliw:fir_filter_vliw.S vliw_o3:fir_filter_vliw_o3.S unrolled:fir_filter_vliw_unrolled.S"
dp_DIR=dp
dp_VARIANTS="scalar:dp_scalar.c vliw:dp_vliw.S vliw_better:dp_vliw_better.S"
matmult_DIR=matmult
matmult_VARIANTS="scalar:matmult_scalar.c unrolled:matmult_unrolled.S vliw:matmult_vliw.S"
ALL_BENCHES="fir dp matmult"

build_one() { # build_one <bench> <label> <srcfile>
  local bench=$1 label=$2 src=$3 d="$OUT/$bench" dirvar="${1}_DIR" f
  local def="-DBENCH_${bench^^}"
  mkdir -p "$d"
  # Re-stage every time so edited sources are never measured stale. The .S
  # arch attribute is rewritten to include F/D, matching the ilp32d toolchain.
  for f in "$ROOT/${!dirvar}"/*.{c,h,S}; do
    [ -e "$f" ] || continue
    case "$f" in
      *.S) sed "s/^[[:space:]]*\.attribute arch,.*/\t.attribute arch, \"$ARCH_ATTR\"/" "$f" > "$d/$(basename "$f")" ;;
      *)   cp "$f" "$d/" ;;
    esac
  done
  rm -f "$d/$label.elf"
  (cd "$d" && $RVGCC $CFLAGS $def -I. $LDFLAGS -o "$label.elf" "$HARNESS" "$src" data.S 2>&1 \
     | grep -v 'LOAD segment with RWX' | grep -v '^$')
  [ -e "$d/$label.elf" ]
}

run_one() { # run_one <bench> <label> <sim>  -> "cycles instrs checksum verify"
  local bench=$1 label=$2 sim=$3
  local o
  o=$(cd "$SIMDIR" && timeout 900 "./bazel-bin/riscv/$sim" --semihost_arm \
        --output_dir="$OUT/protos" "$OUT/$bench/$label.elf" 2>/dev/null)
  echo "$(sed -n 's/^kernel_cycles=//p' <<<"$o") \
$(sed -n 's/^kernel_instructions=//p' <<<"$o") \
$(sed -n 's/^checksum=//p' <<<"$o") \
$(sed -n 's/^verify=//p' <<<"$o")"
}

rtl_check() { # rtl_check <elf>  -> "ok" | "<n> unsafe" | "... <w> warn" | "n/a"
  [ -e "$CHECKER" ] || { echo "n/a"; return; }
  local r n w out
  r=$(python3 "$CHECKER" "$1" --objdump "$RVOBJDUMP" 2>/dev/null)
  n=$(sed -n 's/^[[:space:]]*UNSAFE bundles[[:space:]]*:[[:space:]]*//p' <<<"$r")
  w=$(sed -n 's/^[[:space:]]*bundles with warnings[[:space:]]*:[[:space:]]*//p' <<<"$r")
  case "$n" in ""|*[!0-9]*) echo "n/a"; return ;; 0) out="ok" ;; *) out="$n unsafe" ;; esac
  # Warnings are non-fatal: today they are bundles that straddle a 64-byte
  # I-cache line, which the RTL silently runs as scalar instructions.
  [ "${w:-0}" != 0 ] && out="$out, $w warn"
  echo "$out"
}

do_bench() { # do_bench <bench>
  local bench=$1 varvar="${1}_VARIANTS"
  local ref="" refc="" hints fmt="%-12s %5s %12s %12s %10s %8s  %-10s %-14s %s\n"
  echo
  echo "================ $bench ================"
  printf "$fmt" variant hints "g_sim cyc" "vliw cyc" absorbed speedup checksum correct rtl
  printf "$fmt" ------------ ----- ------------ ------------ ---------- -------- ---------- -------------- ----------
  for v in ${!varvar}; do
    local label=${v%%:*} src=${v##*:}
    if ! build_one "$bench" "$label" "$src"; then
      printf "%-12s  BUILD FAILED (%s)\n" "$label" "$src"; continue
    fi
    hints=$(grep -oE "c\\.li[[:space:]]+x0" "$OUT/$bench/$src" 2>/dev/null | wc -l)
    read -r gc gi gs gv <<<"$(run_one "$bench" "$label" rv32g_sim)"
    read -r vc vi vs vv <<<"$(run_one "$bench" "$label" rv32_vliw_sim)"
    if [ -z "$gc" ] || [ -z "$vc" ]; then
      printf "%-12s  RUN FAILED (no kernel_cycles output)\n" "$label"; continue
    fi
    [ -z "$ref" ] && { ref="$gs"; refc="$vc"; }
    local ok="ref"; [ "$label" != "${!varvar%%:*}" ] && { [ "$gs" = "$ref" ] && ok="PASS" || ok="FAIL"; }
    [ "$gs" != "$vs" ] && ok="$ok/SIM-MISMATCH"
    [ -n "$gv" ] && ok="$ok,known:$vv"
    printf "$fmt" "$label" "$hints" "$gc" "$vc" "$((gc - vc))" \
           "$(awk -v a="$refc" -v b="$vc" 'BEGIN{printf "%.2fx", a/b}')" \
           "$gs" "$ok" "$(rtl_check "$OUT/$bench/$label.elf")"
  done
}

case "${1:-all}" in
  all) for b in $ALL_BENCHES; do do_bench "$b"; done ;;
  *)   v="${1}_VARIANTS"
       if [ -z "${!v:-}" ]; then echo "usage: measure.sh [all|${ALL_BENCHES// /|}]" >&2; exit 1; fi
       do_bench "$1" ;;
esac
echo
