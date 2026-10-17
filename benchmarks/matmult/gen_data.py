# SPDX-License-Identifier: Apache-2.0
#
# Generate data.S for the matmult benchmark.
#
# Inputs reproduce Embench-IoT matmult-int exactly: a 20x20 int32 matrix A and
# B filled, row-major, by Embench's LCG (seed 0, seed = (seed*133 + 81) % 8095),
# A first, then B. The expected product is emitted too, so the harness can
# check the kernel against a known answer rather than only against the scalar
# variant. Run with --check-embench to confirm it equals the `exp` table in
# Embench's verify_benchmark().
#
# usage: python3 gen_data.py > data.S

import sys

N = 20  # Embench UPPERLIMIT


def lcg(count):
  seed = 0
  out = []
  for _ in range(count):
    seed = (seed * 133 + 81) % 8095
    out.append(seed)
  return out


def to_i32(v):
  v &= 0xFFFFFFFF
  return v - (1 << 32) if v & 0x80000000 else v


def matmul(a, b):
  res = []
  for i in range(N):
    for j in range(N):
      res.append(to_i32(sum(a[i * N + k] * b[k * N + j] for k in range(N))))
  return res


def emit(name, values, alignment=32):
  print(".global %s" % name)
  print(".balign %d" % alignment)
  print("%s:" % name)
  for v in values:
    print("    .word 0x%08x" % (v & 0xFFFFFFFF))


vals = lcg(2 * N * N)
a, b = vals[:N * N], vals[N * N:]
expected = matmul(a, b)

if "--check-embench" in sys.argv:
  # Pass the path to Embench's src/matmult-int/matmult-int.c after the flag.
  import re
  src = open(sys.argv[sys.argv.index("--check-embench") + 1]).read()
  table = src[src.index("matrix exp"):]
  table = table[:table.index("};")]
  ref = [int(x) for x in re.findall(r"\d+", table)]
  ok = ref == expected
  print("matches Embench exp table: %s (%d values)" % (ok, len(ref)))
  sys.exit(0 if ok else 1)

print('.section .data,"aw",@progbits')
print(".global matmult_n")
print(".balign 4")
print("matmult_n:")
print("    .word %d" % N)
emit("matrix_a", a)
emit("matrix_b", b)
emit("matrix_res", [0] * (N * N))
emit("matrix_expected", expected)
