#!/bin/bash
# Build and run the Embench-IoT suite (unmodified, scalar -O2) on rv32g_sim,
# then print a per-benchmark instruction-class profile and resource-bound
# speedup ceilings for the STARBUG lane rules (see embench_profile.py).
#
#   ./run_embench.sh              # every benchmark
#   ./run_embench.sh crc32 edn    # a subset
#
# Embench is GPL-3.0, so it is fetched at a pinned commit instead of being
# vendored into this repo. Override with EMBENCH_DIR=/path/to/embench-iot.
# -std=gnu17: Embench predates C23 (wikisort typedefs its own `bool`).
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
SIMDIR="$(cd "$HERE/../.." && pwd)"
OUT="${OUT:-$SIMDIR/../build/embench}"
EMBENCH_COMMIT="09c2ed8"
EMBENCH_DIR="${EMBENCH_DIR:-$OUT/embench-iot}"
RVGCC="${RVGCC:-$HOME/riscv/rv32/bin/riscv32-unknown-elf-gcc}"
CFLAGS="-O2 -std=gnu17 -march=rv32imafdc -mabi=ilp32d -DGLOBAL_SCALE_FACTOR=1 -DWARMUP_HEAT=0"
LDFLAGS="-specs=semihost.specs -T $HERE/../harness/link.ld"

mkdir -p "$OUT"
if [ ! -d "$EMBENCH_DIR/src" ]; then
  git clone -q https://github.com/embench/embench-iot.git "$EMBENCH_DIR" || exit 1
  git -C "$EMBENCH_DIR" checkout -q "$EMBENCH_COMMIT" 2>/dev/null \
    || { git -C "$EMBENCH_DIR" fetch -q --unshallow; git -C "$EMBENCH_DIR" checkout -q "$EMBENCH_COMMIT"; }
fi

benches=("$@")
[ ${#benches[@]} -eq 0 ] && benches=($(ls "$EMBENCH_DIR/src"))

for b in "${benches[@]}"; do
  d="$OUT/$b"; mkdir -p "$d"; rm -f "$d"/*.elf "$d"/*.proto "$d"/run.txt
  if ! $RVGCC $CFLAGS -I"$EMBENCH_DIR/support" -I"$EMBENCH_DIR/src/$b" $LDFLAGS \
        -o "$d/$b.elf" "$HERE/embench_main.c" "$EMBENCH_DIR/support/beebsc.c" \
        "$EMBENCH_DIR/src/$b"/*.c -lm 2> "$d/build.log"; then
    echo "$b: BUILD FAILED (see $d/build.log)" >&2; continue
  fi
  (cd "$SIMDIR" && timeout 1800 ./bazel-bin/riscv/rv32g_sim --semihost_arm \
     --output_dir="$d" "$d/$b.elf" > "$d/run.txt" 2>/dev/null)
  grep -q '^verify=' "$d/run.txt" || echo "$b: RUN FAILED (see $d/run.txt)" >&2
done

python3 "$HERE/embench_profile.py" "$OUT" "${benches[@]}"
