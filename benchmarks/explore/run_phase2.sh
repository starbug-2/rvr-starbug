#!/bin/bash
# STARBUG2 phase 2: predication (if-conversion) and post-increment loads/stores.
#
# For each benchmark, code variant (-O2, -O2 -funroll-loops), width (4, 8) and
# machine (base, +load +branch on its own lane), builds:
#   none     phase-1 packing
#   ai       + post-increment fusion (custom-1 .pi loads/stores)
#   pA       + if-conversion, predicates in x9-x15 as specified (s1, a0-a5),
#            used only where liveness shows the register is free
#   pA_all   as pA but converting even where the block gets longer
#   pB       + if-conversion, creg remapped to s8-s11, which every benchmark
#            source and the driver are compiled without (-ffixed-s8..s11)
#   pA_ai, pB_ai  both features
# Baselines: scalar, and scalar_fixed (s8-s11 reserved) to price setup B.
# If-conversion is profile-guided: each scalar build's execution counts
# (make_profile.py) decide whether a conversion saves issue slots.
# Fetch-aware layout and the 64-byte fetch model throughout; ideal memory.
#
#   ./run_phase2.sh                    # the six default benchmarks
#   ./run_phase2.sh huffbench          # a subset
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
SIMDIR="$(cd "$HERE/../.." && pwd)"
OUT="${OUT:-$SIMDIR/../build/starbug2_phase2}"
EMBENCH_DIR="${EMBENCH_DIR:-$SIMDIR/../build/embench/embench-iot}"
RVGCC="${RVGCC:-$HOME/riscv/rv32/bin/riscv32-unknown-elf-gcc}"
RVOBJDUMP="${RVGCC%gcc}objdump"
STARBUG_LLVM="${STARBUG_LLVM:-$SIMDIR/../starbug-llvm}"
BASE_CFLAGS="-O2 -std=gnu17 -march=rv32imafdc -mabi=ilp32d -DGLOBAL_SCALE_FACTOR=1 -DWARMUP_HEAT=0"
FIXED="-ffixed-s8 -ffixed-s9 -ffixed-s10 -ffixed-s11"
LD="$HERE/../harness/link.ld"
LINE=64
PRED_A="s1,a0,a1,a2,a3,a4,a5"
PRED_B="s8,s9,s10,s11"
SIM_PRED_B="x24,x25,x26,x27"

declare -A CODE_FLAGS=([o2]="" [unroll]="-funroll-loops")
declare -A WIDTH_ARGS=([w4]="--width 4" [w8]="--width 8")
declare -A FEAT_ARGS=([base]="" [load_brown]="--extra-load --branch own")
declare -A VAR_ARGS=([none]="" [ai]="--autoinc"
                     [pA]="--ifconvert --pred-regs $PRED_A"
                     [pA_all]="--ifconvert --ifconvert-all --pred-regs $PRED_A"
                     [pB]="--ifconvert --pred-regs $PRED_B --reserved-regs $PRED_B"
                     [pA_ai]="--autoinc --ifconvert --pred-regs $PRED_A"
                     [pB_ai]="--autoinc --ifconvert --pred-regs $PRED_B --reserved-regs $PRED_B")
VARIANTS="none ai pA pA_all pB pA_ai pB_ai"

