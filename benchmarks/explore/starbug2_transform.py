# SPDX-License-Identifier: Apache-2.0
"""Assembly transforms for the STARBUG2 phase-2 features, applied before
packing (see starbug2_bundle.py --ifconvert / --autoinc).

Post-increment fusion (--autoinc)
  `lw rd,0(rp)` ... `addi rp,rp,k`  ->  `lw.pi rd,k(rp)`   (also lb/lh/lbu/lhu,
  sb/sh/sw), when the memory op is the last instruction to touch rp before the
  addi, its offset is 0, and (for loads) rd != rp. Requiring nothing in between
  to use rp means the fusion never adds a dependence. sp is left alone.

If-conversion (--ifconvert)
  if-then:       B: ...; b<cond> L      T: up to 4 ops       L: ...
  if-then-else:  B: ...; b<cond> Le     T: up to 4 ops; j Lj   Le: up to 4 ops   Lj:
  guard chain:   B: ...; b<c1> L  ops; b<c2> L  ops; [b<c3> X]  L:
                 (short-circuit &&: every guard skips to L). Each guard's
                 "continue" condition is computed as 0/1 and and-ed into p;
                 the ops and the optional exit branch run predicated on p.
  The branch is replaced by a compare that writes a predicate register p
  (none when the branch already tests a usable predicate register for zero),
  T runs predicated on "branch not taken", the else part on the complement,
  and the branch (and the j/Le label of the else form) disappear. T and the
  else part may not contain labels, calls, system/FP ops or control flow, and
  may not touch p. p must be dead after B, from a per-function liveness
  analysis (calls clobber caller-saved registers; ret keeps a0/a1, sp, ra and
  callee-saved registers live; an indirect jump keeps everything live).
  Predicated ops are tagged "#@pred <reg> <z>" for the bundler.
"""

import re

import starbug_bundle as sb

ALL = set(range(32))
CALLER_SAVED = {1, 5, 6, 7} | set(range(10, 18)) | set(range(28, 32))   # ra t0-6 a0-7
ARGS = set(range(10, 18))
RET_LIVE = {1, 2, 3, 4, 8, 9, 10, 11} | set(range(18, 28))            # ra sp gp tp s0-11 a0 a1
# Registers every compiled file leaves alone (setup B reserves s8-s11 with
# -ffixed-*): no caller expects them preserved, so they are dead at returns.
RESERVED = set()
REG_TOKEN = re.compile(r"\b(zero|ra|sp|gp|tp|fp|t[0-6]|s[0-9]|s1[01]|a[0-7]|x[0-9]+)\b")
LABEL = re.compile(r"^([A-Za-z_.$][\w.$]*):")
MAX_ARM = 4

# branch -> (compare op, operand order, z for the fall-through arm). The
# fall-through arm runs when the branch is NOT taken.
SWAP = {"bgt": "blt", "ble": "bge", "bgtu": "bltu", "bleu": "bgeu"}
ONE = {"blez": ("bge", "zero", 0), "bgez": ("bge", 0, "zero"),
       "bltz": ("blt", 0, "zero"), "bgtz": ("blt", "zero", 0)}


def code_tokens(line):
  t = line.split("#")[0].strip()
  if not t or t.startswith(".") or LABEL.match(line):
    return None
  return t


def regs_in(text):
  return {sb.REG[r] for r in REG_TOKEN.findall(text) if r in sb.REG} - {0}


def uses_defs(text):
  """Liveness view of one instruction: (uses, defs, kind) with kind one of
  'plain', 'branch', 'jump', 'ret', 'ijump', 'call', 'tail'."""
  ins = sb.classify(text)
  m = ins.mnem
  ops = ins.ops
  if m in ("call", "jal") or (m == "jalr" and ops and ops[0] != "zero"):
    return ARGS, set(CALLER_SAVED), "call"
  if m == "tail":
    return ARGS | (RET_LIVE - RESERVED), set(), "tail"
  if m == "ret" or (m == "jr" and ops and ops[0] == "ra"):    # GCC also writes `jr ra`
    return RET_LIVE - RESERVED, set(), "ret"
  if m == "jr":
    return regs_in(text), set(), "ijump"
  if ins.cls == "branch":
    return ins.uses, set(), "branch"
  if m == "j":
    return set(), set(), "jump"
  if ins.cls == "barrier":
    return regs_in(" ".join(ops)), set(), "plain"     # conservative: no defs
  return ins.uses, ins.defs, "plain"


