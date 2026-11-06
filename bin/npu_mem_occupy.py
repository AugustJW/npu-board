#!/usr/bin/env python3
"""NPU 显存守护占用脚本 - 伪装为 VLLMWorker_FK 占用 90% HBM，AICore 持续 >=80%

在远程 NPU 服务器上执行，通过 aclrtMalloc 占用目标设备 90% 的总 HBM。
进程名设置为 VLLMWorker_FK，在 npu-smi info 和 ps 中可见。
同时用 aclnnMatmul 持续烧 4096x4096 FP16 矩阵乘，让 AICore 维持 >=80%。

用法:
    python3 npu_mem_occupy.py <device_id>

示例:
    python3 npu_mem_occupy.py 0    # 占用 NPU 0
    python3 npu_mem_occupy.py 3    # 占用 NPU 3
"""
import atexit
import ctypes
import os
import re
import subprocess
import sys
import time

device_id = sys.argv[1] if len(sys.argv) > 1 else "0"

try:
    import setproctitle
    setproctitle.setproctitle("VLLMWorker_FK")
except ImportError:
    try:
        libc = ctypes.CDLL("libc.so.6")
        PR_SET_NAME = 15
        libc.prctl(PR_SET_NAME, b"VLLMWorker_FK", 0, 0, 0)
    except Exception:
        pass

TARGET_RATIO = 0.90
ACL_MEM_MALLOC_HUGE_FIRST = 0
ACL_ERROR_NONE = 0
ACL_MEM_TYPE_HBM = 1

allocated_ptrs = []
acl_global = None


def cleanup():
    if not allocated_ptrs:
        return
    try:
        acl = acl_global or ctypes.CDLL("libascendcl.so")
        for ptr in allocated_ptrs:
            try:
                acl.aclrtFree(ptr)
            except Exception:
                pass
        acl.aclrtResetDevice(int(device_id))
        acl.aclFinalize()
    except Exception:
        pass


atexit.register(cleanup)


def ts():
    return time.strftime("%H:%M:%S")


def _real_process_on_device(target_dev):
    """检测目标卡上是否出现了非 FK 的真实进程，用于自身让位。

    VLLMWorker_FK 启动于完全空闲的机器（guard 的 is_fully_idle 要求无运行服务），
    故启动时目标卡无任何非 FK 进程。之后 npu-smi 进程段若在该卡上列出非 FK 进程，
    即视为真实进程到来，应主动释放 90% HBM 让位，不等 guard 下一轮。

    返回 True 表示需要让位（发现真实进程）；False 表示无需让位。
    npu-smi 不可用时返回 False（不误让位；真实进程的释放交由 guard 下一轮
    has_real_service 判定，30 分钟级，宁可让位慢也不误退出）。
    """
    try:
        out = subprocess.run(
            ["npu-smi", "info"], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:
        return False  # npu-smi 不可用，退回原行为（等 guard 下一轮），不误杀

    in_proc = False
    for line in out.split("\n"):
        s = line.strip()
        if "Process id" in s or "Process name" in s:
            in_proc = True
            continue
        if not in_proc or not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.split("|")]
        cells = [c for c in cells if c != ""]
        if len(cells) < 4:
            continue
        try:
            npu_id = int(cells[0].split()[0])
        except (ValueError, IndexError):
            npu_id = 0
        proc_name = cells[2].strip()
        if npu_id != int(target_dev):
            continue
        # 排除自身及同类假占用进程
        if "FK" in proc_name or "npu_mem_occupy" in proc_name:
            continue
        return True
    return False


# 自身让位检查周期：每 N 次 _burn_aicore 调用检查一次（每次烧 5s → ~30s 一查）
YIELD_CHECK_EVERY = 6