[ -d "$EMBENCH_DIR/src" ] || { echo "run benchmarks/embench/run_embench.sh first (fetches Embench)" >&2; exit 1; }
benches=("$@")
[ ${#benches[@]} -eq 0 ] && benches=(matmult-int edn crc32 huffbench nsichneu nettle-sha256)

sim() { # sim <elf> <tag> [extra flags...]
  local elf=$1 tag=$2; shift 2
  local d; d="$(dirname "$elf")/runs/$tag"; mkdir -p "$d"
  (cd "$SIMDIR" && timeout 1800 ./bazel-bin/riscv/rv32_vliw_sim --semihost_arm \
     --output_dir="$d" "$@" "$elf" > "$d/stdout.txt" 2>/dev/null)
}

build() { # build <src dir> <out dir> <bundler command...>   (MAIN_CFLAGS: extra driver flags)
  local src=$1 p=$2; shift 2
  mkdir -p "$p"
  local n; n=$(ls "$src"/*.s | wc -l)
  for ((i = 0; i < n; i++)); do
    "${@//@I@/$i}" "$src/$i.s" "$p/$i.s" --file-id "$i" || return 1
    $RVGCC $BASE_CFLAGS -c -o "$p/$i.o" "$p/$i.s" || return 1
  done
  $RVGCC $BASE_CFLAGS ${MAIN_CFLAGS:-} -I"$EMBENCH_DIR/support" -specs=semihost.specs -T "$LD" -o "$p/b.elf" \
    "$HERE/../embench/embench_main.c" "$p"/[0-9]*.o -lm 2>&1 | grep -v RWX | grep -v '^$'
  [ -e "$p/b.elf" ]
}

for b in "${benches[@]}"; do
  d="$OUT/$b"; rm -rf "$d"; mkdir -p "$d"
  srcs=("$EMBENCH_DIR/src/$b"/*.c "$EMBENCH_DIR/support/beebsc.c")
  for code in o2 unroll; do
    echo "== $b ($code)"
    for kind in normal fixed; do
      mkdir -p "$d/$code/src_$kind"
      extra=""; [ $kind = fixed ] && extra="$FIXED"
      for i in "${!srcs[@]}"; do
        $RVGCC $BASE_CFLAGS ${CODE_FLAGS[$code]} $extra -I"$EMBENCH_DIR/support" -I"$EMBENCH_DIR/src/$b" \
          -fno-asynchronous-unwind-tables -fno-unwind-tables -g0 \
          -S -o "$d/$code/src_$kind/$i.s" "${srcs[$i]}" || exit 1
      done
    done
    # scalar baselines; their execution counts become the if-conversion profile
    for sc in scalar scalar_fixed; do
      kind=normal; MAIN_CFLAGS=""; [ $sc = scalar_fixed ] && { kind=fixed; MAIN_CFLAGS="$FIXED"; }
      MAIN_CFLAGS="$MAIN_CFLAGS" build "$d/$code/src_$kind" "$d/$code/$sc" python3 "$HERE/starbug_bundle.py" --rules scalar || exit 1
      sim "$d/$code/$sc/b.elf" ideal --pc_histogram="$d/$code/$sc/pc_histogram.txt"
      "${RVGCC%gcc}nm" "$d/$code/$sc/b.elf" | grep ' Lsb_' > "$d/$code/$sc/block_labels.txt"
      python3 "$HERE/make_profile.py" "$d/$code/$sc" || exit 1
    done
    for w in w4 w8; do
      for f in base load_brown; do
        for v in $VARIANTS; do
          p="$d/$code/${w}_${f}_$v"
          src="$d/$code/src_normal"; simflags=""; pcount=7; MAIN_CFLAGS=""; prof="$d/$code/scalar"
          case $v in pB*) src="$d/$code/src_fixed"; simflags="--starbug_pred_regs=$SIM_PRED_B"; pcount=4
                          MAIN_CFLAGS="$FIXED"; prof="$d/$code/scalar_fixed" ;; esac
          pargs=""; case $v in p*) pargs="--profile $prof/profile_@I@.json" ;; esac
          args="${WIDTH_ARGS[$w]} ${FEAT_ARGS[$f]}"
          MAIN_CFLAGS="$MAIN_CFLAGS" build "$src" "$p" python3 "$HERE/starbug2_bundle.py" $args ${VAR_ARGS[$v]} $pargs --align-loops $LINE \
            || { echo "$b $code $w $f $v: BUILD FAILED" >&2; continue; }
          python3 "$HERE/rule_check2.py" "$p/b.elf" $args --pred-count $pcount --objdump "$RVOBJDUMP" \
            --starbug-llvm "$STARBUG_LLVM" > "$p/rule_check.txt" 2>&1
          sim "$p/b.elf" ideal --packet_line_bytes=$LINE $simflags
          sim "$p/b.elf" ideal_nofetch $simflags
        done
      done
    done
  done
done

python3 "$HERE/report_phase2.py" "$OUT" "${benches[@]}"
