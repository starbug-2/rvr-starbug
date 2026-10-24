#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Turn a scalar build's execution counts into per-source-line counts.

The scalar build (starbug_bundle.py --rules scalar) labels every basic block
Lsb_<file>_<n> and records, in <file>.sched.json, which source line each of
its instructions came from. With the simulator's --pc_histogram and the
block label addresses (nm), each block's execution count is known, and every
source line in it gets that count. Writes profile_<file>.json per source file.

usage: make_profile.py SCALAR_DIR
       (expects pc_histogram.txt, block_labels.txt and *.sched.json there)
"""

import glob
import json
import os
import sys

d = sys.argv[1]
hist = {}
for line in open(os.path.join(d, "pc_histogram.txt")):
  a, c = line.split()
  hist[int(a, 16)] = int(c)
addr = {}
for line in open(os.path.join(d, "block_labels.txt")):
  a, _t, name = line.split()
  addr[name] = int(a, 16)
for f in sorted(glob.glob(os.path.join(d, "*.sched.json"))):
  data = json.load(open(f))
  counts = {}
  for blk in data["blocks"]:
    c = hist.get(addr.get(blk["label"], -1), 0)
    for ln in blk.get("lines", []):
      if ln is not None:
        counts[ln] = c
  json.dump(counts, open(os.path.join(d, "profile_%d.json" % data["file_id"]), "w"))
