#!/usr/bin/env bash
# Read-only check of one sandbox worker node against the requirements in infra/node-requirements.md.
#
# Run it on the node itself, for example:
#   ssh <node> 'LOCAL_PATH=<local> SHARED_PATH=<shared> RUNTIME_DIR=<runtime-dir> bash -s' < node_check.sh
#
# Each check prints one line "LEVEL key detail" with LEVEL in PASS, WARN, FAIL, INFO.
# Exit status is 1 when any check FAILs, else 0. Nothing on the node is changed.
#
# Parameters (environment variables):
#   LOCAL_PATH          local disk that will hold build outputs (<local>)            default /
#   SHARED_PATH         shared mount (<shared>); empty skips the shared checks       default empty
#   RUNTIME_DIR         container runtime data directory (image layers, writable
#                       container layers); empty skips                              default empty
#   KUBELET_DIR         kubelet root directory (emptyDir volumes, pod logs)          default /var/lib/kubelet
#   WORK_PATH           absolute path that must exist identically on every worker;
#                       empty skips                                                  default empty
#   MIN_CPUS            minimum logical CPUs                                         default 32
#   MIN_MEM_GB          minimum physical memory in GB                                default 128
#   MIN_LOCAL_FREE_GB   minimum free space under LOCAL_PATH in GB                    default 300
#   MIN_MAX_MAP_COUNT   minimum vm.max_map_count                                     default 1048576
#   IMAGEFS_EVICT_PCT   kubelet eviction threshold imagefs.available, percent        default 15
#   NODEFS_EVICT_PCT    kubelet eviction threshold nodefs.available, percent         default 10
#   MARGIN_PCT          free space wanted above an eviction threshold, percent       default 5
#   USE_SUDO            1 allows `sudo -n` for read-only steps (kernel log, root-only
#                       directories)                                                 default 0
set -u
export LC_ALL=C

LOCAL_PATH=${LOCAL_PATH:-/}
SHARED_PATH=${SHARED_PATH:-}
RUNTIME_DIR=${RUNTIME_DIR:-}
KUBELET_DIR=${KUBELET_DIR:-/var/lib/kubelet}
WORK_PATH=${WORK_PATH:-}
MIN_CPUS=${MIN_CPUS:-32}
MIN_MEM_GB=${MIN_MEM_GB:-128}
MIN_LOCAL_FREE_GB=${MIN_LOCAL_FREE_GB:-300}
MIN_MAX_MAP_COUNT=${MIN_MAX_MAP_COUNT:-1048576}
IMAGEFS_EVICT_PCT=${IMAGEFS_EVICT_PCT:-15}
NODEFS_EVICT_PCT=${NODEFS_EVICT_PCT:-10}
MARGIN_PCT=${MARGIN_PCT:-5}
USE_SUDO=${USE_SUDO:-0}

failures=0
say() { printf '%-4s %-24s %s\n' "$1" "$2" "$3"; [ "$1" = FAIL ] && failures=$((failures + 1)); return 0; }
as_root() { if [ "$USE_SUDO" = 1 ]; then sudo -n "$@"; else "$@"; fi; }

# Free GB, total GB, free percent, filesystem type, device, mount point of the filesystem holding $1.
# Free percent is available/size, the ratio kubelet compares with its eviction thresholds
# (blocks reserved for root count as used, so it is lower than 100 - df's Use%).
fs_facts() {
  as_root df -P -k "$1" 2>/dev/null | awk 'NR==2 {printf "%d %d %d", $4/1048576, $2/1048576, ($4*100)/$2}'
  printf ' %s %s %s\n' "$(as_root findmnt -n -o FSTYPE -T "$1" 2>/dev/null)" \
    "$(as_root findmnt -n -o SOURCE -T "$1" 2>/dev/null)" "$(as_root findmnt -n -o TARGET -T "$1" 2>/dev/null)"
}

rotational() {  # prints 1 for spinning disk, 0 for SSD, ? when unknown
  local dev r
  dev=$(as_root findmnt -n -o SOURCE -T "$1" 2>/dev/null)
  r=$(lsblk -dno ROTA "$dev" 2>/dev/null | head -1 | tr -d ' ')
  echo "${r:-?}"
}

check_dir_fs() {  # key path eviction_pct
  local key=$1 path=$2 evict=$3 real facts free total used fstype src target freepct
  real=$(as_root readlink -f "$path" 2>/dev/null || echo "$path")
  if ! as_root test -e "$real"; then say WARN "$key" "$path does not exist"; return; fi
  facts=$(fs_facts "$real"); read -r free total freepct fstype src target <<<"$facts"
  say INFO "$key" "path=$path resolved=$real fs=$fstype mount=$target device=$src rotational=$(rotational "$real")"
  case "$fstype" in nfs*|cifs|fuse*) say FAIL "$key-fs" "on $fstype; container runtime and kubelet data must stay on a local disk";; esac
  if [ "$freepct" -lt "$evict" ]; then
    say FAIL "$key-free" "${free}G free of ${total}G (${freepct}% free), below the ${evict}% eviction threshold"
  elif [ "$freepct" -lt $((evict + MARGIN_PCT)) ]; then
    say WARN "$key-free" "${free}G free of ${total}G (${freepct}% free), within ${MARGIN_PCT}% of the ${evict}% eviction threshold"
  else
    say PASS "$key-free" "${free}G free of ${total}G (${freepct}% free), eviction at ${evict}%"
  fi
}

