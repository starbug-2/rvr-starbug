#!/bin/bash
# STARBUG2 (custom-0 execute-packet header) experiment.
#
# For each benchmark and each code variant (-O2, -O2 -funroll-loops):
#   baselines  scalar (rv32_vliw_sim with no packets) and the STARBUG1
#              hint design R0 (starbug_bundle.py --rules R0)
#   STARBUG2   width {4, 8, 8 with MUL on 4 lanes} x
#              {base, +load, +branch lane 0, +branch own lane,
#               +load +branch lane 0, +load +branch own lane}
# Each configuration is built twice: naive layout, and fetch-aware layout
# (loops start on a 64-byte line, starbug2_bundle.py --align-loops). Every
# build is checked from the binary (rule_check2.py) and run on rv32_vliw_sim
# with the 64-byte fetch model and without it; aligned +load builds also run
# with word-interleaved banks and with upper/lower halves after profile-guided
# data placement (place_banks.py). report2.py summarises.
#
#   ./run_starbug2.sh                    # the six default benchmarks
#   ./run_starbug2.sh matmult-int edn    # a subset
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
SIMDIR="$(cd "$HERE/../.." && pwd)"
OUT="${OUT:-$SIMDIR/../build/starbug2}"
EMBENCH_DIR="${EMBENCH_DIR:-$SIMDIR/../build/embench/embench-iot}"
RVGCC="${RVGCC:-$HOME/riscv/rv32/bin/riscv32-unknown-elf-gcc}"
RVOBJDUMP="${RVGCC%gcc}objdump"
RVNM="${RVGCC%gcc}nm"
STARBUG_LLVM="${STARBUG_LLVM:-$SIMDIR/../starbug-llvm}"
BASE_CFLAGS="-O2 -std=gnu17 -march=rv32imafdc -mabi=ilp32d -DGLOBAL_SCALE_FACTOR=1 -DWARMUP_HEAT=0"
LD="$HERE/../harness/link.ld"
LINE=64
SPLIT=0x80000000

declare -A CODE_FLAGS=([o2]="" [unroll]="-funroll-loops")
declare -A WIDTH_ARGS=([w4]="--width 4" [w8]="--width 8" [w8m4]="--width 8 --mdu-lanes 4")
declare -A FEAT_ARGS=([base]="" [load]="--extra-load" [br0]="--branch lane0" [brown]="--branch own"
                      [load_br0]="--extra-load --branch lane0" [load_brown]="--extra-load --branch own")
WIDTHS="w4 w8 w8m4"
FEATS="base load br0 brown load_br0 load_brown"