def functions(lines):
  """Yield (start, end) line ranges of each function in text sections."""
  start = None
  in_text = False
  for i, line in enumerate(lines):
    if sb.TEXT_SECTION.match(line):
      in_text = True
    elif sb.OTHER_SECTION.match(line):
      in_text = False
      if start is not None:
        yield start, i
        start = None
    m = LABEL.match(line)
    if in_text and m and not m.group(1).startswith(".L"):
      if start is not None:
        yield start, i
      start = i
  if start is not None:
    yield start, len(lines)


class Block:
  def __init__(self):
    self.label = None
    self.lines = []           # indices of instruction lines
    self.succ = []
    self.live_in = set()
    self.live_out = set()


def liveness(lines, lo, hi):
  """Basic blocks of one function with live-in/live-out register sets."""
  blocks, cur, by_label = [], Block(), {}
  for i in range(lo, hi):
    m = LABEL.match(lines[i])
    if m:
      if cur.lines or cur.label:
        blocks.append(cur)
        cur = Block()
      cur.label = m.group(1)
      by_label[cur.label] = len(blocks)
      continue
    t = code_tokens(lines[i])
    if t is None:
      continue
    cur.lines.append(i)
    _u, _d, kind = uses_defs(t)
    if kind in ("branch", "jump", "ret", "ijump", "tail"):
      blocks.append(cur)
      cur = Block()
  if cur.lines or cur.label:
    blocks.append(cur)
  exit_live = {}
  for n, b in enumerate(blocks):
    if not b.lines:
      b.succ = [n + 1] if n + 1 < len(blocks) else []
      continue
    t = code_tokens(lines[b.lines[-1]])
    u, _d, kind = uses_defs(t)
    target = t.split()[-1].split(",")[-1]
    if kind == "branch":
      b.succ = [x for x in (by_label.get(target), n + 1) if x is not None and x < len(blocks)]
    elif kind == "jump":
      b.succ = [by_label[target]] if target in by_label else []
      if target not in by_label:
        exit_live[n] = ALL
    elif kind in ("ret", "tail"):
      b.succ = []
    elif kind == "ijump":
      b.succ, exit_live[n] = [], ALL
    else:
      b.succ = [n + 1] if n + 1 < len(blocks) else []
      if n + 1 >= len(blocks):
        exit_live[n] = ALL
  changed = True
  while changed:
    changed = False
    for n in reversed(range(len(blocks))):
      b = blocks[n]
      out = set(exit_live.get(n, set()))
      for s in b.succ:
        out |= blocks[s].live_in
      live = set(out)
      for i in reversed(b.lines):
        u, d, _k = uses_defs(code_tokens(lines[i]))
        live = (live - d) | u
      if out != b.live_out or live != b.live_in:
        b.live_out, b.live_in, changed = out, live, True
  return blocks, by_label


def condition(t):
  """For branch text t: (compare text template, operand a, operand b, z)
  meaning: predicate p = <cmp> a, b and the fall-through arm runs when
  (p != 0) if z == 0, (p == 0) if z == 1. cmp None means p = a itself."""
  ins = sb.classify(t)
  m, ops = ins.mnem, ins.ops
  if m in ("beqz", "bnez"):
    return None, ops[0], None, 0 if m == "beqz" else 1
  if m in ONE:
    base, x, y = ONE[m]
    m, ops = base, [ops[0] if x == 0 else x, ops[0] if y == 0 else y]
  if m in SWAP:
    m, ops = SWAP[m], [ops[1], ops[0]]
  if m in ("beq", "bne"):
    return "xor", ops[0], ops[1], 0 if m == "beq" else 1
  if m in ("blt", "bge"):
    return "slt", ops[0], ops[1], 1 if m == "blt" else 0
  if m in ("bltu", "bgeu"):
    return "sltu", ops[0], ops[1], 1 if m == "bltu" else 0
  return None


