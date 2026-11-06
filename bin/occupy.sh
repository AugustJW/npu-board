#!/bin/bash
# Start the mockedup NPU-occupier container on ONE node.
# Safety-gated: only runs if the node is verifiably fully idle RIGHT NOW
# (no vllm/ray procs, all 8 cards at baseline HBM, no mockedup already Up).
# Container policy: privileged + driver mounts, restart=NO (the occupier
# yields by exiting; the sentinel decides whether to re-occupy later).
# Env overrides for testing:
#   MOCKUP_ALLOWED="<ips>"  allow-list override
#   MOCKUP_OCCUPY=no        start container with `sleep infinity` (no occupy)
#   MOCKUP_FORCE=yes        skip idle checks (TEST ONLY — combine with
#                           MOCKUP_CARDS to limit which cards are touched)
#   MOCKUP_CARDS="5"        occupy only these card ids
#   MOCKUP_NAME=mockup_test container name override
# Usage: bash occupy.sh <ip>
set -u
KEY="${NPU_KEY:-$HOME/.ssh/id_rsa}"
MON="${NPU_MON:-$(dirname "$0")/..}"
DEFAULT_ALLOWED="${NPU_IPS:-10.0.0.1 10.0.0.2}"  # set NPU_IPS to your node list
ALLOWED="${MOCKUP_ALLOWED:-$DEFAULT_ALLOWED}"
ip=${1:-}
[ -z "$ip" ] && { echo "[occupy] usage: bash occupy.sh <ip>"; exit 1; }
case " $ALLOWED " in *" $ip "*) ;; *) echo "[occupy] $ip not in allowed list, refuse"; exit 1;; esac
SSHO="-i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o BatchMode=yes"

# --- push gate script (harmless file) and run it ---
ssh $SSHO root@$ip 'mkdir -p /root/mockup' 2>/dev/null || { echo "[occupy] $ip unreachable, refuse"; exit 1; }
scp $SSHO -q $MON/gate.sh root@$ip:/root/mockup/ 2>/dev/null || { echo "[occupy] $ip scp gate fail"; exit 1; }
res=$(ssh $SSHO root@$ip 'bash /root/mockup/gate.sh' 2>/dev/null)
echo "[occupy] $ip gate: $res"
procs=$(echo "$res" | sed -n 's/.*procs=\([0-9]*\).*/\1/p')
cards=$(echo "$res" | sed -n 's/.*cards=\([0-9]*\).*/\1/p')
busy=$(echo  "$res" | sed -n 's/.*busy=\([0-9]*\).*/\1/p')
others=$(echo "$res" | sed -n 's/.*others=\([0-9]*\).*/\1/p')
mock=$(echo  "$res" | sed -n 's/.*mock=\([a-z]*\)/\1/p')
[ -z "$procs" ] && { echo "[occupy] $ip gate failed/ssh fail, refuse"; exit 1; }
if [ "$mock" != "none" ]; then
  [ "${MOCKUP_NAME:-mockedup}" = "mockedup" ] && { echo "[occupy] $ip mockedup already Up, skip"; exit 0; }
  [ "$mock" = "${MOCKUP_NAME:-mockedup}" ] && { echo "[occupy] $ip $mock already Up, skip"; exit 0; }
fi
# blacklist: a node where our mockedup was externally killed stays off-limits
if [ "${MOCKUP_FORCE:-no}" != "yes" ] && [ -f "$MON/blacklist" ]; then
  if awk -v ip="$ip" -v now="$(date +%s)" '$1==ip && $2>now{f=1} END{exit !f}' "$MON/blacklist" 2>/dev/null; then
    echo "[occupy] $ip blacklisted (recent conflict), refuse"; exit 1
  fi
