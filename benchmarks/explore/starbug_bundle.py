#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Basic-block bundler for STARBUG rule-set experiments.

Reads a GCC -O2 RISC-V assembly file and re-emits it with `c.li x0,N` bundle
hints, packing each basic block under one of four rule sets:

  R0  current STARBUG contract (paper Table 1)
  R1  R0 + a second data-memory port that can do one extra LOAD per bundle
  R2  R0 + a branch/jump may end a bundle (it takes lane 0, like memory)
  R3  R1 + R2
  R2b R0 + a branch/jump may end a bundle on its own lane (does not compete
      with the memory port for lane 0)
  R3b R1 + R2b

Rules shared by every set:
  * at most 4 instructions per bundle
  * no RAW, WAW or WAR register dependence between members of one bundle
  * memory order: a store never shares a bundle with, or passes, another
    memory op (no alias analysis); loads may pair and reorder among loads
  * lane 0 holds at most one of {memory op on port A, branch/jump}
  * calls, CSR/system ops, FP, multi-instruction pseudos (call, tail, la, big
    li) and anything unrecognised are barriers: never bundled, never crossed
  * scheduling never crosses a basic-block boundary (label or control op)

Rule-set specifics:
  R0/R2  one memory op per bundle (port A, lane 0)
  R1/R3  plus one load on port B (the second memory bank's port)
  R0/R1  a branch/jump always issues alone
  R2/R3  a branch/jump may be the LAST member of the block's final bundle,
         taking lane 0. R2b/R3b: same, but the branch has its own lane, so a
         memory op can still use lane 0 in that bundle. Members are all earlier in program order, so they
         execute on both paths and the bundle still runs correctly on a scalar
         core that ignores the hint.

Emission order inside a bundle: port-A memory op, port-B load, ALU ops in
program order, branch last. With no intra-bundle dependences any order is
equivalent; this one keeps R0 binaries legal for the RTL bundle checker
(memory op in slot 0) and keeps branches last for sequential execution.

Outputs <out>.s and <out>.sched.json (per-block schedules with the reason each
instruction landed where it did). Every block, bundled or not, gets a label
`Lsb_<file>_<n>` so execution counts can be joined back to blocks.

usage: starbug_bundle.py IN.s OUT.s --rules {scalar,R0,R1,R2,R3} --file-id N
"""

import argparse
import json
import re
import sys

ABI = (["zero", "ra", "sp", "gp", "tp", "t0", "t1", "t2", "s0", "s1"] +
       ["a%d" % i for i in range(8)] + ["s%d" % i for i in range(2, 12)] +
       ["t3", "t4", "t5", "t6"])
FABI = (["ft%d" % i for i in range(8)] + ["fs0", "fs1"] +
        ["fa%d" % i for i in range(8)] + ["fs%d" % i for i in range(2, 12)] +
        ["ft%d" % i for i in range(8, 12)])
REG = {}
for i, n in enumerate(ABI):
  REG[n] = i
  REG["x%d" % i] = i
REG["fp"] = 8
for i, n in enumerate(FABI):
  REG[n] = 32 + i
  REG["f%d" % i] = 32 + i


def regname(r):
  return ABI[r] if r < 32 else FABI[r - 32]


R_TYPE = {"add", "sub", "sll", "slt", "sltu", "xor", "srl", "sra", "or", "and"}
M_TYPE = {"mul", "mulh", "mulhsu", "mulhu", "div", "divu", "rem", "remu"}
I_TYPE = {"addi", "slti", "sltiu", "xori", "ori", "andi", "slli", "srli", "srai"}
PSEUDO_RR = {"mv", "not", "neg", "seqz", "snez", "sltz", "sgtz"}
LOADS = {"lb", "lbu", "lh", "lhu", "lw"}
STORES = {"sb", "sh", "sw"}
# STARBUG2 post-increment forms (custom-1): access at rs1, then rs1 += imm.
PI_FUNCT3 = {"lb.pi": 0, "lh.pi": 1, "lw.pi": 2, "lbu.pi": 4, "lhu.pi": 5,
             "sw.pi": 3, "sb.pi": 6, "sh.pi": 7}
BR2 = {"beq", "bne", "blt", "bge", "bltu", "bgeu", "bgt", "ble", "bgtu", "bleu"}
BR1 = {"beqz", "bnez", "blez", "bgez", "bltz", "bgtz"}
MEM_RE = re.compile(r"^(.*)\(\s*([A-Za-z0-9]+)\s*\)$")


class Insn:
  def __init__(self, text, mnem, ops):
    self.text = text.strip()
    self.mnem = mnem
    self.ops = ops
    self.cls = "barrier"
    self.defs = set()
    self.uses = set()
    self.why_barrier = ""
    self.emit = self.text    # what is written to the output assembly
    self.pred = None         # STARBUG2 predicate: (register, z) or None

  @property
  def is_mem(self):
    return self.cls in ("load", "store")

  @property
  def is_ctl(self):
    return self.cls in ("branch", "jump")


def reg(tok):
  tok = tok.strip()
  if tok not in REG:
    raise ValueError(tok)
  return REG[tok]


def classify(text):
  parts = text.strip().split(None, 1)
  mnem = parts[0]
  ops = [o.strip() for o in parts[1].split(",")] if len(parts) > 1 else []
  ins = Insn(text, mnem, ops)
  try:
    if mnem in R_TYPE or mnem in M_TYPE:
      ins.cls = "mul" if mnem in M_TYPE else "alu"
      ins.defs, ins.uses = {reg(ops[0])}, {reg(ops[1]), reg(ops[2])}
    elif mnem in I_TYPE:
      ins.cls, ins.defs, ins.uses = "alu", {reg(ops[0])}, {reg(ops[1])}
    elif mnem in PSEUDO_RR:
      ins.cls, ins.defs, ins.uses = "alu", {reg(ops[0])}, {reg(ops[1])}
    elif mnem == "lui":
      ins.cls, ins.defs = "alu", {reg(ops[0])}
    elif mnem == "li":
      v = int(ops[1], 0)
      if -2048 <= v <= 2047 or (v & 0xFFF) == 0 and -(1 << 31) <= v < (1 << 32):
        ins.cls, ins.defs = "alu", {reg(ops[0])}
      else:
        ins.why_barrier = "li expands to 2 instructions"
    elif mnem == "nop":
      ins.cls = "alu"
    elif mnem in PI_FUNCT3:
      m = MEM_RE.match(ops[1])
      base, imm = reg(m.group(2)), int(m.group(1), 0)
      if mnem.startswith("l"):
        ins.cls, ins.defs, ins.uses = "load", {reg(ops[0]), base}, {base}
        ins.emit = ".insn i 0x2b, %d, %s, %s, %d" % (PI_FUNCT3[mnem], ops[0], m.group(2), imm)
      else:
        ins.cls, ins.defs, ins.uses = "store", {base}, {reg(ops[0]), base}
        ins.emit = ".insn s 0x2b, %d, %s, %d(%s)" % (PI_FUNCT3[mnem], ops[0], imm, m.group(2))
    elif mnem in LOADS:
      m = MEM_RE.match(ops[1])
      ins.cls, ins.defs, ins.uses = "load", {reg(ops[0])}, {reg(m.group(2))}
    elif mnem in STORES:
      m = MEM_RE.match(ops[1])
      ins.cls, ins.uses = "store", {reg(ops[0]), reg(m.group(2))}
    elif mnem in BR2:
      ins.cls, ins.uses = "branch", {reg(ops[0]), reg(ops[1])}
    elif mnem in BR1:
      ins.cls, ins.uses = "branch", {reg(ops[0])}
    elif mnem == "j":
      ins.cls = "jump"
    elif mnem == "jr":
      ins.cls, ins.uses = "jump", {reg(ops[0])}
    elif mnem == "ret":
      ins.cls, ins.uses = "jump", {REG["ra"]}
    else:
      ins.why_barrier = "not bundleable (%s)" % mnem
  except (ValueError, IndexError, AttributeError):
    ins.cls, ins.why_barrier = "barrier", "unparsed operands"
  ins.defs.discard(0)
  ins.uses.discard(0)
  return ins


class Rules:
  def __init__(self, name):
    self.name = name
    self.bundle = name != "scalar"
    self.extra_load = name in ("R1", "R3", "R3b")
    self.branch_in_bundle = name in ("R2", "R3", "R2b", "R3b")
    self.branch_own_lane = name in ("R2b", "R3b")


class Bundle:
  def __init__(self):
    self.members = []      # indices into the block
    self.lane0 = None      # None | "load" | "store" | "ctl"
    self.lane0_idx = None
    self.portb = None      # index of the port-B load


def dependence(a, b):
  """Why b (later in program order) cannot share a bundle with or precede a."""
  raw = a.defs & b.uses
  if raw:
    return "RAW on %s" % regname(min(raw))
  waw = a.defs & b.defs
  if waw:
    return "WAW on %s" % regname(min(waw))
  war = a.uses & b.defs
  if war:
    return "WAR on %s" % regname(min(war))
  if a.is_mem and b.is_mem and "store" in (a.cls, b.cls):
    return "memory order (store)"
  return None


def try_place(ins, idx, b, rules):
  """Add instruction idx to bundle b if resources allow. Returns None on
  success or the reason it does not fit."""
  if len(b.members) >= 4:
    return "bundle already has 4 instructions"
  if ins.cls == "load":
    if b.lane0 is None:
      b.lane0, b.lane0_idx = "load", idx
    elif rules.extra_load and b.portb is None:
      b.portb = idx
    elif rules.extra_load:
      return "both load ports taken"
    else:
      return "lane 0 (only memory port) taken"
  elif ins.cls == "store":
    if b.lane0 is not None:
      return "lane 0 (memory port A) taken"
    b.lane0, b.lane0_idx = "store", idx
  elif ins.is_ctl and rules.branch_own_lane:
    pass                                   # dedicated branch lane
  elif ins.is_ctl:
    if b.lane0 == "load" and rules.extra_load and b.portb is None:
      b.portb, b.lane0, b.lane0_idx = b.lane0_idx, None, None  # load -> port B
    if b.lane0 is not None:
      return "lane 0 taken"
    b.lane0, b.lane0_idx = "ctl", idx
  b.members.append(idx)
  return None


def schedule(block, rules):
  """List-schedule one basic block. Returns (bundles, reasons) where bundles
  is a list of member-index lists in emission order."""
  n = len(block)
  preds = [{} for _ in range(n)]
  for j in range(n):
    for i in range(j):
      d = dependence(block[i], block[j])
      if d:
        preds[j][i] = d
  ctl = n - 1 if block[-1].is_ctl else None
  body = [i for i in range(n) if i != ctl]
  # priority: longest dependence path to the end of the block
  height = [1] * n
  for i in reversed(range(n)):
    for j in range(i + 1, n):
      if i in preds[j]:
        height[i] = max(height[i], height[j] + 1)
  where = {}
  first_reject = {}
  bundles = []
  remaining = list(body)
  while remaining:
    b = Bundle()
    k = len(bundles)
    ready = [i for i in remaining if all(p in where for p in preds[i])]
    ready.sort(key=lambda i: (-height[i], i))
    for i in ready:
      why = try_place(block[i], i, b, rules)
      if why is None:
        where[i] = k
      else:
        first_reject.setdefault(i, (k, why, b.lane0_idx if "lane 0" in why else None))
    for i in b.members:
      remaining.remove(i)
    bundles.append(b)
  if ctl is not None:
    placed = False
    if rules.branch_in_bundle and bundles:
      last = bundles[-1]
      if not any(p in last.members for p in preds[ctl]):
        why = try_place(block[ctl], ctl, last, rules)
        if why is None:
          where[ctl] = len(bundles) - 1
          placed = True
        else:
          first_reject[ctl] = (len(bundles) - 1, why, last.lane0_idx)
    if not placed:
      b = Bundle()
      try_place(block[ctl], ctl, b, rules)
      where[ctl] = len(bundles)
      bundles.append(b)

  # reasons
  reasons = {}
  for i in range(n):
    k = where[i]
    earliest = max([where[p] + 1 for p in preds[i]], default=0)
    if i == ctl and not rules.branch_in_bundle:
      reasons[i] = "branches/jumps are never bundled under this rule set"
    elif i == ctl and len(bundles[k].members) > 1:
      reasons[i] = "joins the block's final bundle (a branch must be last)"
    elif k == earliest:
      if preds[i]:
        p = max(preds[i], key=lambda p: where[p])
        reasons[i] = "waits for `%s` (%s)" % (block[p].text, preds[i][p])
      else:
        reasons[i] = "ready at block start"
    else:
      rk, why, blocker = first_reject.get(i, (earliest, "scheduler priority", None))
      if blocker is not None and blocker != i:
        why += " by `%s`" % block[blocker].text
      if i == ctl:
        why = "could not join the final bundle: " + why
      reasons[i] = why

  ordered = []
  for b in bundles:
    alu = [i for i in b.members if i not in (b.lane0_idx, b.portb) and not block[i].is_ctl]
    order = ([b.lane0_idx] if b.lane0 in ("load", "store") else []) + \
            ([b.portb] if b.portb is not None else []) + sorted(alu) + \
            [i for i in b.members if block[i].is_ctl]
    ordered.append(order)
  return ordered, reasons


DIRECTIVE_OK = re.compile(r"^\s*\.(cfi_|loc\b)")
TEXT_SECTION = re.compile(r"^\s*\.(text\b|section\s+\.text)")
OTHER_SECTION = re.compile(r"^\s*\.(section|data\b|bss\b|rodata\b)")
LABEL = re.compile(r"^([A-Za-z_.$][\w.$]*):")
PRED_MARK = re.compile(r"#@pred\s+(\w+)\s+([01])")   # set by starbug2_transform


def hint_header(insns, bnd):
  """Bundle marker for the STARBUG1 hint encoding."""
  return ["\tc.li\tx0,%d\n" % len(bnd)]


def process(lines, rules, file_id, schedule_fn=None, header_fn=hint_header):
  """Re-emit an assembly file with every basic block bundled.

  schedule_fn(insns) -> (bundles, reasons) defaults to this file's R0-R3b
  scheduler; header_fn(insns, bundle) returns the marker lines placed before
  each multi-instruction bundle (see starbug2_bundle.py for the custom-0
  header).
  """
  if schedule_fn is None:
    schedule_fn = lambda insns: schedule(insns, rules)
  out = ["\t.option norelax\t# bundles hold an exact instruction count\n"]
  blocks_json = []
  in_text = False
  func = None
  block = []
  bid = [0]

  def emit_block(insns, label_note=None):
    if not insns:
      return
    label = "Lsb_%d_%d" % (file_id, bid[0])
    bid[0] += 1
    out.append("%s:\n" % label)
    if insns[0].cls == "barrier" or not rules.bundle:
      bundles = [[i] for i in range(len(insns))]
      reasons = {i: (insns[i].why_barrier or "scalar build") for i in range(len(insns))}
    else:
      bundles, reasons = schedule_fn(insns)
    for bnd in bundles:
      if len(bnd) > 1 or insns[bnd[0]].pred:
        out.extend(header_fn(insns, bnd))
      for i in bnd:
        out.append("\t%s\n" % insns[i].emit)
    blocks_json.append({
        "label": label, "function": func, "insns": [x.text for x in insns],
        "classes": [x.cls for x in insns], "bundles": bundles,
        "preds": [("%s%s" % ("!" if x.pred[1] else "", x.pred[0])) if x.pred else ""
                  for x in insns],
        "lines": [getattr(x, "line", None) for x in insns],
        "reasons": [reasons[i] for i in range(len(insns))]})

  for line_no, line in enumerate(lines):
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
      out.append(line)
      continue
    if TEXT_SECTION.match(line):
      emit_block(block); block = []
      in_text = True
      out.append(line)
      continue
    if OTHER_SECTION.match(line):
      emit_block(block); block = []
      in_text = False
      out.append(line)
      continue
    if not in_text:
      out.append(line)
      continue
    m = LABEL.match(line)
    if m:
      emit_block(block); block = []
      if not m.group(1).startswith(".L"):
        func = m.group(1)
      out.append(line)
      continue
    if stripped.startswith("."):
      if DIRECTIVE_OK.match(line):
        continue                        # drop debug/unwind noise
      emit_block(block); block = []
      out.append(line)
      continue
    ins = classify(stripped.split("#")[0])
    ins.line = line_no
    pm = PRED_MARK.search(stripped)
    if pm:
      ins.pred = (pm.group(1), int(pm.group(2)))
      ins.uses.add(reg(pm.group(1)))
    if ins.cls == "barrier":
      emit_block(block); block = []
      emit_block([ins])
      continue
    block.append(ins)
    if ins.is_ctl:
      emit_block(block); block = []
  emit_block(block)
  return out, blocks_json


UNCONDITIONAL = ("j", "jr", "ret", "tail")
LOOP_LABEL = re.compile(r"^(\.L\w+):")


def code_bytes(line):
  """Upper bound on the bytes a line assembles to (4 per instruction/word)."""
  t = line.split("#")[0].split()
  if not t or t[0].endswith(":"):
    return 0
  if t[0] == ".word":
    return 4
  return 0 if t[0].startswith(".") else 4


def align_loops(lines, line_bytes):
  """Fetch-aware layout: start each small loop (a label that a later branch
  jumps back to, with a body that fits in one I-cache line even if nothing
  is compressed) on a line boundary, so none of its packets crosses a line.
  Larger loops are left alone: aligning them only moves the crossings. The
  padding is jumped over: if the code before the loop can fall into it, a
  `j` to the loop is inserted, costing one cycle per loop entry instead of
  executing the padding."""
  label_at, backward = {}, set()
  for idx, line in enumerate(lines):
    m = LOOP_LABEL.match(line)
    if m:
      label_at[m.group(1)] = idx
      continue
    t = line.split("#")[0].split()
    if t and not t[0].startswith(".") and not t[0].endswith(":"):
      target = t[-1].split(",")[-1]
      if target in label_at and label_at[target] < idx:
        body = sum(code_bytes(l) for l in lines[label_at[target]:idx + 1])
        if body <= line_bytes:
          backward.add(target)
  out, last_insn = [], None
  for line in lines:
    m = LOOP_LABEL.match(line)
    if m and m.group(1) in backward:
      if last_insn not in UNCONDITIONAL:
        out.append("\tj\t%s\t\t# jump over the alignment padding\n" % m.group(1))
      out.append("\t.balign\t%d\n" % line_bytes)
    out.append(line)
    t = line.split("#")[0].split()
    if t and not t[0].startswith(".") and not t[0].endswith(":"):
      last_insn = t[0]
  return out


def main():
  ap = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("input")
  ap.add_argument("output")
  ap.add_argument("--rules", required=True, choices=["scalar", "R0", "R1", "R2", "R3", "R2b", "R3b"])
  ap.add_argument("--file-id", type=int, default=0)
  ap.add_argument("--align-loops", type=int, default=0, metavar="BYTES",
                  help="start small loops on BYTES-aligned lines (0 = off)")
  args = ap.parse_args()
  out, blocks = process(open(args.input).readlines(), Rules(args.rules), args.file_id)
  if args.align_loops:
    out = align_loops(out, args.align_loops)
  open(args.output, "w").writelines(out)
  json.dump({"rules": args.rules, "file_id": args.file_id, "blocks": blocks},
            open(args.output.rsplit(".", 1)[0] + ".sched.json", "w"), indent=1)


if __name__ == "__main__":
  main()
