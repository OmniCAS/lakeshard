# 节点要求

多机构建对沙箱节点的要求，以及怎样核对。检查脚本：`scripts/node_check.sh`（每台节点）和 `scripts/cluster_check.sh`（集群）。

## 要求

| 项 | 要求 | 为什么 |
| --- | --- | --- |
| 核数 | 按构建规模定。中等仓库每个 worker 16–32 核即可；超大仓库总共 250–400 核才有明显收益（见 `docs/design.zh.md`） | 关键路径之外的工作才能被多机分摊；Lake 在单机上线程过多会争锁 |
| 内存 | 每个 worker 约 128 GB；放顶端模块（导入最多的那几层）的 worker 约 256 GB | 越靠近顶端的模块，加载的依赖越多，单个 `lean` 进程的内存越大 |
| 本地盘 | 每个 worker 约 300 GB 可用于构建产物，而且在容器运行时数据和 kubelet 的驱逐阈值之外另算 | 构建目录有大量小文件，必须在本地盘上写 |
| 共享挂载 | 所有 worker 挂同一个共享目录 `<shared>`，路径一致 | 交换打包好的产物、分发镜像归档、放只读缓存；不用来直接构建 |
| `vm.max_map_count` | ≥ 1048576 | 大构建的进程有大量内存映射；这个参数只能在宿主上改，不能按容器设 |
| 同一绝对路径 | 每个 worker 上的构建目录在容器内是同一个绝对路径（例如 `/work`） | Lake 和 `lean` 的输出会带路径；路径不同就不能逐字节比对产物 |
| 实例时长 | 单个沙箱的最长存活时间 ≥ 一次分片任务的耗时（加上余量），并允许续期 | 构建做到一半被回收只能重来 |
| 进程/线程数 | 每个沙箱允许的 PID 数（线程也算）≥ 构建并发数 × 每个 `lean` 进程的线程数 | 默认的小上限（例如 128）会让并行构建在启动阶段就失败；可以用 `LEAN_NUM_THREADS` 限制每个进程的线程数 |
| 镜像 | 构建镜像在每台 worker 上预载 | 运行时现拉大镜像会拖慢启动，网络不好时还会失败 |

## 示例环境对照

三台 worker 物理上都是 128 线程、503 GB 内存，但和其他用户共用，沙箱能用的部分被预留量压得很小：

| 项 | worker-1 | worker-2 | worker-3 | 是否满足 |
| --- | --- | --- | --- | --- |
| 沙箱可分配 CPU / 内存 | 12 / 47.5 GiB | 16 / 63.5 GiB | 33 / 270 GiB | 只有 worker-3 能放 128 GB 的实例；三台合计 61 核 |
| 本地盘可用（扣掉驱逐线前） | 1.5 TB | 0.5 TB | 2.7 TB | 满足，worker-2 偏紧 |
| 共享挂载 | 有 | 有 | 有 | 满足，但有频繁的短时卡顿（见 storage.md） |
| `vm.max_map_count` | 65530 | 132065675 | 65530 | worker-1、worker-3 要改 |
| 单沙箱 PID 上限 | 128 | 128 | 128 | 对并行构建不够 |
| 单实例上限 | 32 CPU / 256 GiB / 300 GiB 盘 / 6 小时 | | | 能放一个大实例；平台的总额度只有 32 CPU / 256 GiB |

结论：这套环境够做低并发的流程验证，不够做超大仓库的全量构建。要扩，先在宿主上商定能让给沙箱的 CPU 和内存，调低预留量，再提高平台额度；PID 上限要为构建单独设计。

## 怎样核对

```bash
# 每台 worker（只读）
ssh <worker> 'LOCAL_PATH=<local> SHARED_PATH=<shared> RUNTIME_DIR=<runtime-dir> \
  MIN_CPUS=32 MIN_MEM_GB=128 MIN_LOCAL_FREE_GB=300 USE_SUDO=1 bash -s' < infra/scripts/node_check.sh

# 集群（在 hub 上，只读）
ssh <hub> 'SELECTOR="<sandbox-node-selector>" EXPECTED_NODES=worker-1,worker-2,worker-3 \
  SANDBOX_NS=<sandbox-namespace> GATEWAY_NS=<gateway-namespace> GATEWAY_DEPLOY=<gateway-deployment> \
  bash -s' < infra/scripts/cluster_check.sh
```

`node_check.sh` 对每项给出 PASS、WARN、FAIL；有 FAIL 时退出码为 1。它按 kubelet 的算法判断驱逐余量：可用空间 ÷ 总容量，文件系统给 root 保留的块算作已用。所以它的“空闲比例”会比 `df` 的 `Use%` 推出来的数低几个点。
