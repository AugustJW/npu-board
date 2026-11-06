#!/bin/bash
# STOP ALL containers on a node (docker stop, not rm — containers are kept so
# their owners can `docker start` them again; mockedup is fully removed since
# it is ours) and drop it from the sentinel's occupied-state tracking.
# Frees all NPU HBM/AICore held by any container on that machine.
# Usage: bash release.sh <ip>
set -u
KEY="${NPU_KEY:-$HOME/.ssh/id_rsa}"
MON="${NPU_MON:-$(dirname "$0")/..}"
ip=${1:-}
[ -z "$ip" ] && { echo "[release] usage: bash release.sh <ip>"; exit 1; }
ssh -i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o BatchMode=yes \
    root@$ip '
      n_before=$(docker ps -q 2>/dev/null | wc -l)
      docker rm -f mockedup >/dev/null 2>&1
      docker stop $(docker ps -q) >/dev/null 2>&1
      rm -f /root/mockup/HOLDER
      n_after=$(docker ps -q 2>/dev/null | wc -l)
      echo "[release] '"$ip"' containers stopped: $n_before -> $n_after running, at $(date "+%F %T")"
    '
grep -v "^$ip " $MON/state_occupied > $MON/state_occupied.tmp 2>/dev/null || true
mv $MON/state_occupied.tmp $MON/state_occupied 2>/dev/null || true
