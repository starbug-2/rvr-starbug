#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check every STARBUG2 execute packet in a linked ELF against a machine
configuration (see starbug2_bundle.py for the rules).

Packets are re-derived from the binary: each custom-0 header word is decoded
(thermometer count, or the wide form with a second word) and its members are
decoded with starbug-llvm's instruction decoder, independently of the
bundler. Checks per packet:
  * the header's op count is legal and that many members follow
  * no member is itself a header, a c.li hint, or a call/system/FP/unknown op
  * no RAW or WAW between members; a WAR pair must have the reader first
    (MPACT executes members in order, the hardware reads before it writes)
  * a store shares its packet with no other memory op
  * ops fit the lanes: --width, --extra-load, --branch, --mdu-lanes
  * predicate nibbles select one of the --pred-count registers; ops with
    complementary predicates (same register, opposite z) may write the same
    register and are exempt from the dependence and memory checks
Custom-1 post-increment loads/stores are decoded here (they write rs1 too).
Also counts packets that span a 64-byte line (the fetch penalty).

usage: rule_check2.py ELF --width 4 [--extra-load] [--branch none|lane0|own]
                          [--mdu-lanes N] [--starbug-llvm DIR] [--objdump PATH]
"""

import argparse
import os
import sys

import starbug2_bundle as sb2

OPCODE = 0b0001011


def decode_member(isa, addr, word, size, text):
  """starbug-llvm's decoder, plus the custom-1 post-increment forms."""
  if size == 4 and (word & 0x7f) == 0b0101011:
    f3, rs1 = (word >> 12) & 7, (word >> 15) & 31
    if f3 in (0, 1, 2, 4, 5):
      return isa.Insn(addr, word, 4, isa.LOAD, (rs1,), ((word >> 7) & 31, rs1), text)
    return isa.Insn(addr, word, 4, isa.STORE, (rs1, (word >> 20) & 31), (rs1,), text)
  return isa.decode(addr, word, text)


def op_count(hdr):
  if (hdr >> 7) & 1:
    return ((hdr >> 13) & 7) + 1
  p = ((hdr >> 15) & 1, (hdr >> 14) & 1, (hdr >> 13) & 1)
  return {(0, 0, 0): 1, (1, 0, 0): 2, (1, 1, 0): 3, (1, 1, 1): 4}.get(p, -1)


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("elf")
  ap.add_argument("--width", type=int, choices=[4, 8], required=True)
  ap.add_argument("--extra-load", action="store_true")
  ap.add_argument("--branch", choices=["none", "lane0", "own"], default="none")
  ap.add_argument("--mdu-lanes", type=int, default=None)
  ap.add_argument("--line-bytes", type=int, default=64)
  ap.add_argument("--pred-count", type=int, default=7)
  ap.add_argument("--starbug-llvm", default=os.path.join(
      os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "starbug-llvm"))
  ap.add_argument("--objdump", default=None)
  ap.add_argument("--max-report", type=int, default=10)
  args = ap.parse_args()
  sys.path.insert(0, os.path.join(args.starbug_llvm, "tools"))
  import starbug_isa as isa
  import starbug_bundle_check as chk

  m = sb2.Machine(args.width, args.extra_load, args.branch, args.mdu_lanes)
  cls_map = {isa.ALU: "alu", isa.MUL: "mul", isa.LOAD: "load", isa.STORE: "store",
             isa.BRANCH: "branch", isa.JUMP: "jump"}
  insns = chk.disassemble(args.elf, chk.find_objdump(args.objdump))
  bad, sizes, packets, crossing = [], {}, 0, 0
  i = 0
  while i < len(insns):
    addr, word, size, _text, _f = insns[i]
    if size == 2 and isa.hint_length(word) is not None:
      bad.append((addr, ["STARBUG1 c.li hint in a STARBUG2 binary"]))
    if size != 4 or (word & 0x7f) != OPCODE:
      i += 1
      continue
    packets += 1
    errs = []
    n = op_count(word)
    j = i + (2 if (word >> 7) & 1 else 1)
    if n < 1 or n > args.width:
      errs.append("illegal op count in header 0x%08x" % word)
      n = max(n, 0)
    members = []
    words = [word, insns[i + 1][1] if (word >> 7) & 1 and i + 1 < len(insns) else 0]
    preds = []
    for k in range(j, min(j + n, len(insns))):
      a2, w2, s2, t2, _ = insns[k]
      if s2 == 4 and (w2 & 0x7f) == OPCODE:
        errs.append("header inside a packet")
      members.append(decode_member(isa, a2, w2, s2, t2))
      slot = k - j
      nib = (words[slot // 4] >> (28 - 4 * (slot % 4))) & 0xf
      creg, z = nib >> 1, nib & 1
      if creg > args.pred_count or (creg == 0 and z):
        errs.append("bad predicate nibble %x" % nib)
      preds.append((creg, z) if creg else None)
    if len(members) != n:
      errs.append("header declares %d ops, %d follow" % (n, len(members)))
    sizes[n] = sizes.get(n, 0) + 1
    end = members[-1].addr + members[-1].size - 1 if members else addr + 3
    if addr // args.line_bytes != end // args.line_bytes:
      crossing += 1
    classes = []
    for x in members:
      if x.cls not in cls_map:
        errs.append("op not allowed in a packet: %s" % x.text)
      classes.append(cls_map.get(x.cls, "alu"))
    def complementary(a, b):
      return preds[a] and preds[b] and preds[a][0] == preds[b][0] and preds[a][1] != preds[b][1]

    for a in range(len(members)):
      for b in range(a + 1, len(members)):
        if complementary(a, b):
          continue
        x, y = members[a], members[b]
        if x.writes & y.reads:
          errs.append("RAW x%d: %s -> %s" % (min(x.writes & y.reads), x.text, y.text))
        if x.writes & y.writes:
          errs.append("WAW x%d: %s, %s" % (min(x.writes & y.writes), x.text, y.text))
    mem = [k for k, x in enumerate(members) if x.uses_lsu]
    for a in mem:
      for b in mem:
        if a < b and not complementary(a, b) and isa.STORE in (members[a].cls, members[b].cls):
          errs.append("a store shares its packet with another memory op")
    if not m.fits(classes):
      errs.append("ops do not fit the lanes: %s" % " ".join(classes))
    if errs:
      bad.append((addr, errs))
    i = j + n
  print("%s: %d packets, sizes %s, %d span a %d-byte line, %d break the rules" % (
      os.path.basename(args.elf), packets, dict(sorted(sizes.items())), crossing,
      args.line_bytes, len(bad)))
  for addr, errs in bad[:args.max_report]:
    print("  @%#x: %s" % (addr, "; ".join(errs)))
  sys.exit(1 if bad else 0)


if __name__ == "__main__":
  main()
