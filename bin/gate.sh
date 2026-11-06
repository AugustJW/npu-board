#!/bin/bash
# Safety gate v2 — runs ON the target node (pushed by occupy.sh).
# Layout-independent npu-smi parsing: a chip row is any line containing a
# PCI bus-id ("0000:"); its HBM usage is the LAST "used/total" pair on that
# line. Spaces around the slash are normalized first because busy cards
# render as "59771/ 65536" (column overflow, no space before the slash).
# v2: also counts OTHER running containers — a node with any foreign
# Up container is someone's active machine, never occupy it (2026-10-30
# incident: occupied 14.31 in the gap between a colleague's old service
# dying and their new container starting).
# Prints: procs=<n> cards=<n> busy=<n> others=<n> mock=<none|mockedup>
procs=$(ps -eo cmd | grep -iE "vllm serve|ray::|EngineCore" | grep -v grep | wc -l)
read cards busy <<< "$(npu-smi info 2>/dev/null | awk '
  /0000:/ {
    line = $0
    gsub(/ *\//, "/", line)
    gsub(/\/ */, "/", line)
    n = 0
    while (match(line, /[0-9]+\/[0-9]+/)) {
      pair = substr(line, RSTART, RLENGTH)
      split(pair, t, "/")
      n++
      used[n] = t[1] + 0
      total[n] = t[2] + 0
      line = substr(line, RSTART + RLENGTH)
    }
    if (n > 0 && total[n] > 0) {
      cards++
      if (used[n] > 5000) busy++
    }
  }
  END { print cards + 0, busy + 0 }
')"
others=$(docker ps --format "{{.Names}}" 2>/dev/null | grep -vx mockedup | wc -l)
mock=$(docker ps --format "{{.Names}}" 2>/dev/null | grep -x mockedup)
echo "procs=$procs cards=$cards busy=$busy others=$others mock=${mock:-none}"