def arm(lines, start):
  """Collect a straight-line arm from line index start: returns (instruction
  line indices, index of the line that ends it: a label or a control op) or
  None if it holds a non-packable op or a directive first."""
  idx, i = [], start
  while i < len(lines):
    if LABEL.match(lines[i]):
      return idx, i
    t = code_tokens(lines[i])
    if t is not None:
      ins = sb.classify(t)
      if ins.is_ctl:
        return idx, i
      if ins.cls == "barrier":
        return None
      idx.append(i)
    elif lines[i].strip().startswith(".") and not lines[i].strip().startswith((".cfi", ".loc")):
      return None
    i += 1
  return None


def label_at(lines, i):
  m = LABEL.match(lines[i]) if i < len(lines) else None
  return m.group(1) if m else None


def next_code(lines, i):
  """Index of the next label or instruction line at or after i."""
  while i < len(lines) and code_tokens(lines[i]) is None and not LABEL.match(lines[i]):
    i += 1
  return i


def normalized(t, q):
  """Instructions setting q = 1 when branch t is NOT taken, else 0."""
  cmp, a, b, z = condition(t)
  if cmp is None:                                   # beqz / bnez a
    return ["\t%s\t%s,%s\n" % ("snez" if z == 0 else "seqz", q, a)]
  if cmp == "xor":
    return ["\txor\t%s,%s,%s\n" % (q, a, b), "\t%s\t%s,%s\n" % ("snez" if z == 0 else "seqz", q, q)]
  out = ["\t%s\t%s,%s,%s\n" % (cmp, q, a, b)]
  if z == 1:
    out.append("\txori\t%s,%s,1\n" % (q, q))
  return out


def match_shape(lines, bi, target, label_refs):
  """Recognise what follows the branch at line bi. Returns a dict with
  kind 'then' / 'else' / 'chain', the arms and the join label, or None."""
  first = arm(lines, bi + 1)
  if first is None:
    return None
  segs, guards, i = [first[0]], [], first[1]
  # if-then-else: T; j Lj; Le(target, referenced once): E; Lj:
  t = code_tokens(lines[i]) if i < len(lines) else None
  if t and t.split()[0] == "j":
    lj, k = t.split()[-1], next_code(lines, i + 1)
    if label_at(lines, k) == target and label_refs.get(target, 0) == 1:
      e = arm(lines, k + 1)
      if e and label_at(lines, e[1]) == lj:
        return {"kind": "else", "then": first[0], "else": e[0], "join": lj,
                "drop": [i, k], "exit": None, "guards": []}
    return None
  # guard chain: further branches to the same target, optionally ending in an
  # exit branch elsewhere, then the target label
  exit_line = None
  while label_at(lines, i) != target:
    t = code_tokens(lines[i]) if i < len(lines) else None
    if t is None:
      return None
    ins = sb.classify(t)
    if ins.cls != "branch" or condition(t) is None:
      return None
    if ins.ops[-1] == target:
      guards.append(i)
      nxt = arm(lines, i + 1)
      if nxt is None:
        return None
      segs.append(nxt[0])
      i = nxt[1]
    elif label_at(lines, next_code(lines, i + 1)) == target:
      exit_line = i
      i = next_code(lines, i + 1)
    else:
      return None
  kind = "then" if not guards and exit_line is None else "chain"
  return {"kind": kind, "segs": segs, "guards": guards, "exit": exit_line,
          "join": target, "then": first[0], "else": [], "drop": []}