fi
if [ "${MOCKUP_FORCE:-no}" != "yes" ]; then
  [ "$procs" != "0" ] && { echo "[occupy] $ip has $procs vllm/ray procs, refuse"; exit 1; }
  [ "$cards" -lt 8 ] 2>/dev/null && { echo "[occupy] $ip npu-smi parse fail ($cards cards), refuse"; exit 1; }
  [ "$busy" != "0" ] && { echo "[occupy] $ip has $busy busy cards, refuse"; exit 1; }
  # Other running containers are NOT a hard refusal — an Up container that
  # holds no NPU memory (engine exited / controller-only) leaves the cards
  # genuinely free (user request 2026-11-06: 15.41 had 5 idle containers but
  # all cards free). The procs + HBM checks above are the real protection:
  # a loading engine shows vllm/ray procs and rising HBM and is still caught.
  [ "$others" != "0" ] && echo "[occupy] $ip note: $others other containers Up (cards verified free, proceeding)"
else
  echo "[occupy] $ip MOCKUP_FORCE=yes — skipping idle/blacklist checks"
fi

# --- push occupier + runner scripts ---
scp $SSHO -q $MON/npu_mem_occupy.py $MON/runner.sh root@$ip:/root/mockup/ || { echo "[occupy] $ip scp fail"; exit 1; }
ssh $SSHO root@$ip 'chmod +x /root/mockup/runner.sh /root/mockup/npu_mem_occupy.py' 2>/dev/null

# --- pick image: preferred tag first, then any local omniinfer-a2-arm ---
IMG=$(ssh $SSHO root@$ip "docker images --format '{{.Repository}}:{{.Tag}}' | grep -x 'registry-cbu.huawei.com/omniai_omniinfer_dev/omniinfer-a2-arm:release_0.7.5-202609161419-vllm'")
[ -z "$IMG" ] && IMG=$(ssh $SSHO root@$ip "docker images --format '{{.Repository}}:{{.Tag}}' | grep -m1 'omniinfer-a2-arm'")
[ -z "$IMG" ] && { echo "[occupy] $ip no omniinfer-a2-arm image locally, refuse"; exit 1; }
echo "[occupy] $ip image: $IMG"

# --- launch (restart policy intentionally default=no) ---
# record who asked for this hold (optional, shown on the web board)
if [ -n "${MOCKUP_HOLDER:-}" ]; then
  ssh $SSHO root@$ip "printf '%s' '${MOCKUP_HOLDER}' > /root/mockup/HOLDER" 2>/dev/null || true
fi
if [ "${MOCKUP_OCCUPY:-yes}" = "no" ]; then
  CMD="sleep infinity"
  echo "[occupy] $ip SMOKE MODE: sleep only, no occupy"
else
  CMD="/root/mockup/runner.sh"
fi
ssh $SSHO root@$ip "docker rm -f ${MOCKUP_NAME:-mockedup} >/dev/null 2>&1; docker run -d --name ${MOCKUP_NAME:-mockedup} \
  --entrypoint bash \
  --privileged \
  --device /dev/davinci_manager --device /dev/hisi_hdc --device /dev/devmm_svm \
  -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
  -v /usr/local/dcmi:/usr/local/dcmi \
  -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
  -v /usr/bin/hccn_tool:/usr/bin/hccn_tool \
  -v /usr/local/sbin:/usr/local/sbin \
  -v /root/mockup:/root/mockup \
  -e OCCUPY_CARDS='${MOCKUP_CARDS:-}' \
  '$IMG' $CMD" || { echo "[occupy] $ip docker run fail"; exit 1; }
echo "[occupy] $ip ${MOCKUP_NAME:-mockedup} started $(date '+%F %T')"
# track default-name occupies for the sentinel's external-kill detection
if [ "${MOCKUP_NAME:-mockedup}" = "mockedup" ] && [ "${MOCKUP_OCCUPY:-yes}" != "no" ]; then
  touch $MON/state_occupied
  grep -v "^$ip " $MON/state_occupied > $MON/state_occupied.tmp 2>/dev/null || true
  echo "$ip $(date +%s)" >> $MON/state_occupied.tmp
  mv $MON/state_occupied.tmp $MON/state_occupied 2>/dev/null || true
fi