def main():
    global acl_global
    target = device_id
    t_start = time.time()

    print(f"[{ts()}] === NPU Guard Occupy ===", flush=True)
    print(f"[{ts()}] 目标设备: NPU {target} | 进程名: VLLMWorker_FK | 目标占用: {TARGET_RATIO*100:.0f}%", flush=True)

    ascendcl_paths = [
        "libascendcl.so",
        "/usr/local/Ascend/ascend-toolkit/lib64/libascendcl.so",
        "/usr/local/Ascend/cann/lib64/libascendcl.so",
    ]
    acl = None
    for p in ascendcl_paths:
        try:
            acl = ctypes.CDLL(p)
            print(f"[{ts()}] 加载 libascendcl.so: {p}", flush=True)
            break
        except OSError:
            continue
    if acl is None:
        print(f"[{ts()}] 未找到 libascendcl.so，已搜索: {ascendcl_paths}", flush=True)
        print(f"[{ts()}] 进入降级模式: 仅心跳驻留, HBM 占用靠 driver 残留, AICore 烧机跳过", flush=True)
        heartbeat = 0
        try:
            while True:
                time.sleep(10)
                heartbeat += 1
                if heartbeat % 60 == 0:
                    elapsed = time.time() - t_start
                    print(f"[{ts()}] 心跳(降级): NPU {target} 占用中, 运行 {elapsed:.0f}s ({elapsed/3600:.1f}h)", flush=True)
        except KeyboardInterrupt:
            print(f"[{ts()}] 收到退出信号", flush=True)
        sys.exit(0)
    acl_global = acl

    ret = acl.aclInit(None)
    print(f"[{ts()}] aclInit: ret={ret}", flush=True)
    if ret not in (ACL_ERROR_NONE, 100000):
        print(f"[{ts()}] aclInit 失败 (ret={ret}), 退出", flush=True)
        sys.exit(1)

    ret = acl.aclrtSetDevice(int(target))
    print(f"[{ts()}] aclrtSetDevice({target}): ret={ret}", flush=True)
    if ret != ACL_ERROR_NONE:
        print(f"[{ts()}] 设置设备失败 (ret={ret}), 请确认在正确环境(容器/宿主机)执行", flush=True)
        sys.exit(1)

    free = ctypes.c_size_t()
    total = ctypes.c_size_t()
    acl.aclrtGetMemInfo(ACL_MEM_TYPE_HBM, ctypes.byref(free), ctypes.byref(total))
    total_mb = total.value // (1024 * 1024)
    free_mb = free.value // (1024 * 1024)
    already_used_mb = total_mb - free_mb

    target_mb = int(total_mb * TARGET_RATIO)

    if already_used_mb >= target_mb:
        print(f"[{ts()}] 已使用 {already_used_mb}MB >= 目标 {target_mb}MB, 无需额外占用, 进入心跳+烧机", flush=True)
        heartbeat = 0
        try:
            while True:
                _burn_aicore(acl, int(target), 5)
                heartbeat += 1
                if heartbeat % YIELD_CHECK_EVERY == 0:
                    if _real_process_on_device(target):
                        print(f"[{ts()}] 检测到 NPU {target} 出现真实进程, 自身让位退出", flush=True)
                        break
                if heartbeat % 60 == 0:
                    elapsed = time.time() - t_start
                    print(f"[{ts()}] 心跳(已满): NPU {target} 占用中, 运行 {elapsed:.0f}s ({elapsed/3600:.1f}h)", flush=True)
        except KeyboardInterrupt:
            print(f"[{ts()}] 收到退出信号", flush=True)
        sys.exit(0)

    to_allocate_mb = target_mb - already_used_mb
    print(f"[{ts()}] 总显存: {total_mb}MB | 已用: {already_used_mb}MB | 空闲: {free_mb}MB | 需分配: {to_allocate_mb}MB", flush=True)

    acl.aclrtMalloc.restype = ctypes.c_int
    acl.aclrtMalloc.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_size_t,
        ctypes.c_uint,
    ]

    chunk_mb = 1024
    allocated_mb = 0

    while chunk_mb >= 1 and allocated_mb < to_allocate_mb:
        remaining = to_allocate_mb - allocated_mb
        if chunk_mb > remaining:
            chunk_mb = remaining

        size = chunk_mb * 1024 * 1024
        ptr = ctypes.c_void_p()
        ret = acl.aclrtMalloc(ctypes.byref(ptr), size, ACL_MEM_MALLOC_HUGE_FIRST)
        if ret == ACL_ERROR_NONE:
            allocated_ptrs.append(ptr)
            allocated_mb += chunk_mb
            if chunk_mb >= 512 or allocated_mb % 4096 == 0 or allocated_mb >= to_allocate_mb:
                print(f"[{ts()}] [+] 分配 {chunk_mb}MB OK | 累计 {allocated_mb}MB / {to_allocate_mb}MB", flush=True)
        else:
            chunk_mb = chunk_mb // 2
            if chunk_mb < 1:
                break
            print(f"[{ts()}] [-] {chunk_mb*2}MB 失败(ret={ret}), 缩小到 {chunk_mb}MB", flush=True)

    t_alloc = time.time() - t_start

    free_after = ctypes.c_size_t()
    total_after = ctypes.c_size_t()
    acl.aclrtGetMemInfo(ACL_MEM_TYPE_HBM, ctypes.byref(free_after), ctypes.byref(total_after))
    used_pct = (1 - free_after.value / total_after.value) * 100 if total_after.value > 0 else 0

    print(f"\n[{ts()}] 分配完成 | {len(allocated_ptrs)} 块 | 累计 {allocated_mb}MB | 耗时 {t_alloc:.1f}s", flush=True)
    print(f"[{ts()}] HBM 占用率: {used_pct:.1f}% | 保持占用中...", flush=True)

    heartbeat = 0
    try:
        while True:
            _burn_aicore(acl, int(target), 5)
            heartbeat += 1
            if heartbeat % YIELD_CHECK_EVERY == 0:
                if _real_process_on_device(target):
                    print(f"[{ts()}] 检测到 NPU {target} 出现真实进程, 自身让位退出, 释放 {allocated_mb}MB HBM", flush=True)
                    break
            if heartbeat % 60 == 0:
                elapsed = time.time() - t_start
                print(f"[{ts()}] 心跳: NPU {target} 占用中, 运行 {elapsed:.0f}s ({elapsed/3600:.1f}h)", flush=True)
    except KeyboardInterrupt:
        print(f"[{ts()}] 收到退出信号, 释放显存", flush=True)


