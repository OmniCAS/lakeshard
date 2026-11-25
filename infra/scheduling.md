# 只调度到指定节点

目标：沙箱只落在 `worker-1`、`worker-2`、`worker-3`。hub 和集群里的其他节点一个都不接。

## 机制

沙箱平台用一份工作负载模板创建 Pod，模板里用 `nodeSelector` 要求节点带上几类标签：

| 标签（示例键名） | 含义 | 谁来设 |
| --- | --- | --- |
| `sandbox.example.org/enabled=true` | 这台节点接沙箱 | 管理员；K8s 发行版的节点配置里也写一份，防止重启 agent 时被改回 |
| `sandbox.example.org/tier=worker` | 角色是 worker（hub 是 `control`） | 同上 |
| `sandbox.example.org/pids-attested=<n>` | 这台节点的每 Pod PID 上限已核实为 n | 一个定时任务：按节点 UID、boot ID 和 kubelet 实际配置核实后才打上，核实失败就摘掉 |

符合条件的节点 = 三类标签都对。第三类标签不能手工补：它是“隔离措施已生效”的证据，手工补就失去了意义。

## 做法

| 要做的 | 怎么做 | 不这样做的原因 |
| --- | --- | --- |
| hub 不接沙箱 | `enabled=false`、`tier=control`；控制面组件用 `nodeSelector` 固定在 hub 上 | 用污点也能做，但还得给每个控制面组件加容忍；标签更直接 |
| 把多余的节点移出池子 | `kubectl label node <node> sandbox.example.org/enabled=false --overwrite`，同时写进该节点的 K8s 发行版配置（不需要重启） | 不用 cordon：cordon 会挡住这台节点上的所有工作负载，不只是沙箱 |
| 节点暂时不健康 | 交给 kubelet 自己的污点（`DiskPressure` 等），修好就自动撤 | 不要手工去掉 kubelet 的污点 |
| 已在运行的实例 | 改标签只影响新调度，不会驱逐已有实例 | |

改完用 `scripts/cluster_check.sh` 核对：符合条件的节点集合必须正好等于 `EXPECTED_NODES`。

示例环境改之前的读数：

```text
FAIL eligible-extra   nodes outside the pool still match: <extra-node>
FAIL node/worker-2    DiskPressure taint=node.kubernetes.io/disk-pressure:NoSchedule
FAIL gateway          1 ready on 1 node(s): worker-1; want >= 2 on distinct nodes
```

## 资源预留：和宿主上其他负载共存

worker 是和别人共用的机器。示例环境用 kubelet 的 `system-reserved` 和 `kube-reserved` 把沙箱能用的部分压小（例如 128 线程的机器只给沙箱 12–33 核）。调度器按这个“可分配量”算，不按物理核数算。

- 想让沙箱多用，就调低 `system-reserved`。这要先和宿主上的其他用户商定，改完重启 agent。
- `kubectl top node` 的百分比按可分配量算，包含宿主上的全部负载，超过 100% 是正常的，不影响调度。

## 四层额度

一个实例能不能创建，取决于四层的交集：

| 层 | 管什么 |
| --- | --- |
| 调用方授权 | 每个调用方的并发数、累计 CPU/内存、单实例最长时间、允许的镜像（精确到 digest） |
| 平台账本 | 按调用方原子地累计预约；临时盘有总预约 |
| K8s 命名空间 | `ResourceQuota`（Pod 数、合计 CPU/内存/临时盘）和 `LimitRange`（单容器上下限、默认值） |
| 节点 | 各节点剩余的可分配量和磁盘余量 |

例如示例环境：命名空间配额是 32 Pod、32 CPU、256 GiB，一个 32 CPU / 256 GiB 的大实例就会占满整个命名空间。同时，某个调用方的授权可能只有 3.2 CPU / 16 GiB。所以“配置的上限”不等于“实际能同时跑的量”，规划并发时要四层一起算。

## 运行时入口网关

沙箱里的命令和文件操作都经过一个运行时入口网关（反向代理到各沙箱里的执行代理）。它只有一个副本时就是单点：所在节点被驱逐或宕机，所有实例的命令和文件操作都会失败。

- 副本数 ≥ 3，每个 worker 一个：用 `topologySpreadConstraints`，`topologyKey: kubernetes.io/hostname`，`maxSkew: 1`，`whenUnsatisfiable: DoNotSchedule`，并加上 `matchLabelKeys: [pod-template-hash]`。
  - 不加后者时，滚动更新期间还在退出的旧副本也会被算进去。示例环境里第一次滚动的结果是两个新副本落在同一台、另一台一个都没有，补上它再滚一次才均匀。
- 加一个 `PodDisruptionBudget`，`minAvailable: 2`。
- 它的 `nodeSelector` 和沙箱用同一组标签，这样不会落到池外节点。
- 改模板会触发滚动更新：先起新的再停旧的，但旧副本上正在进行的长连接会断，所以要在没有活动实例时改。
- 如果网关的 Deployment 由部署工具生成，要把同样的设置写回部署源，否则下次部署会被改回去。

注意先后顺序：先解除节点的磁盘压力，再加按主机名打散的约束。新版 K8s 计算打散时默认把带污点的节点也算进去，节点还带污点时新副本会一直 Pending。

## 出站：沙箱能连到哪里

只有入站策略时，沙箱的出站是全开的。示例环境实测，从沙箱里能建立 TCP 连接的有：

- hub 的 K8s API；
- 集群内的 kubernetes 服务；
- 共享盘 NFS 服务器的 2049、111 端口；
- worker 节点的 SSH 和 kubelet 端口。

只有平台自己的服务被入站策略挡住。共享盘按 UID 认人（`sec=sys`），这是最大的暴露面：不可信代码可以用用户态 NFS 客户端冒充任意 UID，能否得手取决于服务器是否要求特权源端口。

建议加一条出站策略（K8s NetworkPolicy，`policyTypes: [Egress]`）：

- 允许集群 DNS；
- 允许公网（`0.0.0.0/0` 减去内网网段）；
- 拒绝节点网段、存储网段、hub 和集群服务网段；
- 构建任务确实需要访问的内网服务（例如产物库），单独按 IP 和端口放行。

改之前先确认现有调用方没有依赖内网访问。

## 节点之间的网络

示例环境三台 worker 之间：

| 项 | 实测 |
| --- | --- |
| 往返时间 | 约 0.2 ms |
| 10 GbE 直连 TCP 吞吐 | 7.1–9.1 Gbit/s |
| 1 GbE 管理网 | 0.91 Gbit/s |
| 防火墙 | worker-1 不接受另外两台连入的高位端口，另外两台可以 |

跨机传产物或开协调端口前，先逐对测一遍连通性，不要假设节点之间全通。
