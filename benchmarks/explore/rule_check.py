#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check every bundle in a linked ELF against a STARBUG rule set (R0-R3).

Independent of the bundler: bundles are re-derived from the instruction
encodings with starbug-llvm's decoder (tools/starbug_isa.py), so a bundler bug
or an assembler surprise (e.g. a relaxed branch turning into two instructions)
shows up here.

  R0  1-4 members; memory/control/system ops only in slot 0; one memory op;
      no branches/jumps in bundles; no RAW/WAW/WAR between members
  R1  as R0, plus one extra LOAD allowed in slot 1 (second memory port)
  R2  as R0, plus one branch/jump allowed, and only as the LAST member; it
      takes lane 0, so the bundle then has no port-A memory op
  R3  R1 + R2 (a bundle ending in a branch may still hold one port-B load)
  R2b as R2, but the branch has its own lane: slot 0 may still hold a memory op
  R3b R1 + R2b

Exit status 1 if any bundle breaks the rules.
usage: rule_check.py ELF --rules R0 [--starbug-llvm DIR] [--objdump PATH]
"""

import argparse
import os
import sys


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("elf")
  ap.add_argument("--rules", required=True, choices=["R0", "R1", "R2", "R3", "R2b", "R3b"])
  ap.add_argument("--starbug-llvm", default=os.path.join(
      os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "starbug-llvm"))
  ap.add_argument("--objdump", default=None)
  ap.add_argument("--max-report", type=int, default=10)
  args = ap.parse_args()
  sys.path.insert(0, os.path.join(args.starbug_llvm, "tools"))
  import starbug_isa as isa
  import starbug_bundle_check as chk

  extra_load = args.rules in ("R1", "R3", "R3b")
  branch_ok = args.rules in ("R2", "R3", "R2b", "R3b")
  branch_own_lane = args.rules in ("R2b", "R3b")
  insns = chk.disassemble(args.elf, chk.find_objdump(args.objdump))
  bundles, _ = chk.analyse(insns, 64)
  ctl = {isa.BRANCH, isa.JUMP}
  bad = []
  sizes = {}
  for b in bundles:
    m = b.members
    errs = []
    sizes[len(m)] = sizes.get(len(m), 0) + 1
    if not 1 <= b.declared_len <= 4 or len(m) != b.declared_len:
      errs.append("bad length %d" % b.declared_len)
    for i in range(len(m)):
      for j in range(i + 1, len(m)):
        for kind, regs in (("RAW", m[i].writes & m[j].reads),
                           ("WAW", m[i].writes & m[j].writes),
                           ("WAR", m[i].reads & m[j].writes)):
          if regs:
            errs.append("%s x%d between %s and %s" % (kind, min(regs), m[i].text, m[j].text))
    mem = [i for i, x in enumerate(m) if x.uses_lsu]
    stores = [i for i in mem if m[i].cls != isa.LOAD]
    br = [i for i, x in enumerate(m) if x.cls in ctl]
    other_l0 = [i for i, x in enumerate(m)
                if x.is_lane0_only and not x.uses_lsu and x.cls not in ctl]
    if other_l0:
      errs.append("system/pc-relative/unknown op in a bundle: %s" % m[other_l0[0]].text)
    if br and not branch_ok:
      errs.append("branch/jump in a bundle: %s" % m[br[0]].text)
    if len(br) > 1 or (br and br[0] != len(m) - 1):
      errs.append("branch/jump must be the single last member")
    port_a = [i for i in mem if i == 0]
    port_b = [i for i in mem if i != 0]
    if br and not branch_own_lane:   # the branch owns lane 0 / port A
      port_b, port_a = mem, []
    if len(port_a) > 1:
      errs.append("two memory ops on port A")
    if port_b:
      if not extra_load:
        errs.append("memory op outside slot 0: %s" % m[port_b[0]].text)
      elif len(port_b) > 1 or any(m[i].cls != isa.LOAD for i in port_b):
        errs.append("port B allows a single extra load only")
      elif not br and port_b[0] != 1:
        errs.append("extra load must be in slot 1")
    if stores and len(mem) > 1:
      errs.append("a store shares its bundle with another memory op")
    if errs:
      bad.append((b, errs))
  print("%s: %d bundles, sizes %s, %d break %s" % (
      os.path.basename(args.elf), len(bundles), dict(sorted(sizes.items())), len(bad), args.rules))
  for b, errs in bad[:args.max_report]:
    print("  @%#x: %s" % (b.hint_addr, "; ".join(errs)))
  sys.exit(1 if bad else 0)


if __name__ == "__main__":
  main()
