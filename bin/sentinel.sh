#!/bin/bash
# Continuous sentinel v3: every INTERVAL seconds sweep all 16 monitored nodes:
#   - detect our mockedup being externally removed / nodes needing release
#     FIRST (before any occupy, to close the fight window)
#   - occupy nodes idle for CONSECUTIVE consecutive sweeps (10-min window each;
#     guards against service-swap gaps — 2026-10-30 incident on 14.31 where we
#     occupied in the gap between a colleague's old service dying 00:10 and
#     their new container starting 00:21)
#   - verify the hold actually took (8 cards ~90% HBM within VERIFY_WAIT);
#     release + blacklist 6h on failure or external removal (conflict, not war)
# Run on launch host:  nohup bash sentinel.sh 600 >/dev/null 2>&1 &
set -u
MON="${NPU_MON:-$(dirname "$0")/..}"
LOG=$MON/sentinel.log
KEY="${NPU_KEY:-$HOME/.ssh/id_rsa}"
INTERVAL=${1:-600}
CONSECUTIVE=${2:-2}
VERIFY_WAIT=${3:-90}
BLACKLIST_SECS=$((6*3600))
SSHO="-i $KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o BatchMode=yes"
touch $MON/state_idle_count $MON/state_occupied $MON/blacklist

destate() {  # destate <file> <ip>
  grep -v "^$2 " "$1" > "$1.tmp" 2>/dev/null || true
  mv "$1.tmp" "$1" 2>/dev/null || true
}
blacklist_add() {  # blacklist_add <ip> <reason>
  destate $MON/state_occupied "$1"
  destate $MON/blacklist "$1"
  echo "$1 $(( $(date +%s) + BLACKLIST_SECS ))" >> $MON/blacklist
  echo "$(date '+%F %T') [sentinel] $1 blacklisted ${BLACKLIST_SECS}s ($2)" >> $LOG
}

echo "$(date '+%F %T') === sentinel v3 start pid=$$ interval=${INTERVAL}s consecutive=$CONSECUTIVE ===" >> $LOG
while true; do
  TS=$(bash $MON/sweep.sh | grep -oE 'ts=[0-9]+' | cut -d= -f2)
  [ -z "$TS" ] && { echo "$(date '+%F %T') [sentinel] sweep failed"; sleep $INTERVAL; continue; }
  SUM=$(python3 $MON/summarize.py $MON/snapshots $TS)
  NOW=$(date '+%F %T')
  echo "$SUM" | grep '^COUNTS' | sed "s/^/$NOW /" >> $LOG

  # --- pass 1: reconcile our occupied nodes (BEFORE occupying anything) ---
  while read -r ip ots; do
    [ -z "${ip:-}" ] && continue
    probe=$(ssh $SSHO root@$ip 'docker ps -a --format "{{.Names}}"; echo GATE_OK' 2>/dev/null)
    case "$probe" in
      *GATE_OK*) ;;
      *) echo "$(date '+%F %T') [sentinel] $ip unreachable, skip reconcile" >> $LOG; continue ;;
    esac
    exists=$(echo "$probe" | grep -x mockedup)
    verdict=$(echo "$SUM" | awk -v ip=$ip '$1==ip{print $3}')
    if [ "$verdict" = "BUSY" ]; then
      if [ -n "$exists" ]; then
        echo "$(date '+%F %T') [sentinel] $ip BUSY but mockedup still present -> release" >> $LOG
        bash $MON/release.sh $ip >> $LOG 2>&1
      fi
      destate $MON/state_occupied "$ip"   # normal yield, no blacklist
    elif [ -z "$exists" ]; then
      echo "$(date '+%F %T') [sentinel] $ip mockedup externally removed" >> $LOG
      blacklist_add "$ip" "external-removal"
    fi
  done < $MON/state_occupied

  # --- pass 2: idle streaks -> occupy ---
  for ip in $(echo "$SUM" | awk '$1 ~ /^7\./{print $1}'); do
    verdict=$(echo "$SUM" | awk -v ip=$ip '$1==ip{print $3}')
    if [ "$verdict" != "IDLE" ]; then
      destate $MON/state_idle_count "$ip"
      continue
    fi
    n=$(awk -v ip=$ip '$1==ip{print $2+0}' $MON/state_idle_count 2>/dev/null); n=${n:-0}
    n=$((n+1))
    { destate $MON/state_idle_count "$ip"; echo "$ip $n" >> $MON/state_idle_count; }
    [ "$n" -lt "$CONSECUTIVE" ] && continue
    echo "$(date '+%F %T') [sentinel] IDLE x$n -> occupy $ip" >> $LOG
    bash $MON/occupy.sh $ip >> $LOG 2>&1
    # verify the hold took: all 8 cards >5GB HBM within VERIFY_WAIT
    sleep $VERIFY_WAIT
    held=$(ssh $SSHO root@$ip 'npu-smi info 2>/dev/null' 2>/dev/null | awk '
      /0000:/ {
        line = $0
        gsub(/ *\//, "/", line); gsub(/\/ */, "/", line)
        n = 0
        while (match(line, /[0-9]+\/[0-9]+/)) {
          pair = substr(line, RSTART, RLENGTH); split(pair, t, "/")
          n++; used[n] = t[1]+0; total[n] = t[2]+0
          line = substr(line, RSTART + RLENGTH)
        }
        if (n > 0 && total[n] > 0 && used[n] > 5000) c++
      }
      END { print c + 0 }')
    echo "$(date '+%F %T') [sentinel] $ip hold-verify: $held/8 cards held" >> $LOG
    if [ "$held" != "8" ]; then
      bash $MON/release.sh $ip >> $LOG 2>&1
      blacklist_add "$ip" "hold-failed-$held/8"
    fi
  done

  # --- log anomalies ---
  echo "$SUM" | awk '$1 ~ /^7\./ && ($3=="HOST-OCCUPIER" || $3=="PARTIAL" || $3=="UNKNOWN" || $3=="UNREACHABLE" || $3=="PARSE_FAIL"){print $1, $3}' \
    | sed "s/^/$(date '+%F %T') [sentinel] anomaly /" >> $LOG

  sleep $INTERVAL
done
