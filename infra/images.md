# 镜像的构建与发放

## 流程

```text
配方（仓库里的目录） ─PR─> CI：构建 linux/amd64、离线冒烟、产出 OCI 归档与回执
   └─合并后手动发布─> CI 推到托管仓库（私有）+ 归档作为构建产物保留
        └─运维核验取件：按白名单核对来源、提交、运行号、每个 blob 的 sha256、冒烟结果、大小
             └─发布到镜像仓库（项目级配额；发布身份只有 pull/push）
                  └─预载到每台 worker：导入、pin、加 repo@digest 别名
                       └─准入：给指定调用方加精确的镜像 URI（带 digest）
                            └─任务按 digest 创建沙箱
```

| 原则 | 说明 |
| --- | --- |
| 构建不在集群里做 | 由 CI 构建，集群节点只导入核验过的归档 |
| 用 digest，不用 tag | tag 方便人读；准入、预载、创建都按 digest |
| 发布和准入是两步 | 镜像发布到仓库，不等于任何调用方能用它；准入只加精确 URI |
| 准入前先清空 | 改调用方的镜像列表前，确认它没有活动或未决的实例，免得撤掉正在用的能力 |
| 预载而不是运行时拉取 | worker 的网络条件不一；运行时拉大镜像会拖慢启动或失败。三台都要预载，之后才准入 |
| 版本有上限 | 每个镜像系列最多 pin 两个版本；加第三个前先退役旧的授权和缓存 |
| 凭据分开 | 构建用 CI 的短期身份；发布用只有 pull/push 的发布身份；沙箱的调用 Key 不能发布镜像 |

## 把单镜像流程通用化

示例环境的第一版流程是为一个 Python 示例镜像写的。加第二个镜像时，会在下面几处卡住，都要改成按配方声明：

| 环节 | 第一版写死的 | 改成 |
| --- | --- | --- |
| 配方 | 必须有 `Dockerfile`、`requirements.lock`、`workload.py`、`.dockerignore`，每个 ≤1 MiB | 每个配方带一份 `image.json`（见下） |
| CI 冒烟 | 固定运行 `python …/workload.py --self-test`，1 CPU / 512 MiB / 128 PID，无网络、只读根，构建限时 15 分钟 | 冒烟命令、资源和期望输出从 `image.json` 读；只读根下给 `/tmp` 一个 tmpfs；构建限时按配方设 |
| 运维取件 | 产物名、镜像引用前缀、冒烟结果都按那一个镜像比对，归档上限 1 GiB | 按镜像名查受审的白名单（名称、期望冒烟结果、归档上限），新镜像先进白名单 |
| 预载 | 节点列表写死 | 节点列表取自调度池（和 `cluster_check.sh` 的 `EXPECTED_NODES` 一致）；可以先把归档放到 `<shared>`，各节点从共享盘导入 |
| 容量 | 镜像仓库项目配额固定 | 发布前按归档大小 × 保留版本数核算配额 |

`image.json` 示例：

```json
{
  "name": "build-tools",
  "platform": "linux/amd64",
  "build_timeout_minutes": 30,
  "max_archive_bytes": 4294967296,
  "smoke": {
    "argv": ["/opt/smoke/run.sh"],
    "network": false,
    "read_only_root": true,
    "tmpfs": ["/tmp"],
    "cpus": 2,
    "memory": "2g",
    "pids": 256,
    "expect_stdout_json": {"ok": true}
  }
}
```

冒烟输出要固定（不含时间、路径、随机数），这样运维取件时才能逐字节比对。

建议先用一个很小的新镜像把通用化后的流程从头走一遍（例如只含 `git`、`curl`、`zstd` 的基础工具镜像），再用大镜像。

## 例子：Lean 构建镜像

这一节用来检验流程能不能满足后续任务，不是构建方案本身。

配方要点：

- 基础镜像固定 digest；装 `git`、`curl`、`zstd`、`ca-certificates`。
- 用固定版本的 elan 安装器（校验 sha256），安装一个指定的 toolchain（版本作为构建参数，归档同样校验 sha256），设为默认。
- 不开 elan 的自动更新；运行时不联网也能用。
- 冒烟：`lean --version` 输出与 toolchain 版本一致，再离线编译一个小文件并检查 `.olean` 生成。
- 不把 Mathlib 缓存放进镜像：它很大，而且跟着 Mathlib 的提交变。放在 `<shared>` 做只读共享缓存，由任务按版本取用。

```dockerfile
FROM <base-image>@sha256:<digest>
ARG ELAN_VERSION=<version>
ARG ELAN_SHA256=<sha256>
ARG LEAN_TOOLCHAIN=leanprover/lean4:<version>
RUN apt-get update && apt-get install -y --no-install-recommends git curl zstd ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/elan.tar.gz "https://github.com/leanprover/elan/releases/download/v${ELAN_VERSION}/elan-x86_64-unknown-linux-gnu.tar.gz" \
 && echo "${ELAN_SHA256}  /tmp/elan.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/elan.tar.gz -C /tmp && /tmp/elan-init -y --no-modify-path --default-toolchain "${LEAN_TOOLCHAIN}" \
 && rm -f /tmp/elan.tar.gz /tmp/elan-init
ENV PATH=/root/.elan/bin:$PATH ELAN_HOME=/root/.elan
COPY smoke/ /opt/smoke/
CMD ["/bin/sh", "-c", "sleep infinity"]
```

（示意：实际配方还要锁定 apt 包版本，并按沙箱平台的要求选择运行用户；如果沙箱不以 root 运行，`ELAN_HOME` 要放到该用户可读的位置。）

预计会碰到的限制：

| 限制 | 影响 | 处理 |
| --- | --- | --- |
| 归档上限（第一版 1 GiB） | 一个 toolchain 解压后约 1–2 GB，压缩后的层大小要实测，可能超限 | 在 `image.json` 里按实测设上限，相应提高镜像仓库配额 |
| 构建限时（第一版 15 分钟） | 下载和解压工具链可能超时 | 按配方设 |
| 每沙箱 128 PID | `lean` 按核数开线程，并行时很快超限 | 镜像里默认设 `LEAN_NUM_THREADS`；平台侧为构建单独设计 PID 上限 |
| 容器只读根和临时盘大小 | 构建要写大量文件 | 构建目录用 `<local>` 上的挂载或足够大的临时盘，路径在容器内固定 |
