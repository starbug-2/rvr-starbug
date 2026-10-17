#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Instruction-class profile and STARBUG speedup ceilings for Embench runs.

Reads the per-opcode counters MPACT writes (<bench>.proto) plus the driver's
kernel_instructions/verify lines (run.txt) for each benchmark directory under
OUT, as produced by run_embench.sh.

The opcode counters cover the whole program, not just the timed region; the
"kern%" column (timed instructions / all instructions) shows how close the mix
is to a pure kernel profile.

Classes follow the STARBUG RTL lane rules (starbug-llvm tools/starbug_isa.py):
loads, stores, atomics, CSR/system ops and AUIPC run only in lane 0; branches
and jumps also need lane 0 and, under the current compiler policy, are never
bundled at all. Everything else (ALU, mul/div, FP arithmetic) can use any lane.

Ceilings are RESOURCE-ONLY upper bounds on issue-slot speedup over scalar:
they ignore data dependences, so real schedules land below them. They answer
"how much could bundling help at best under these rules", which is what tells
you which rule is worth changing:

  now      4 lanes, 1 LSU in lane 0, branches/jumps never bundled
  2lsu     as now, but two memory ops per bundle
  brb      as now, but a branch/jump may sit in lane 0 of a bundle
  2lsu+brb both changes
"""

import math
import os
import re
import sys

LANES = 4


def classify(op):
  if op in ("cycles", "instructions", "none"):
    return None
  if re.fullmatch(r"(Lb|Lbu|Lh|Lhu|Lw|Clw|Clwsp|Flw|Fld|Cflw|Cflwsp|Cfld|Cfldsp|Lrw)", op):
    return "load"
  if re.fullmatch(r"(Sb|Sh|Sw|Csw|Cswsp|Fsw|Fsd|Cfsw|Cfswsp|Cfsd|Cfsdsp|Scw)", op):
    return "store"
  if op.startswith("Amo"):
    return "sys"            # atomics: lane 0 only, lumped with system ops
  if re.fullmatch(r"(Beq|Bne|Blt|Bge|Bltu|Bgeu|Cbeqz|Cbnez)", op):
    return "branch"
  if re.fullmatch(r"(J|Jal|Jalr|Jr|Cj|Cjal|Cjalr|Cjr)", op):
    return "jump"
  if re.fullmatch(r"(Mul|Mulh|Mulhsu|Mulhu|Div|Divu|Rem|Remu)", op):
    return "muldiv"
  if (op.startswith("Csrr") or op.startswith("Fence") or op.startswith("Sfence")
      or op in ("Ecall", "Ebreak", "Cebreak", "Mret", "Sret", "Uret", "Wfi",
                "SlliSemihost", "SraiSemihost", "Auipc", "AuipcHint", "Unimp",
                "Cunimp")):
    return "sys"
  if op.startswith("F"):
    return "fp"
  return "alu"


def read_counters(path):
  counts, name = {}, None
  for line in open(path, errors="replace"):
    m = re.search(r'name: "num_([A-Za-z0-9_]+)"', line)
    if m:
      name = m.group(1)
      continue
    m = re.search(r"uint64_value: (\d+)", line)
    if m and name:
      counts[name] = int(m.group(1))
      name = None
  return counts


def ceiling(c, two_lsu, branch_in_bundle):
  mem = c["load"] + c["store"] + c["sys"]
  ctl = c["branch"] + c["jump"]
  total = sum(c.values())
  if branch_in_bundle:
    lane0 = (mem / 2 if two_lsu else mem) + ctl   # branch shares lane 0
    slots = max(lane0, total / LANES)
  else:
    body = total - ctl                             # ctl ops issue alone
    slots = ctl + max(mem / 2 if two_lsu else mem, body / LANES)
  return total / slots


def main():
  out = sys.argv[1]
  benches = sys.argv[2:] or sorted(os.listdir(out))
  classes = ["load", "store", "branch", "jump", "muldiv", "fp", "alu", "sys"]
  hdr = ("%-15s %4s %10s %5s | " + " ".join(["%5s"] * len(classes)) +
         " | %5s %6s | %5s %5s %5s %8s")
  row = ("%-15s %4s %10d %4.0f%% | " + " ".join(["%4.1f%%"] * len(classes)) +
         " | %4.0f%% %6.1f | %4.2fx %4.2fx %4.2fx %7.2fx")
  print(hdr % (("bench", "ok", "kern_insn", "kern%") + tuple(classes) +
               ("lane0", "ins/br", "now", "2lsu", "brb", "2lsu+brb")))
  print("-" * 140)
  geo = {k: [] for k in ("now", "2lsu", "brb", "both")}
  for b in benches:
    d = os.path.join(out, b)
    proto = os.path.join(d, b + ".proto")
    run = os.path.join(d, "run.txt")
    if not (os.path.exists(proto) and os.path.exists(run)):
      continue
    txt = open(run).read()
    m = re.search(r"kernel_instructions=(\d+)", txt)
    v = re.search(r"verify=(\w+)", txt)
    if not m:
      continue
    raw = read_counters(proto)
    c = {k: 0 for k in classes}
    for op, n in raw.items():
      k = classify(op)
      if k:
        c[k] += n
    total = sum(c.values())
    kern = int(m.group(1))
    lane0 = c["load"] + c["store"] + c["sys"] + c["branch"] + c["jump"]
    ctl = c["branch"] + c["jump"]
    cn, c2, cb, cboth = (ceiling(c, False, False), ceiling(c, True, False),
                         ceiling(c, False, True), ceiling(c, True, True))
    for k, x in zip(("now", "2lsu", "brb", "both"), (cn, c2, cb, cboth)):
      geo[k].append(x)
    print(row % ((b, "PASS" if v and v.group(1) == "PASS" else "FAIL", kern,
                  100.0 * kern / total) +
                 tuple(100.0 * c[k] / total for k in classes) +
                 (100.0 * lane0 / total, total / ctl if ctl else float("inf"),
                  cn, c2, cb, cboth)))
  if geo["now"]:
    g = {k: math.exp(sum(map(math.log, v)) / len(v)) for k, v in geo.items()}
    print("-" * 140)
    print("%-15s %s ceilings (geomean over %d): now %.2fx  2lsu %.2fx  brb %.2fx  2lsu+brb %.2fx"
          % ("", "resource-only", len(geo["now"]), g["now"], g["2lsu"], g["brb"], g["both"]))


if __name__ == "__main__":
  main()