[ -d "$EMBENCH_DIR/src" ] || { echo "run benchmarks/embench/run_embench.sh first (fetches Embench)" >&2; exit 1; }
benches=("$@")
[ ${#benches[@]} -eq 0 ] && benches=(matmult-int edn crc32 huffbench nsichneu nettle-sha256)

sim() { # sim <simulator> <elf> <tag> [extra flags...]
  local s=$1 elf=$2 tag=$3; shift 3
  local d; d="$(dirname "$elf")/runs/$tag"; mkdir -p "$d"
  (cd "$SIMDIR" && timeout 1800 "./bazel-bin/riscv/$s" --semihost_arm \
     --output_dir="$d" "$@" "$elf" > "$d/stdout.txt" 2>/dev/null)
}

link() { # link <dir> <elf> [ld script]
  local d=$1 elf=$2 ld=${3:-$LD}
  $RVGCC $BASE_CFLAGS -I"$EMBENCH_DIR/support" -specs=semihost.specs -T "$ld" -o "$elf" \
    "$HERE/../embench/embench_main.c" "$d"/[0-9]*.o -lm 2>&1 | grep -v RWX | grep -v '^$'
  [ -e "$elf" ]
}

build() { # build <bench dir> <code> <name> <bundler command...>  (writes <dir>/<code>/<name>/)
  local d=$1 code=$2 name=$3; shift 3
  local p="$d/$code/$name"; mkdir -p "$p"
  local n; n=$(ls "$d/$code/src"/*.s | wc -l)
  for ((i = 0; i < n; i++)); do
    "$@" "$d/$code/src/$i.s" "$p/$i.s" --file-id "$i" || return 1
    $RVGCC $BASE_CFLAGS -c -o "$p/$i.o" "$p/$i.s" || return 1
  done
  link "$p" "$p/b.elf"
}

for b in "${benches[@]}"; do
  d="$OUT/$b"; rm -rf "$d"; mkdir -p "$d"
  srcs=("$EMBENCH_DIR/src/$b"/*.c "$EMBENCH_DIR/support/beebsc.c")
  for code in o2 unroll; do
    echo "== $b ($code)"
    mkdir -p "$d/$code/src"
    for i in "${!srcs[@]}"; do
      $RVGCC $BASE_CFLAGS ${CODE_FLAGS[$code]} -fdata-sections -I"$EMBENCH_DIR/support" \
        -I"$EMBENCH_DIR/src/$b" -fno-asynchronous-unwind-tables -fno-unwind-tables -g0 \
        -S -o "$d/$code/src/$i.s" "${srcs[$i]}" || exit 1
    done
    # baselines
    build "$d" $code scalar python3 "$HERE/starbug_bundle.py" --rules scalar || exit 1
    sim rv32_vliw_sim "$d/$code/scalar/b.elf" ideal --pc_histogram="$d/$code/scalar/pc_histogram.txt"
    $RVNM "$d/$code/scalar/b.elf" | grep ' Lsb_' > "$d/$code/scalar/block_labels.txt"
    # STARBUG1 hint design, same fetch model (a crossing bundle runs scalar)
    for h in hint_R0 hint_R0_al; do
      al=""; [ $h = hint_R0_al ] && al="--align-loops $LINE"
      build "$d" $code $h python3 "$HERE/starbug_bundle.py" --rules R0 $al || exit 1
      sim rv32_vliw_sim "$d/$code/$h/b.elf" ideal --packet_line_bytes=$LINE
      sim rv32_vliw_sim "$d/$code/$h/b.elf" ideal_nofetch
      python3 "$HERE/rule_check.py" "$d/$code/$h/b.elf" --rules R0 --objdump "$RVOBJDUMP" \
        --starbug-llvm "$STARBUG_LLVM" > "$d/$code/$h/rule_check.txt" 2>&1
    done
    # STARBUG2 configurations
    for w in $WIDTHS; do
      for f in $FEATS; do
       for layout in naive aligned; do
        cfg="${w}_$f"; [ $layout = aligned ] && cfg="${cfg}_al"
        p="$d/$code/$cfg"
        args="${WIDTH_ARGS[$w]} ${FEAT_ARGS[$f]}"
        al=""; [ $layout = aligned ] && al="--align-loops $LINE"
        build "$d" $code "$cfg" python3 "$HERE/starbug2_bundle.py" $args $al || { echo "$b $code $cfg: BUILD FAILED" >&2; continue; }
        python3 "$HERE/rule_check2.py" "$p/b.elf" $args --objdump "$RVOBJDUMP" \
          --starbug-llvm "$STARBUG_LLVM" > "$p/rule_check.txt" 2>&1
        sim rv32_vliw_sim "$p/b.elf" ideal --packet_line_bytes=$LINE
        sim rv32_vliw_sim "$p/b.elf" ideal_nofetch
        [ $layout = aligned ] || continue
        case $f in load*) ;; *) continue ;; esac
        sim rv32_vliw_sim "$p/b.elf" interleave --packet_line_bytes=$LINE --dmem_banks=interleave:2
        sim rv32_vliw_sim "$p/b.elf" split --packet_line_bytes=$LINE --dmem_banks=split:$SPLIT \
          --bank_pair_log="$p/pairs.txt"
        python3 "$HERE/place_banks.py" "$p/pairs.txt" "$p/b.elf" "$p"/[0-9]*.o --ld "$LD" \
          --out-ld "$p/link_placed.ld" --nm "$RVNM" --objdump "$RVOBJDUMP" > "$p/placement.txt" || continue
        mkdir -p "$p/placed"
        link "$p" "$p/placed/b.elf" "$p/link_placed.ld" || continue
        sim rv32_vliw_sim "$p/placed/b.elf" split_placed --packet_line_bytes=$LINE --dmem_banks=split:$SPLIT
       done
      done
    done
  done
done

python3 "$HERE/report2.py" "$OUT" "${benches[@]}"
