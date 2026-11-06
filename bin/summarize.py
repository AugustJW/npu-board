#!/usr/bin/env python3
"""Summarize the latest sweep snapshots on the launch host.
Usage: python3 summarize.py [snapshot_dir] [ts]   (ts optional -> latest)
Verdicts: IDLE / MOCKUP / BUSY / PARTIAL / HOST-OCCUPIER / PARSE_FAIL / UNREACHABLE
"""
import glob, os, re, sys
from collections import Counter

OUT = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("NPU_MON", ".") + "/snapshots"
ts = sys.argv[2] if len(sys.argv) > 2 else None
if not ts:
    files = sorted(glob.glob(os.path.join(OUT, "*.txt")))
    if not files:
        print("NO SNAPSHOTS"); sys.exit(1)
    ts = os.path.basename(files[-1]).rsplit("_", 1)[1].split(".")[0]

CHIP_RE = re.compile(r'^(\S+)\s+(\S+)\s*/\s*(\S+)\s+(\S+)\s*/\s*(\S+)$')

def parse(path):
    d = {"host": "?", "docker": [], "procs": [], "fleet": [], "top": [],
         "images": [], "cards": [], "reachable": False}
    sec = None
    for line in open(path, errors="replace"):
        line = line.rstrip("\n")
        if line.startswith("HOST="):
            d["reachable"] = True; d["host"] = line[5:]; continue
        if line == "---DOCKER---": sec = "docker"; continue
        if line == "---PROCS---": sec = "procs"; continue
        if line == "---FLEET---": sec = "fleet"; continue
        if line == "---TOP---": sec = "top"; continue
        if line == "---IMAGES---": sec = "images"; continue
        if line == "---NPU---": sec = "npu"; continue
        if line.startswith("---"): sec = None; continue
        if sec == "docker":
            if "|" in line:
                parts = line.split("|")
                d["docker"].append((parts[0], parts[1] if len(parts) > 1 else "",
                                   parts[2] if len(parts) > 2 else ""))
        elif sec == "procs":
            if line.strip(): d["procs"].append(line.strip()[:150])
        elif sec == "fleet":
            if line.strip(): d["fleet"].append(line.strip()[:150])
        elif sec == "top":
            if line.strip(): d["top"].append(line.strip()[:150])
        elif sec == "images":
            if line.strip(): d["images"].append(line.strip()[:150])
        elif sec == "npu":
            if line.startswith("|"):
                cells = [c.strip() for c in line.split("|")[1:-1]]
                if len(cells) >= 3:
                    m = CHIP_RE.match(cells[2])
                    if m:
                        try:
                            d["cards"].append((float(m.group(1)),
                                               int(float(m.group(4))),
                                               int(float(m.group(5)))))
                        except ValueError:
                            pass
    return d

def verdict(d):
    if not d["reachable"]:
        return "UNREACHABLE", "ssh failed"
    procs_real = [p for p in d["procs"] if re.search(r"vllm|ray::|EngineCore", p, re.I)]
    occupier = [p for p in d["procs"] if re.search(r"npu_mem_occupy|VLLMWorker_FK", p, re.I)]
    run_ct = [(n, i, s) for (n, i, s) in d["docker"] if s.startswith("Up")]
    mock_ct = [(n, i, s) for (n, i, s) in run_ct
               if re.search(r"mock", n, re.I) or re.search(r"mock", i, re.I)]
    n = len(d["cards"])
    nbusy = sum(1 for (a, h, t) in d["cards"] if h > 5000 or a > 5)
    if procs_real:
        return "BUSY", "vllm/ray procs: %d" % len(procs_real)
    # occupier procs are visible in host ps even when inside the mockedup
    # container — container presence wins, else it's a forbidden host occupier
    if mock_ct:
        return "MOCKUP", "%s Up, %d/%d cards held" % (mock_ct[0][0], nbusy, n)
    if occupier:
        return "HOST-OCCUPIER", "host-level occupier (forbidden pattern)"
    if n == 0:
        return "PARSE_FAIL", "no cards parsed - read raw"
    if nbusy == 0:
        return "IDLE", "all %d cards idle" % n
    if nbusy < n:
        return "PARTIAL", "%d/%d cards busy, no vllm/mock proc" % (nbusy, n)
    return "UNKNOWN", "%d/%d cards busy, no proc/container" % (nbusy, n)

files = sorted(glob.glob(os.path.join(OUT, "*_%s.txt" % ts)))
rows = []
for f in files:
    ip = os.path.basename(f).split("_")[0]
    d = parse(f)
    v, why = verdict(d)
    rows.append((ip, d, v, why))

print("=== NPU monitor summary ts=%s (%d machines) ===" % (ts, len(rows)))
print("%-16s %-4s %-14s %-38s %s" % ("IP", "CARD", "VERDICT", "WHY", "HBM-GB per card"))
for ip, d, v, why in rows:
    hbms = ",".join("%.1f" % (h / 1024.0) for (a, h, t) in d["cards"]) or "-"
    print("%-16s %-4s %-14s %-38s %s" % (ip, len(d["cards"]), v, why, hbms))
print()
for ip, d, v, why in rows:
    if d["docker"]:
        print("[%s] docker: %s" % (ip, "; ".join("%s(%s)" % (n, s.split()[0]) for (n, i, s) in d["docker"][:6])))
    if d["procs"]:
        print("[%s] procs: %s" % (ip, " | ".join(p[:110] for p in d["procs"][:3])))
    if d["fleet"]:
        print("[%s] fleet: %s" % (ip, d["fleet"][0][:110]))
print()
print("COUNTS:", dict(Counter(r[2] for r in rows)))
print("IDLE machines (candidates for mockup):",
      [r[0] for r in rows if r[2] == "IDLE"])
