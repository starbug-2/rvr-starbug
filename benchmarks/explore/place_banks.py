#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Profile-guided data placement for an upper/lower-halves banked memory.

With a split at 0x80000000 the stack (0x0-0x8000) is in bank 0 and every
global object is in bank 1, so any two global loads in one bundle collide.
This picks which global objects to move into bank 0 so that the objects most
often loaded together end up in different banks:

  1. read the simulator's --bank_pair_log (address pairs per bundle, counts)
  2. map each address to its data object (nm -S on the profiled ELF)
  3. greedy max-cut: assign movable objects to the bank that minimises
     same-bank pair weight, then refine until no single move helps
  4. write a linker script that places the bank-0 objects at 0x00100000

Only objects in the benchmark's own -fdata-sections assembly can move; the
stack is fixed in bank 0 and heap/libc data are fixed in bank 1. Pairs inside
one object (e.g. A[i] with A[j]) cannot be separated by placement and are
reported as such.

usage: place_banks.py PAIRLOG ELF OBJ.o [OBJ.o ...] --ld IN.ld --out-ld OUT.ld
"""

import argparse
import bisect
import re
import subprocess

SPLIT = 0x80000000
BANK0_ORIGIN = 0x00100000


def symbols(nm, elf):
  syms = []
  for line in subprocess.run([nm, "-S", "--defined-only", elf], capture_output=True,
                             text=True, check=True).stdout.splitlines():
    p = line.split()
    if len(p) == 4 and p[2].lower() in "bdgrs":
      syms.append((int(p[0], 16), int(p[1], 16), p[3]))
  syms.sort()
  return syms


def sections_of(objdump, objs):
  """symbol name -> input section name, for objects defined in OBJs."""
  sec = {}
  for o in objs:
    for line in subprocess.run([objdump, "-t", o], capture_output=True, text=True,
                               check=True).stdout.splitlines():
      m = re.match(r"^[0-9a-f]+\s+[lg]\s+O\s+(\S+)\s+[0-9a-f]+\s+(\S+)$", line)
      if m and re.match(r"^\.(s?bss|s?data|s?rodata)\.", m.group(1)):
        sec[m.group(2)] = m.group(1)
  return sec


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("pairlog")
  ap.add_argument("elf")
  ap.add_argument("objs", nargs="+")
  ap.add_argument("--ld", required=True)
  ap.add_argument("--out-ld", required=True)
  ap.add_argument("--nm", default="riscv32-unknown-elf-nm")
  ap.add_argument("--objdump", default="riscv32-unknown-elf-objdump")
  args = ap.parse_args()

  syms = symbols(args.nm, args.elf)
  starts = [s[0] for s in syms]
  movable = sections_of(args.objdump, args.objs)

  def owner(a):
    if a < SPLIT:
      return "<stack>"
    i = bisect.bisect_right(starts, a) - 1
    if i >= 0 and syms[i][0] <= a < syms[i][0] + max(syms[i][1], 1):
      return syms[i][2]
    return "<heap/other>"

  weight, self_w, total = {}, {}, 0
  for line in open(args.pairlog):
    a, b, c = line.split()
    oa, ob, c = owner(int(a, 16)), owner(int(b, 16)), int(c)
    total += c
    if oa == ob:
      self_w[oa] = self_w.get(oa, 0) + c
    else:
      k = tuple(sorted((oa, ob)))
      weight[k] = weight.get(k, 0) + c

  bank = {"<stack>": 0, "<heap/other>": 1}
  objs = {o for k in weight for o in k} - set(bank)
  for o in objs:
    bank[o] = 1
  move_ok = {o for o in objs if o in movable}

  def cost(o, b):
    return sum(w for (x, y), w in weight.items()
               if o in (x, y) and bank[y if x == o else x] == b)

  order = sorted(move_ok, key=lambda o: -sum(w for k, w in weight.items() if o in k))
  for o in order:                       # greedy
    bank[o] = 0 if cost(o, 0) < cost(o, 1) else 1
  for _ in range(20):                   # refine
    changed = False
    for o in order:
      other = 1 - bank[o]
      if cost(o, other) < cost(o, bank[o]):
        bank[o] = other
        changed = True
    if not changed:
      break

  same = sum(w for (x, y), w in weight.items() if bank[x] == bank[y])
  unfixable = sum(self_w.values())
  to_move = sorted(o for o in move_ok if bank[o] == 0)
  print("pairs logged: %d" % total)
  print("  inside one object (placement cannot help): %d (%.0f%%)" % (unfixable, 100.0 * unfixable / total if total else 0))
  print("  between objects, same bank after placement: %d (%.0f%%)" % (same, 100.0 * same / total if total else 0))
  print("  moved to bank 0: %s" % (", ".join(to_move) or "none"))
  for o, w in sorted(self_w.items(), key=lambda kv: -kv[1])[:5]:
    print("  self-pairs in %s: %d" % (o, w))

  ld = open(args.ld).read()
  ld = ld.replace("  RAM (rwx) : ORIGIN = 0x80000000, LENGTH = 16M\n",
                  "  RAM (rwx) : ORIGIN = 0x80000000, LENGTH = 16M\n"
                  "  BANK0 (rw) : ORIGIN = 0x%08x, LENGTH = 1M\n" % BANK0_ORIGIN)
  inputs = "\n".join("    *(%s)" % movable[o] for o in to_move)
  block = ("  /* Bank-aware placement (place_banks.py): objects moved into the\n"
           "     stack's bank, away from the globals they are loaded with. */\n"
           "  .bank0 : {\n%s\n  } > BANK0\n\n" % inputs) if to_move else ""
  ld = ld.replace("  .rodata : {", block + "  .rodata : {", 1)
  open(args.out_ld, "w").write(ld)


if __name__ == "__main__":
  main()
