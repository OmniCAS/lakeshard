#!/usr/bin/env bash
# Read-only check that sandboxes can only land on the intended worker nodes and that those nodes
# and the runtime gateway are healthy. See infra/scheduling.md.
#
# Run it where kubectl reaches the cluster, for example on the hub:
#   ssh <hub> 'KUBECTL="kubectl" SELECTOR="..." EXPECTED_NODES=worker-1,worker-2,worker-3 bash -s' < cluster_check.sh
#
# Output: one line "LEVEL key detail" per check (PASS, WARN, FAIL, INFO). Exit 1 on any FAIL.
#
# Parameters (environment variables):
#   KUBECTL          command that runs kubectl (may include sudo or a kubeconfig)   default kubectl
#   SELECTOR         label selector the sandbox workload template requires (required)
#   EXPECTED_NODES   comma-separated node names; the eligible set must equal it      default empty (no check)
#   SANDBOX_NS       namespace of sandbox pods (quota, placement of running pods)    default empty
#   GATEWAY_NS       namespace of the runtime gateway deployment                     default empty
#   GATEWAY_DEPLOY   name of the runtime gateway deployment                          default empty
#   MIN_GATEWAY      minimum ready gateway replicas on distinct nodes                default 2
set -u
export LC_ALL=C
KUBECTL=${KUBECTL:-kubectl}
SELECTOR=${SELECTOR:?set SELECTOR to the label selector of sandbox-eligible nodes}
EXPECTED_NODES=${EXPECTED_NODES:-}
SANDBOX_NS=${SANDBOX_NS:-}
GATEWAY_NS=${GATEWAY_NS:-}
GATEWAY_DEPLOY=${GATEWAY_DEPLOY:-}
MIN_GATEWAY=${MIN_GATEWAY:-2}

failures=0
say() { printf '%-4s %-24s %s\n' "$1" "$2" "$3"; [ "$1" = FAIL ] && failures=$((failures + 1)); return 0; }
k() { $KUBECTL "$@"; }

eligible=$(k get nodes -l "$SELECTOR" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort)
if [ -z "$eligible" ]; then say FAIL eligible "no node matches $SELECTOR"; else say INFO eligible "$(echo $eligible | tr ' ' ',')"; fi

if [ -n "$EXPECTED_NODES" ]; then
  want=$(echo "$EXPECTED_NODES" | tr ',' '\n' | sort)
  extra=$(comm -23 <(echo "$eligible") <(echo "$want") | tr '\n' ' ')
  missing=$(comm -13 <(echo "$eligible") <(echo "$want") | tr '\n' ' ')
  [ -n "$extra" ] && say FAIL eligible-extra "nodes outside the pool still match: $extra"
  [ -n "$missing" ] && say FAIL eligible-missing "pool nodes that do not match: $missing"
  [ -z "$extra$missing" ] && say PASS eligible-set "exactly $EXPECTED_NODES"
fi

for n in $eligible; do
  line=$(k get node "$n" -o jsonpath='{range .status.conditions[*]}{.type}={.status} {end}|{range .spec.taints[*]}{.key}:{.effect} {end}|{.spec.unschedulable}|{.status.allocatable.cpu} {.status.allocatable.memory} {.status.allocatable.ephemeral-storage}')
  conds=${line%%|*}; rest=${line#*|}; taints=${rest%%|*}; rest=${rest#*|}; unsched=${rest%%|*}; alloc=${rest#*|}
  bad=""
  case " $conds" in *" Ready=True"*) ;; *) bad="$bad not-Ready";; esac
  for c in DiskPressure MemoryPressure PIDPressure; do case " $conds" in *" $c=True"*) bad="$bad $c";; esac; done
  for t in $taints; do case "$t" in *:NoSchedule|*:NoExecute) bad="$bad taint=$t";; esac; done
  [ "$unsched" = true ] && bad="$bad cordoned"
  if [ -n "$bad" ]; then say FAIL "node/$n" "${bad# }"; else say PASS "node/$n" "Ready, no pressure, no blocking taint"; fi
  say INFO "alloc/$n" "cpu memory ephemeral = $alloc"
done

if [ -n "$GATEWAY_NS" ] && [ -n "$GATEWAY_DEPLOY" ]; then
  ready=$(k -n "$GATEWAY_NS" get deploy "$GATEWAY_DEPLOY" -o jsonpath='{.status.readyReplicas}')
  sel=$(k -n "$GATEWAY_NS" get deploy "$GATEWAY_DEPLOY" -o jsonpath='{range $k,$v := .spec.selector.matchLabels}{$k}={$v},{end}' 2>/dev/null)
  if [ -z "$sel" ]; then
    sel=$(k -n "$GATEWAY_NS" get deploy "$GATEWAY_DEPLOY" -o go-template='{{range $k,$v := .spec.selector.matchLabels}}{{$k}}={{$v}},{{end}}')
  fi
  nodes=$(k -n "$GATEWAY_NS" get pods -l "${sel%,}" --field-selector=status.phase=Running -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' | sort -u)
  distinct=$(echo "$nodes" | grep -c .)
  if [ "${ready:-0}" -ge "$MIN_GATEWAY" ] && [ "$distinct" -ge "$MIN_GATEWAY" ]; then
    say PASS gateway "${ready} ready on $distinct nodes: $(echo $nodes | tr ' ' ',')"
  else
    say FAIL gateway "${ready:-0} ready on $distinct node(s): $(echo $nodes | tr ' ' ','); want >= $MIN_GATEWAY on distinct nodes"
  fi
  for n in $nodes; do echo "$eligible" | grep -qx "$n" || say WARN "gateway/$n" "gateway runs on a node outside the pool"; done
fi

if [ -n "$SANDBOX_NS" ]; then
  k -n "$SANDBOX_NS" get resourcequota -o jsonpath='{range .items[*]}{.metadata.name}: used {.status.used} of {.status.hard}{"\n"}{end}' \
    | while read -r q; do say INFO quota "$q"; done
  running=$(k -n "$SANDBOX_NS" get pods --field-selector=status.phase=Running -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}')
  say INFO sandbox-pods "$(echo "$running" | grep -c .) running"
  for n in $(echo "$running" | sort -u); do
    echo "$eligible" | grep -qx "$n" && say INFO "pods/$n" "$(echo "$running" | grep -cx "$n")" \
      || say FAIL "pods/$n" "$(echo "$running" | grep -cx "$n") sandbox pod(s) on a node outside the pool"
  done
fi

[ "$failures" -eq 0 ] && say INFO result "no FAIL" || say INFO result "$failures FAIL"
[ "$failures" -eq 0 ]
