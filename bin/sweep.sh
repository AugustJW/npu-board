#!/bin/bash
# Sweep NPU/docker state of the 16 monitored A2 machines. Run on the bastion host.
# Usage: bash sweep.sh [outdir]   ->  snapshots at <outdir>/<ip>_<ts>.txt, prints ts on last line
KEY="${NPU_KEY:-$HOME/.ssh/id_rsa}"
OUT="${1:-$MON/snapshots}"
SSH_OPTS="-i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8 -o BatchMode=yes"
IPS="${NPU_IPS:-10.0.0.1 10.0.0.2}"  # set NPU_IPS to your node list
mkdir -p "$OUT"
TS=$(date '+%Y%m%d%H%M%S')
for ip in $IPS; do
  (
    ssh $SSH_OPTS root@$ip '
      echo "HOST=$(hostname)"
      echo "---DOCKER---"
      docker ps -a --format "{{.Names}}|{{.Image}}|{{.Status}}" 2>/dev/null
      echo "---PROCS---"
      ps -eo pid,cmd | grep -iE "vllm serve|ray::|EngineCore|npu_mem_occupy|VLLMWorker_FK" | grep -v grep
      echo "---FLEET---"
      ps -eo pid,cmd | grep -iE "npu_manager|npu-fleet" | grep -v grep
      echo "---TOP---"
      ps -eo pid,pcpu,cmd --sort=-pcpu | head -8
      echo "---IMAGES---"
      docker images --format "{{.Repository}}:{{.Tag}}|{{.ID}}" 2>/dev/null | head -12
      echo "---NPU---"
      npu-smi info 2>/dev/null
    ' > "$OUT/${ip}_${TS}.txt" 2>&1
  ) &
done
wait
# prune snapshots older than 2 days
find "$OUT" -name '*.txt' -mtime +2 -delete 2>/dev/null
echo "SWEEP_DONE ts=$TS dir=$OUT"