def if_convert(lines, pred_regs, stats, cost_fn=None, force=False, reserved=(),
               branch_in_packet=True, counts=None):
  """Return a new line list with profitable short branches if-converted.
  cost_fn(list of instruction lines) -> issue slots for one block; when given,
  a conversion is kept only if it does not cost more issue slots than the
  code it replaces, unless force is set. counts (source line index ->
  execution count, from a scalar profile run) weights each block by how often
  it runs; without it every arm is assumed to run. A predicated branch must
  sit in a packet, so guard chains ending in a branch need
  branch_in_packet."""
  global RESERVED
  RESERVED = {sb.REG[r] for r in reserved}
  pset = [sb.REG[r] for r in pred_regs]
  temps = [r for r in range(5, 32) if r not in (8,)]  # any free register may hold a guard
  label_refs = {}
  for line in lines:
    t = code_tokens(line)
    if t:
      tgt = t.split()[-1].split(",")[-1]
      label_refs[tgt] = label_refs.get(tgt, 0) + 1
  edits = {}
  for lo, hi in functions(lines):
    blocks, by_label = liveness(lines, lo, hi)
    for n, b in enumerate(blocks):
      if not b.lines:
        continue
      bi = b.lines[-1]
      t = code_tokens(lines[bi])
      ins = sb.classify(t)
      if ins.cls != "branch":
        continue
      stats["branches"] = stats.get("branches", 0) + 1
      target = ins.ops[-1]
      shape = match_shape(lines, bi, target, label_refs) if condition(t) else None
      if shape is None:
        stats["no shape"] = stats.get("no shape", 0) + 1
        continue
      if shape["exit"] is not None and not branch_in_packet:
        stats["needs branch in packet"] = stats.get("needs branch in packet", 0) + 1
        continue
      if shape["kind"] == "chain":
        body = [i for seg in shape["segs"] for i in seg] + shape["guards"] + \
               ([shape["exit"]] if shape["exit"] is not None else [])
        limit = 2 * MAX_ARM
      else:
        body = shape["then"] + shape["else"]
        limit = 2 * MAX_ARM
      if not shape["then"] and shape["kind"] != "chain" or len(body) > limit or \
         len(shape["then"]) > MAX_ARM or len(shape["else"]) > MAX_ARM:
        stats["arm too long or empty"] = stats.get("arm too long or empty", 0) + 1
        continue
      body_regs, body_defs = set(), set()
      for i in body:
        u, d, _k = uses_defs(code_tokens(lines[i]))
        body_regs |= u | d
        body_defs |= d
      live = set(blocks[n].live_out)
      if shape["exit"] is not None:
        x = code_tokens(lines[shape["exit"]]).split()[-1].split(",")[-1]
        live |= blocks[by_label[x]].live_in if x in by_label else ALL
      cmp, a, bop, z = condition(t)
      repl = {}
      if shape["kind"] in ("then", "else"):
        if cmp is None and sb.REG[a] in pset and sb.REG[a] not in body_defs:
          p, pre = a, ["\t# if-converted (predicate %s): %s\n" % (a, t)]
        else:
          free = [r for r in pset if r not in live and r not in body_regs]
          if not free:
            stats["no free predicate register"] = stats.get("no free predicate register", 0) + 1
            continue
          p = sb.regname(free[0])
          pre = ["\t%s\t%s,%s%s\t# if-converted: %s\n" % (
              "mv" if cmp is None else cmp, p, a, "" if cmp is None else "," + bop, t)]
        repl[bi] = pre
        for i in shape["then"]:
          repl[i] = ["\t%s\t#@pred %s %d\n" % (code_tokens(lines[i]), p, z)]
        for i in shape["else"]:
          repl[i] = ["\t%s\t#@pred %s %d\n" % (code_tokens(lines[i]), p, 1 - z)]
        for i in shape["drop"]:
          repl[i] = []
      else:
        free = [r for r in pset if r not in live and r not in body_regs]
        tmp = [r for r in temps if r not in live and r not in body_regs and (not free or r != free[0])]
        if not free or (shape["guards"] and not tmp):
          stats["no free predicate register"] = stats.get("no free predicate register", 0) + 1
          continue
        p = sb.regname(free[0])
        q = sb.regname(tmp[0]) if tmp else None
        repl[bi] = normalized(t, p)
        for seg_no, seg in enumerate(shape["segs"]):
          for i in seg:
            repl[i] = ["\t%s\t#@pred %s 0\n" % (code_tokens(lines[i]), p)]
          if seg_no < len(shape["guards"]):
            g = shape["guards"][seg_no]
            repl[g] = normalized(code_tokens(lines[g]), q) + ["\tand\t%s,%s,%s\n" % (p, p, q)]
        if shape["exit"] is not None:
          repl[shape["exit"]] = ["\t%s\t#@pred %s 0\n" % (code_tokens(lines[shape["exit"]]), p)]
      if cost_fn is not None:
        def runs(idx, fallback):
          if counts is None:
            return 1
          return counts.get(idx[0], fallback) if idx else fallback
        b_lines = [lines[i] for i in b.lines[:-1]]
        n_b = runs([bi], 1)
        before = n_b * cost_fn(b_lines + [lines[bi]])
        if shape["kind"] == "chain":
          pieces = shape["guards"] + ([shape["exit"]] if shape["exit"] is not None else [])
          ends = dict(zip(range(len(shape["segs"])), pieces))
          for k, seg in enumerate(shape["segs"]):
            first = seg if seg else ([ends[k]] if k in ends else [])
            before += runs(first, 0) * cost_fn([lines[i] for i in seg] +
                                               ([lines[ends[k]]] if k in ends else []))
        else:
          before += runs(shape["then"], 0) * cost_fn(
              [lines[i] for i in shape["then"]] + ([lines[shape["drop"][0]]] if shape["drop"] else []))
          if shape["else"]:
            before += runs(shape["else"], 0) * cost_fn([lines[i] for i in shape["else"]])
        after_lines = list(b_lines)
        for i in sorted(repl):
          after_lines += [l for l in repl[i] if code_tokens(l)]
        after = n_b * cost_fn(after_lines)
        if after > before and not force:
          stats["not profitable"] = stats.get("not profitable", 0) + 1
          continue
      edits.update(repl)
      kind = {"then": "if-then", "else": "if-then-else", "chain": "guard chain"}[shape["kind"]]
      stats[kind] = stats.get(kind, 0) + 1
  out = []
  for i, line in enumerate(lines):
    out.extend(edits.get(i, [line]))
  return out