ACL_FLOAT16 = 1
ACL_FORMAT_ND = 2
ACL_MEMCPY_HOST_TO_DEVICE = 1

burn_ctx = {}


def _init_aicore_burner(acl, device_id):
    """一次性创建 aclnnMatmul 烧机所需的 tensor、device buffer、workspace。

    用 4096x4096 FP16 矩阵乘 C = A * B 作为重算子，让 AICore 持续高负载。
    aclnnMatmul 比 aclnnAbs 重数百倍（4096^3 = 68亿次 FLOPs/次），
    能让 AICore 稳定 >=80%（实测在 910B3 上可达 90%+）。

    aclnnMatmulGetWorkspaceSize(self, a, b, c, cubeMathType, &workspaceSize, &executor)
    aclnnMatmul(workspace, workspaceSize, executor, stream)

    cubeMathType=0 表示默认 Cube 计算类型（FP16）。
    """
    if burn_ctx:
        return burn_ctx

    opapi_paths = [
        "libopapi.so",
        "/usr/local/Ascend/ascend-toolkit/latest/lib64/libopapi.so",
    ]
    opapi = None
    for p in opapi_paths:
        try:
            opapi = ctypes.CDLL(p)
            break
        except OSError:
            continue
    if opapi is None:
        print(f"[{ts()}] 未找到 libopapi.so，AICore 烧机不可用", flush=True)
        return None

    opapi.aclCreateTensor.restype = ctypes.c_void_p
    opapi.aclCreateTensor.argtypes = [
        ctypes.POINTER(ctypes.c_int64), ctypes.c_uint64, ctypes.c_int,
        ctypes.POINTER(ctypes.c_int64), ctypes.c_uint64, ctypes.c_int,
        ctypes.POINTER(ctypes.c_int64), ctypes.c_uint64, ctypes.c_int64,
    ]
    opapi.aclnnMatmulGetWorkspaceSize.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(ctypes.c_void_p),
    ]
    opapi.aclnnMatmulGetWorkspaceSize.restype = ctypes.c_int
    opapi.aclnnMatmul.argtypes = [
        ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_void_p,
    ]
    opapi.aclnnMatmul.restype = ctypes.c_int

    # CANN 8.0.1 libopapi.so 未导出 aclSetTensorDescAddress/aclSetTensorMemoryMapping，
    # 但有 C++ mangled 符号 _ZNK9aclTensor14SetStorageAddrEPv
    setaddr = getattr(opapi, "_ZNK9aclTensor14SetStorageAddrEPv", None)
    if not setaddr:
        print(f"[{ts()}] 未找到 _ZNK9aclTensor14SetStorageAddrEPv 符号", flush=True)
        return None
    setaddr.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    setaddr.restype = ctypes.c_int

    # 4096x4096 FP16 矩阵乘，单次 68 亿 FLOPs，重算子
    M = 4096
    K = 4096
    N = 4096
    bytes_per_elem = 2  # FP16
    a_bytes = M * K * bytes_per_elem
    b_bytes = K * N * bytes_per_elem
    c_bytes = M * N * bytes_per_elem

    # A: [M, K], B: [K, N], C: [M, N]
    a_dims = (ctypes.c_int64 * 2)(M, K)
    b_dims = (ctypes.c_int64 * 2)(K, N)
    c_dims = (ctypes.c_int64 * 2)(M, N)
    a_strides = (ctypes.c_int64 * 2)(K, 1)
    b_strides = (ctypes.c_int64 * 2)(N, 1)
    c_strides = (ctypes.c_int64 * 2)(N, 1)
    a_storage = (ctypes.c_int64 * 2)(M, K)
    b_storage = (ctypes.c_int64 * 2)(K, N)
    c_storage = (ctypes.c_int64 * 2)(M, N)

    a = opapi.aclCreateTensor(a_dims, 2, ACL_FLOAT16, a_strides, 2, ACL_FORMAT_ND, a_storage, 2, 0)
    b = opapi.aclCreateTensor(b_dims, 2, ACL_FLOAT16, b_strides, 2, ACL_FORMAT_ND, b_storage, 2, 0)
    c = opapi.aclCreateTensor(c_dims, 2, ACL_FLOAT16, c_strides, 2, ACL_FORMAT_ND, c_storage, 2, 0)
    if not (a and b and c):
        print(f"[{ts()}] aclCreateTensor 失败 a={a} b={b} c={c}", flush=True)
        return None

    dev_a = ctypes.c_void_p()
    dev_b = ctypes.c_void_p()
    dev_c = ctypes.c_void_p()
    ret = acl.aclrtMalloc(ctypes.byref(dev_a), a_bytes, ACL_MEM_MALLOC_HUGE_FIRST)
    ret2 = acl.aclrtMalloc(ctypes.byref(dev_b), b_bytes, ACL_MEM_MALLOC_HUGE_FIRST)
    ret3 = acl.aclrtMalloc(ctypes.byref(dev_c), c_bytes, ACL_MEM_MALLOC_HUGE_FIRST)
    if ret != ACL_ERROR_NONE or ret2 != ACL_ERROR_NONE or ret3 != ACL_ERROR_NONE:
        print(f"[{ts()}] 烧机 device buffer 分配失败 ret={ret},{ret2},{ret3}", flush=True)
        return None

    host_a = (ctypes.c_uint8 * a_bytes)()
    host_b = (ctypes.c_uint8 * b_bytes)()
    acl.aclrtMemcpy(dev_a, a_bytes, host_a, a_bytes, ACL_MEMCPY_HOST_TO_DEVICE)
    acl.aclrtMemcpy(dev_b, b_bytes, host_b, b_bytes, ACL_MEMCPY_HOST_TO_DEVICE)

    r1 = setaddr(a, dev_a)
    r2 = setaddr(b, dev_b)
    r3 = setaddr(c, dev_c)
    if r1 != ACL_ERROR_NONE or r2 != ACL_ERROR_NONE or r3 != ACL_ERROR_NONE:
        print(f"[{ts()}] SetStorageAddr 失败 r1={r1} r2={r2} r3={r3}", flush=True)
        return None

    stream = ctypes.c_void_p()
    if hasattr(acl, "aclrtCreateStream"):
        acl.aclrtCreateStream.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        acl.aclrtCreateStream(ctypes.byref(stream))

    workspace_buf = ctypes.c_void_p()
    WORKSPACE_SIZE = 256 * 1024 * 1024
    ret = acl.aclrtMalloc(ctypes.byref(workspace_buf), WORKSPACE_SIZE, ACL_MEM_MALLOC_HUGE_FIRST)
    if ret != ACL_ERROR_NONE:
        print(f"[{ts()}] workspace 分配失败 ret={ret}, 尝试更小", flush=True)
        WORKSPACE_SIZE = 128 * 1024 * 1024
        ret = acl.aclrtMalloc(ctypes.byref(workspace_buf), WORKSPACE_SIZE, ACL_MEM_MALLOC_HUGE_FIRST)
        if ret != ACL_ERROR_NONE:
            print(f"[{ts()}] workspace 第二次分配仍失败 ret={ret}", flush=True)
            return None

    ctx = {
        "opapi": opapi, "a": a, "b": b, "c": c,
        "dev_a": dev_a, "dev_b": dev_b, "dev_c": dev_c,
        "stream": stream,
        "workspace_buf": workspace_buf,
        "workspace_size": WORKSPACE_SIZE,
        "M": M, "K": K, "N": N,
    }
    burn_ctx.update(ctx)
    print(f"[{ts()}] AICore 烧机初始化 OK: Matmul {M}x{K}x{N} FP16, workspace={WORKSPACE_SIZE}B", flush=True)
    return burn_ctx


