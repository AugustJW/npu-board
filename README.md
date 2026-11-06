# npu-board — Ascend NPU 监控 & 占卡看板

16 台华为 Ascend A2 (910B) 机器的 NPU 监控 + 真实占卡工具，带 Web 看板。
在某集群上连续运行 8+ 天（2026-10-29 起），稳定占卡、自动巡检。

## 功能

- **监控**：并行 SSH 巡检 16 台机器，每卡 HBM 占用 / AICore / 占卡进程 / 容器列表
- **占卡（mockup 容器）**：在空闲机器上起 `mockedup` docker 容器，容器内
  `aclrtMalloc` 占每卡 90% HBM + `aclnnMatmul` 持续烧 AICore ≥80%
  —— **占卡必须走容器，不允许 host 直接跑占卡进程**
- **安全门禁**：有 vllm/ray 进程或卡被占（HBM >5GB）的机器拒绝占卡；
  其他 Up 容器（引擎已退、仅空挂）不拦截
- **释放 = 停全部容器**：`docker stop` 该机所有运行中容器（同事的保留、
  可 `docker start` 恢复），我们的 `mockedup` 直接删除，显存立即归还
- **Web 看板**：单文件 HTML + Python 标准库后端，无任何前端依赖
  - 统计卡（空闲 NPU / mockup占用 / 两组空卡 / 整机空闲）
  - 每机 8 卡 chip（绿=空闲 金=mockup占用 灰=他人占用），悬停看显存/进程
  - **占用人登记**：任何人可在"占用人"列 +登记 写名字（纯登记不占硬件，
    名字写在被占机器 `/root/mockup/HOLDER` 上，重启看板不丢）
  - 操作：`mockup 占卡`（空闲机）/ `停止全部容器`（占用机）
  - 60 秒自动刷新

### 使用准则（看板顶部横幅同款）

1. **要用的时候**：登记自己的名字 + 停止全部容器
2. **释放的时候**：点 mockup 占卡，把卡占住留给下一个人

## 目录结构

```
bin/
  sweep.sh           # 并行巡检 16 台，快照到 snapshots/（IP 清单在此改）
  summarize.py       # 快照汇总：IDLE/BUSY/PARTIAL/MOCKUP/... 判定
  gate.sh            # 占卡安全门禁（推到节点上执行，npu-smi 健壮解析）
  occupy.sh          # 起 mockedup 容器（门禁+黑名单+镜像选择，见环境变量）
  release.sh         # 停止该机全部容器（同事的 stop 保留，我们的 rm）
  runner.sh          # 容器内入口：改名占卡进程为 NPUHolder_WC 防.guard
  npu_mem_occupy.py  # 占卡核心：每卡一进程，90% HBM + AICore 烧机，真实
                     #   进程出现自动让位退出（来自集群现网脚本）
  sentinel.sh        # 自动巡检+占卡哨兵（可选；当前为手动模式停用）
web/
  server.py          # 看板后端：/api/status /api/occupy /api/release
                     #   /api/register /api/unregister（纯标准库）
  board.html         # 看板前端（单文件，无依赖）
```

## 部署

要求：一台能 SSH 免密（root + key）到所有 NPU 节点的跳板机，节点上有
带 CANN 的 docker 镜像（如 `omniinfer-a2-arm:*`）。

```bash
# 1. 跳板机上
git clone <this-repo> npu_monitor && cd npu_monitor

# 2. 配置机器清单（全部支持环境变量，见 .env.example）：
#    export NPU_IPS="<16台IP>"  NPU_GROUP_A="<组A>"  NPU_GROUP_B="<组B>"
#    export NPU_KEY="~/.ssh/id_rsa"  # 跳板机->节点的 ssh key

# 3. SSH key：跳板机 -> 所有节点的 root 免密

# 4. 起看板
cd web && nohup python3 server.py > server.log 2>&1 &
# 打开 http://<跳板机IP>:8766/
```

CLI 用法：

```bash
bash bin/sweep.sh                          # 巡检
python3 bin/summarize.py                   # 汇总
bash bin/occupy.sh <ip>                    # 占卡（走门禁）
MOCKUP_HOLDER=张三 bash bin/occupy.sh <ip> # 占卡并署名
bash bin/release.sh <ip>                   # 停该机全部容器
nohup bash bin/sentinel.sh 600 &           # 自动哨兵（可选）
```

## 踩坑记录（都在代码里有对应处理）

- **npu-smi 列宽溢出**：忙卡渲染成 `59771/ 65536`（斜杠前无空格），
  解析前必须归一化 `/` 两侧空格
- **反占卡 guard**：部分节点有每分钟 cron `/tmp/kill_guard.sh`，会 pkill
  `VLLMWorker_FK`/`npu_mem_occupy` **连容器内的也杀** → runner.sh 里
  `sed` 改名占卡进程为 `NPUHolder_WC`
- **服务切换空窗**：旧服务刚死、新容器未起时机器"看起来空闲"——占卡
  会打架（14.31 事故）。门禁核心看 vllm/ray 进程 + 卡 HBM；哨兵模式
  另有连续 2 轮空闲 + 占后验证 + 外部删除拉黑 6h
- **容器 restart=NO**：占卡进程检测到真实进程会自动让位退出，容器若
  `--restart always` 会让位后立刻重占、OOM 真实业务
- **ssh 会话挂死**：远端 `nohup ... &` 后 ssh 不退出 → 用 `ssh -f` 或
  让 Bash 调用超时；`pkill -f xxx` 会匹配到 ssh 自身命令行 → 用
  `pkill -f 'xxx[.]yyy'` 防自匹配

## License

MIT