say INFO host "$(uname -sr); $(grep -h PRETTY_NAME /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '"')"

cpus=$(nproc)
model=$(lscpu 2>/dev/null | awk -F: '/^Model name/ {sub(/^ +/, "", $2); print $2; exit}')
[ "$cpus" -ge "$MIN_CPUS" ] && say PASS cpus "$cpus logical ($model)" || say FAIL cpus "$cpus logical, want >= $MIN_CPUS ($model)"

mem_gb=$(awk '/^MemTotal/ {printf "%d", $2/1000000}' /proc/meminfo)
avail_gb=$(awk '/^MemAvailable/ {printf "%d", $2/1000000}' /proc/meminfo)
[ "$mem_gb" -ge "$MIN_MEM_GB" ] && say PASS memory "${mem_gb}G total, ${avail_gb}G available" \
  || say FAIL memory "${mem_gb}G total, want >= ${MIN_MEM_GB}G"
say INFO swap "$(awk '/^SwapTotal/ {t=$2} /^SwapFree/ {f=$2} END {printf "%dG total, %dG used", t/1048576, (t-f)/1048576}' /proc/meminfo)"
say INFO load "$(cut -d' ' -f1-3 /proc/loadavg)"

mmc=$(sysctl -n vm.max_map_count 2>/dev/null || echo 0)
[ "$mmc" -ge "$MIN_MAX_MAP_COUNT" ] && say PASS vm.max_map_count "$mmc" \
  || say FAIL vm.max_map_count "$mmc, want >= $MIN_MAX_MAP_COUNT (host-wide sysctl; cannot be set per container)"
say INFO kernel.pid_max "$(sysctl -n kernel.pid_max 2>/dev/null)"

# Local disk for build outputs.
read -r free total freepct fstype src target <<<"$(fs_facts "$LOCAL_PATH")"
say INFO local "path=$LOCAL_PATH fs=$fstype mount=$target device=$src rotational=$(rotational "$LOCAL_PATH")"
case "$fstype" in nfs*|cifs|fuse*) say FAIL local-fs "LOCAL_PATH is on $fstype, not a local disk";; esac
[ "$free" -ge "$MIN_LOCAL_FREE_GB" ] && say PASS local-free "${free}G free of ${total}G (${freepct}% free)" \
  || say FAIL local-free "${free}G free of ${total}G, want >= ${MIN_LOCAL_FREE_GB}G"

# Container runtime (imagefs) and kubelet (nodefs).
[ -n "$RUNTIME_DIR" ] && check_dir_fs runtime "$RUNTIME_DIR" "$IMAGEFS_EVICT_PCT"
check_dir_fs kubelet "$KUBELET_DIR" "$NODEFS_EVICT_PCT"

# Shared mount.
if [ -n "$SHARED_PATH" ]; then
  opts=$(findmnt -n -o FSTYPE,OPTIONS -T "$SHARED_PATH" 2>/dev/null)
  stype=${opts%% *}; sopts=$(printf '%s' "${opts#* }" | tr -d ' ')
  if [ "$(findmnt -n -o TARGET -T "$SHARED_PATH" 2>/dev/null)" = / ]; then
    say FAIL shared "$SHARED_PATH is not a separate mount (it resolves to /)"
  else
    case "$stype" in nfs*) say PASS shared "$stype $SHARED_PATH";; *) say WARN shared "$SHARED_PATH is $stype, not NFS";; esac
    say INFO shared-options "$sopts"
    case ",$sopts," in *,soft,*) say WARN shared-mount "soft mount: a slow server returns I/O errors to builds";; esac
    timeo=$(printf '%s' "$sopts" | tr ',' '\n' | awk -F= '$1=="timeo" {print $2}')
    [ -n "$timeo" ] && [ "$timeo" -lt 100 ] && say WARN shared-timeo "timeo=$timeo (tenths of a second); short timeouts log 'not responding' on every brief server stall"
    read -r free total freepct _ <<<"$(fs_facts "$SHARED_PATH")"
    say INFO shared-free "${free}G free of ${total}G (${freepct}% free)"
    if test -w "$SHARED_PATH"; then say PASS shared-write "writable by $(id -un)"; else say WARN shared-write "not writable by $(id -un) at the top level; check the subdirectory you will use"; fi
    stalls=$(as_root dmesg 2>/dev/null | grep -c -i 'nfs: server .* not responding')
    if as_root dmesg >/dev/null 2>&1; then
      [ "$stalls" -gt 0 ] && say WARN shared-stalls "$stalls 'not responding' kernel messages since boot ($(uptime -s)); hard mounts block during each stall" \
        || say PASS shared-stalls "no 'not responding' messages since boot"
    else
      say INFO shared-stalls "kernel log not readable; rerun with USE_SUDO=1"
    fi
  fi
fi

# Same absolute path on every worker.
if [ -n "$WORK_PATH" ]; then
  case "$WORK_PATH" in /*) ;; *) say FAIL work-path "$WORK_PATH is not absolute";; esac
  if [ -e "$WORK_PATH" ]; then
    read -r free total freepct fstype _ target <<<"$(fs_facts "$WORK_PATH")"
    say PASS work-path "$WORK_PATH exists on $fstype mount $target (${free}G free)"
  else
    say WARN work-path "$WORK_PATH does not exist yet"
  fi
fi

[ "$failures" -eq 0 ] && say INFO result "no FAIL" || say INFO result "$failures FAIL"
[ "$failures" -eq 0 ]
