source /usr/local/Ascend/ascend-toolkit/set_env.sh 2>/dev/null || true
export LD_LIBRARY_PATH=/usr/local/Ascend/driver/lib64/common:/usr/local/Ascend/driver/lib64/driver:$LD_LIBRARY_PATH
echo "[runner] preparing holder script $(date '+%F %T')"
sed -e 's/VLLMWorker_FK/NPUHolder_WC/g' /root/mockup/npu_mem_occupy.py > /root/mockup/holder.py
chmod +x /root/mockup/holder.py
echo "[runner] starting 8 holder processes $(date '+%F %T')"
for i in 0 1 2 3 4 5 6 7; do
  python3 /root/mockup/holder.py $i > /root/mockup/occupy_$i.log 2>&1 &
done
wait
