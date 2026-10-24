#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Basic-block bundler for the STARBUG2 custom-0 execute-packet design.

Like starbug_bundle.py (same parser, block splitting and report data), but
packs blocks under the STARBUG2 rules and emits custom-0 headers:

  header word  31:16 creg/z nibble per op 0-3 (all zero here: unconditional)
               15:13 4-way form: thermometer op count (p0 p1 p2)
                     wide form:  op count - 1 in binary
               7     W: wide form, a second word follows (31:16 nibbles for
                     ops 4-7, bits 6:0 also custom-0)
               6:0   0001011 (custom-0)
Packets of up to 4 ops always use the one-word form, also on the 8-way
machine; packets of 5-8 ops use the two-word form.

Machine model (--width 4 or 8):
  lane 0       ALU, MUL*, the memory port (loads and stores), branch if
               --branch lane0
  lane 1       ALU, MUL*, plus a second load port if --extra-load
  lane W-1     ALU, MUL*, branch if --branch own
  other lanes  ALU, MUL*
  * MUL/DIV only on lanes below --mdu-lanes (default: all lanes)
Ops are steered to lanes by type, so packet order is free; a packet is legal
if some assignment of its ops to capable lanes exists.

Packet semantics (header spec): every op reads its sources at the start of
the cycle and commits at the end. Hence inside one packet:
  * RAW and WAW are illegal (a packet issues in one cycle)
  * WAR is legal; ops are emitted in program order, so a reader always
    precedes the writer and MPACT's in-order execution of the members gives
    the same result as the parallel hardware
  * a store never shares a packet with, or passes, another memory op
  * a branch may share a packet (--branch lane0/own); all members execute
    and the PC changes at the end of the cycle. Only ops from the branch's
    own block join it, so the packet is correct on both paths.
Calls, CSR/system, FP and multi-instruction pseudos stay scalar, and nothing
crosses a basic-block boundary (a branch target must start a packet).

With --align-loops 64, loops start on a 64-byte line (fetch-aware layout);
see starbug_bundle.align_loops().

usage: starbug2_bundle.py IN.s OUT.s --width 4 [--extra-load]
                          [--branch none|lane0|own] [--mdu-lanes N]
                          [--align-loops 64] --file-id N