def _burn_aicore(acl, device_id, duration_sec):
    """烧机 duration_sec 秒，让 AICore 占用率持续 >=80%。

    用 aclnnMatmul 真实矩阵乘算子反复执行（C = A * B），4096x4096 FP16
    单次 68 亿 FLOPs，是 AICore 重负载算子。无 sleep 间隔，持续饱和。
    关键点：executor 是一次性的，每次调用前必须重新 aclnnMatmulGetWorkspaceSize 取新 executor。
    workspace 必须传预分配的 buffer，不能传 None——某些 CANN 版本 ws_size>0 时
    传 None 会 segfault（进程崩溃但 HBM 不释放）。
    """
    ctx = _init_aicore_burner(acl, device_id)
    if not ctx:
        return
    opapi = ctx["opapi"]
    a = ctx["a"]
    b = ctx["b"]
    c = ctx["c"]
    stream = ctx["stream"]
    workspace_buf = ctx["workspace_buf"]
    CUBE_MATH_TYPE_DEFAULT = 0

    end = time.time() + duration_sec
    iters = 0
    try:
        while time.time() < end:
            ws_size = ctypes.c_uint64(0)
            executor = ctypes.c_void_p()
            ret = opapi.aclnnMatmulGetWorkspaceSize(a, b, c, CUBE_MATH_TYPE_DEFAULT,
                                                    ctypes.byref(ws_size), ctypes.byref(executor))
            if ret != ACL_ERROR_NONE:
                if iters == 0:
                    print(f"[{ts()}] aclnnMatmulGetWorkspaceSize 失败 ret={ret}", flush=True)
                break
            if ws_size.value > ctx["workspace_size"]:
                if iters == 0:
                    print(f"[{ts()}] ws_size {ws_size.value} > workspace {ctx['workspace_size']}, 截断", flush=True)
                ws_size = ctypes.c_uint64(ctx["workspace_size"])
            ret = opapi.aclnnMatmul(workspace_buf, ws_size.value, executor, stream)
            if ret != ACL_ERROR_NONE:
                if iters == 0:
                    print(f"[{ts()}] aclnnMatmul 失败 ret={ret}", flush=True)
                break
            iters += 1
            if iters % 1000 == 0:
                if hasattr(acl, "aclrtSynchronizeStream"):
                    acl.aclrtSynchronizeStream(stream)
                print(f"[{ts()}] 烧机 iters={iters}", flush=True)
    except Exception as e:
        print(f"[{ts()}] 烧机异常: {e}", flush=True)


if __name__ == "__main__":
    main()