def autoinc(lines, stats):
  """Fuse a zero-offset load/store with the following pointer increment."""
  out = list(lines)
  region = []        # instruction line indices of the current straight-line region
  for i in range(len(lines) + 1):
    t = code_tokens(lines[i]) if i < len(lines) else None
    boundary = i == len(lines) or LABEL.match(lines[i]) or \
        (lines[i].strip().startswith(".") and not lines[i].strip().startswith((".cfi", ".loc")))
    if t is None and not boundary:
      continue
    if boundary:
      region = []
      continue
    ins = sb.classify(t)
    if ins.mnem == "addi" and ins.ops[0] == ins.ops[1] and ins.ops[0] not in ("sp", "zero"):
      rp, k = sb.REG[ins.ops[0]], int(ins.ops[2], 0) if re.fullmatch(r"-?\d+", ins.ops[2]) else None
      cand = None
      for j in reversed(region):
        jt = code_tokens(out[j])
        if jt is None:
          continue
        u, d, _ = uses_defs(jt)
        if rp in u or rp in d:
          cand = j
          break
      if cand is not None and k is not None and -2048 <= k <= 2047:
        ci = sb.classify(code_tokens(out[cand]))
        m = sb.MEM_RE.match(ci.ops[1]) if ci.cls in ("load", "store") and len(ci.ops) > 1 else None
        if m and ci.mnem in sb.LOADS | sb.STORES and m.group(1).strip() == "0" \
           and sb.REG.get(m.group(2)) == rp and not (ci.cls == "load" and sb.REG[ci.ops[0]] == rp):
          out[cand] = "\t%s.pi\t%s,%d(%s)\n" % (ci.mnem, ci.ops[0], k, m.group(2))
          out[i] = "\t# autoinc: %s fused into the access above\n" % t
          stats["fused"] = stats.get("fused", 0) + 1
          region = [x for x in region if x != i]
          continue
    if ins.is_ctl or ins.cls == "barrier":
      region = []
      continue
    region.append(i)
  return out