"""

import argparse
import json

import starbug_bundle as sb
import starbug2_transform as tf

OPCODE = 0b0001011


class Machine:
  def __init__(self, width, extra_load, branch, mdu_lanes):
    self.width = width
    self.extra_load = extra_load
    self.branch = branch
    self.mdu_lanes = width if mdu_lanes is None else mdu_lanes
    self.lanes = []
    for i in range(width):
      caps = {"alu"}
      if i < self.mdu_lanes:
        caps.add("mul")
      if i == 0:
        caps |= {"load", "store"}
      if i == 1 and extra_load:
        caps.add("load")
      if (branch == "lane0" and i == 0) or (branch == "own" and i == width - 1):
        caps |= {"branch", "jump"}
      self.lanes.append(caps)

  def fits(self, classes):
    """True if ops of these classes can each get a distinct capable lane."""
    if len(classes) > self.width:
      return False
    if sum(c in ("load", "store") for c in classes) > 1 + self.extra_load:
      return False
    lane_of = [None] * self.width

    def assign(op, seen):
      for ln in range(self.width):
        if classes[op] in self.lanes[ln] and ln not in seen:
          seen.add(ln)
          if lane_of[ln] is None or assign(lane_of[ln], seen):
            lane_of[ln] = op
            return True
      return False

    return all(assign(op, set()) for op in range(len(classes)))


def schedule(block, m):
  """List-schedule one basic block into packets. Returns (packets, reasons);
  each packet lists member indices in program (= emission) order."""
  n = len(block)
  strict = [dict() for _ in range(n)]   # pred -> reason: must be an earlier packet
  war = [dict() for _ in range(n)]      # pred -> reason: same packet allowed
  for j in range(n):
    for i in range(j):
      a, b = block[i], block[j]
      if a.pred and b.pred and a.pred[0] == b.pred[0] and a.pred[1] != b.pred[1]:
        continue            # complementary predicates: never both execute
      raw, waw, wr = a.defs & b.uses, a.defs & b.defs, a.uses & b.defs
      if raw:
        strict[j][i] = "RAW on %s" % sb.regname(min(raw))
      elif waw:
        strict[j][i] = "WAW on %s" % sb.regname(min(waw))
      elif a.is_mem and b.is_mem and "store" in (a.cls, b.cls):
        strict[j][i] = "memory order (store)"
      elif wr:
        war[j][i] = "WAR on %s" % sb.regname(min(wr))
  ctl = n - 1 if block[-1].is_ctl else None
  height = [1] * n
  for i in reversed(range(n)):
    for j in range(i + 1, n):
      if i in strict[j] or i in war[j]:
        height[i] = max(height[i], height[j] + (1 if i in strict[j] else 0))

  where, reject, packets = {}, {}, []
  remaining = [i for i in range(n) if i != ctl]
  while remaining:
    k = len(packets)
    members = []
    progress = True
    while progress and len(members) < m.width:
      # Repeat while ops are added: an op whose WAR predecessor just joined
      # this packet becomes ready for the same packet.
      progress = False
      ready = [i for i in remaining if i not in members
               and all(p in where for p in strict[i])
               and all(p in where or p in members for p in war[i])]
      ready.sort(key=lambda i: (-height[i], i))
      for i in ready:
        if m.fits([block[x].cls for x in members + [i]]):
          members.append(i)
          progress = True
        else:
          reject.setdefault(i, k)
    for i in members:
      where[i] = k
      remaining.remove(i)
    packets.append(sorted(members))
  if ctl is not None:
    joined = False
    if m.branch != "none" and packets:
      last = packets[-1]
      if not any(p in last for p in strict[ctl]) and \
         m.fits([block[x].cls for x in last + [ctl]]):
        last.append(ctl)
        where[ctl] = len(packets) - 1
        joined = True
      else:
        reject[ctl] = len(packets) - 1
    if not joined:
      where[ctl] = len(packets)
      packets.append([ctl])

  reasons = []
  for i in range(n):
    k = where[i]
    earliest = max([where[p] + 1 for p in strict[i]] + [where[p] for p in war[i]], default=0)
    if i == ctl and m.branch == "none":
      reasons.append("branches/jumps are never packed under this configuration")
    elif i == ctl and len(packets[k]) > 1:
      reasons.append("joins the block's final packet")
    elif k == earliest:
      binding = [p for p in strict[i] if where[p] + 1 == k] or [p for p in war[i] if where[p] == k]
      if binding:
        p = binding[0]
        why = strict[i].get(p) or war[i].get(p)
        reasons.append("waits for `%s` (%s)" % (block[p].text, why))
      else:
        reasons.append("ready at block start")
    else:
      cls = block[i].cls
      if i == ctl:
        reasons.append("could not join the final packet: no lane left that can branch")
      elif cls in ("load", "store"):
        reasons.append("memory port(s) taken")
      elif cls == "mul":
        reasons.append("no free lane with a multiplier")
      else:
        reasons.append("packet full (%d lanes)" % m.width)
  return packets, reasons


def make_header(pred_regs):
  """Header emitter: creg k (1-7) selects pred_regs[k-1]."""
  def nibbles(insns, ops):
    v = 0
    for slot, i in enumerate(ops):
      if insns[i].pred:
        reg, z = insns[i].pred
        v |= (((pred_regs.index(reg) + 1) << 1) | z) << (28 - 4 * slot)
    return v

  def header(insns, pkt):
    n = len(pkt)
    if n <= 4:
      therm = {1: 0b000, 2: 0b100, 3: 0b110, 4: 0b111}[n]
      return ["\t.word\t0x%08x\t\t# starbug2 header: %d ops\n"
              % (nibbles(insns, pkt) | (therm << 13) | OPCODE, n)]
    w0 = nibbles(insns, pkt[:4]) | ((n - 1) << 13) | (1 << 7) | OPCODE
    return ["\t.word\t0x%08x\t\t# starbug2 header (wide): %d ops\n" % (w0, n),
            "\t.word\t0x%08x\n" % (nibbles(insns, pkt[4:]) | OPCODE)]
  return header


header = make_header([])


def main():
  ap = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("input")
  ap.add_argument("output")
  ap.add_argument("--width", type=int, choices=[4, 8], required=True)
  ap.add_argument("--extra-load", action="store_true")
  ap.add_argument("--branch", choices=["none", "lane0", "own"], default="none")
  ap.add_argument("--mdu-lanes", type=int, default=None)
  ap.add_argument("--file-id", type=int, default=0)
  ap.add_argument("--align-loops", type=int, default=0, metavar="BYTES",
                  help="start loops on BYTES-aligned lines (0 = off)")
  ap.add_argument("--autoinc", action="store_true",
                  help="fuse zero-offset accesses with pointer increments (custom-1 .pi)")
  ap.add_argument("--ifconvert", action="store_true",
                  help="if-convert short if-then / if-then-else shapes with predicates")
  ap.add_argument("--ifconvert-all", action="store_true",
                  help="with --ifconvert, skip the profitability check")
  ap.add_argument("--pred-regs", default="s1,a0,a1,a2,a3,a4,a5",
                  help="registers selected by creg 1..N (default x9-x15)")
  ap.add_argument("--profile", default=None,
                  help="JSON {source line: count} from make_profile.py, for if-conversion")
  ap.add_argument("--reserved-regs", default="",
                  help="registers no compiled code uses (e.g. s8-s11 under -ffixed-*)")
  args = ap.parse_args()
  m = Machine(args.width, args.extra_load, args.branch, args.mdu_lanes)
  pred_regs = args.pred_regs.split(",")
  lines = open(args.input).readlines()
  stats = {}
  if args.autoinc:
    lines = tf.autoinc(lines, stats)
  if args.ifconvert:
    def cost(block_lines):
      insns = []
      for l in block_lines:
        ins = sb.classify(l.split("#")[0].strip())
        pm = sb.PRED_MARK.search(l)
        if pm:
          ins.pred = (pm.group(1), int(pm.group(2)))
          ins.uses.add(sb.REG[pm.group(1)])
        insns.append(ins)
      return len(schedule(insns, m)[0]) if insns else 0
    lines = tf.if_convert(lines, pred_regs, stats, cost_fn=cost, force=args.ifconvert_all,
                          reserved=[r for r in args.reserved_regs.split(",") if r],
                          branch_in_packet=m.branch != "none",
                          counts=({int(k): v for k, v in json.load(open(args.profile)).items()}
                                  if args.profile else None))
  rules = sb.Rules("R0")                 # only .bundle is consulted
  out, blocks = sb.process(lines, rules, args.file_id,
                           schedule_fn=lambda insns: schedule(insns, m),
                           header_fn=make_header(pred_regs))
  if args.align_loops:
    out = sb.align_loops(out, args.align_loops)
  open(args.output, "w").writelines(out)
  json.dump({"config": vars(args), "file_id": args.file_id, "transform_stats": stats,
             "blocks": blocks},
            open(args.output.rsplit(".", 1)[0] + ".sched.json", "w"), indent=1)


if __name__ == "__main__":
  main()
