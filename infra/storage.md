# 落盘原则

## 什么放哪里

| 数据 | 放在 | 原因 |
| --- | --- | --- |
| 容器运行时数据 `<runtime-dir>`（镜像层、容器可写层；kubelet 称 imagefs） | 本地盘 | overlayfs 不能建在 NFS 上；启动和写入都很频繁 |
| kubelet 目录（emptyDir 卷、Pod 日志；kubelet 称 nodefs） | 本地盘 | 同上；而且它的余量决定 Pod 会不会被驱逐 |
| 沙箱的临时目录、正在构建的工作目录 `<local>/…` | 本地盘 | 大量小文件、频繁 stat、文件锁 |
| 数据库（sqlite 等）、git 工作区 | 本地盘 | 依赖可靠的锁和同步写 |
| 打包好的构建产物（写一次、读多次的大文件） | `<shared>` | 各 worker 都能直接读，不用点对点传 |
| 镜像归档（OCI tar）的中转 | `<shared>` | 每台 worker 从同一份导入，不用各自下载 |
| 只读的共享缓存（工具链、依赖缓存的压缩包） | `<shared>`，只读挂进沙箱 | 一份供所有沙箱用 |
| 任务输入与结果 | `<shared>` 下按任务分目录 | 先写到临时名，写完再改名，读方只认改名后的文件 |

## 怎样挑“相对空闲”的盘

依次看：

1. **余量**：可用空间 ÷ 总容量，和驱逐阈值比。kubelet 默认在 imagefs 空闲低于 15%、nodefs 低于 10% 时开始驱逐。
2. **是不是别人的盘**：放着其他用户数据、还在涨的盘，哪怕现在空闲多也不要放运行时数据。别人的数据涨上来，kubelet 就会驱逐你的 Pod，删镜像也腾不出空间。
3. **介质**：SSD 优先；机械盘只适合放冷数据。
4. **当前忙不忙**：`scripts/io_sample.sh` 采样 10 秒的忙碌比例和吞吐。

示例环境里，三台 worker 都把运行时数据放在一块共用的 `/home` 上：

| 节点 | 运行时数据原来所在 | 情况 | 处理 |
| --- | --- | --- | --- |
| worker-1 | 4 TB SATA SSD，已用 57% | 余量足 | 不动 |
| worker-2 | 7.2 TB SATA SSD，已用 86%（大部分是别人的数据） | 低于 15% 阈值，节点被打上 `DiskPressure` 污点，所有新沙箱都调度不上 | 迁到根盘 NVMe（剩 514 GB） |
| worker-3 | 4 TB SATA SSD，已用 97% | 曾因磁盘压力驱逐过 Pod | 已迁到根盘（剩 2.7 TB） |

还有一个容易忽略的点：kubelet 目录默认在根盘上。worker-1 的根盘已用 81%，离 10% 的驱逐线只剩约 150 GB。那台机器上的 Pod（包括入口网关）随时可能被别人写满根盘挤掉。

## 迁移容器运行时数据目录

以 K3s 为例（数据目录由 `data-dir` 指定，运行时数据在 `<data-dir>/agent/containerd`）。思路是复制、校验，再把原路径换成符号链接，旧目录原样保留作回退。不改配置文件，所以回滚只是换回目录。

前提：这台节点上没有运行中的沙箱 Pod（先把它从调度池里拿出来，或者确认它本来就调度不上）。

```bash
D=<data-dir-parent>        # 例如 K3s data-dir 的上一级
N=<local>/k3s-data         # 新位置，在空闲的本地盘上
TS=$(date -u +%Y%m%dT%H%M%SZ)
sudo install -d -m 700 "$N"
sudo systemctl stop k3s-agent
test "$(grep -c "$D/data" /proc/mounts)" = 0                     # 不应再有挂载
sudo rsync -aHAX --numeric-ids "$D/data/" "$N/data/"
test "$(sudo rsync -aHAXn --numeric-ids --checksum --delete --itemize-changes "$D/data/" "$N/data/" | wc -l)" = 0
sudo mv "$D/data" "$D/data.before-$TS"
sudo ln -s "$N/data" "$D/data"
sudo systemctl start k3s-agent
```

失败处理：

- 在 `mv` 之前失败：直接 `systemctl start k3s-agent`，原目录没动过。
- 回滚：停 agent，删掉符号链接本身，把 `data.before-$TS` 改回 `data`，再启动。新位置的副本保留。

验证：

- 节点 `Ready`。
- kubelet 的 `stats/summary` 里，`imageFs` 的容量和可用量变成新盘的数字。
- kubelet 默认要等 5 分钟才撤掉 `DiskPressure`，之后污点消失。
- 节点上的其他认证标签（例如 PID 限额认证）仍在。只重启 agent、不重启机器，不会改变 boot ID。

迁移后，如果 imagefs 和 nodefs 落在同一块盘上，两个阈值作用于同一块盘，按更严的那个算（15%）。

另一种做法是把 imagefs 的阈值改成绝对值（例如 `imagefs.available<128Gi`）。它不用搬数据，但盘还是和别人共用，别人的数据会继续涨，只是推迟了问题。

## 共享盘（NFS）的注意事项

示例环境的共享盘：NFSv4.2，`hard`、TCP、`timeo=5`、`retrans=2`、`sec=sys`，rsize/wsize 1 MiB，经独立 10 GbE 存储网挂载，往返 0.2–0.4 ms。

| 现象 | 影响 | 对策 |
| --- | --- | --- |
| 内核日志里频繁出现 `nfs: server … not responding`，每次 2–7 秒后恢复（三台都有，最多的一台 74 天 13,000 多次）；客户端重传很少，说明是服务器忙 | `hard` 挂载下，读写 NFS 的进程会阻塞到恢复为止 | 只放能容忍几秒停顿的大文件交换；关键路径（运行时、kubelet、临时目录）不碰 NFS；超时按分钟设，不按秒设 |
| `timeo=5`（0.5 秒）很短 | 服务器稍慢就报“not responding”，日志很吵，判断故障时容易误读 | TCP 下常用 `timeo=600`；改挂载参数要和共享盘的管理者商量 |
| `sec=sys` 按 UID 认人 | 容器里的进程以什么 UID 运行，就以什么身份读写 NFS；如果服务器没开 root_squash，容器里的 root 就是 NFS 上的 root | 给沙箱只挂专用子目录（不要挂整个共享，上面有别人的数据）；固定沙箱进程的 UID；缓存只读挂载；先确认服务器的 root_squash 设置 |
| `hard` 挂载在服务器长时间不响应时 | 用到它的 Pod 可能卡在终止状态，节点上的 kubelet 统计也可能变慢 | 给任务设总超时；监控 `not responding` 的频率；共享目录不要挂进不需要它的沙箱 |
| hub 不在存储网上 | hub 不能直接读写共享盘 | 需要经共享盘中转的事（例如镜像归档）在某台 worker 上做 |

读写基准建议在自己的子目录里做：每台 worker 依次写读 2 GiB 大文件、建 2,000 个小文件，测完删掉。不要几台同时测，以免给共享服务器加压。
