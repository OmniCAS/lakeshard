#!/usr/bin/env bash
# Read-only disk activity sample, to tell which local disk is idle.
# Usage: ssh <node> 'SECONDS_TO_SAMPLE=10 bash -s' < io_sample.sh
# Prints per disk: busy percent (time with I/O in flight), read and write MB/s over the interval.
set -u
S=${SECONDS_TO_SAMPLE:-10}
snap() { awk '$3 ~ /^(sd[a-z]+|nvme[0-9]+n[0-9]+|vd[a-z]+)$/ {print $3, $6, $10, $13}' /proc/diskstats | sort; }
a=$(snap); sleep "$S"; b=$(snap)
join <(echo "$a") <(echo "$b") | awk -v s="$S" '{printf "%-10s busy=%3.0f%% read=%7.1fMB/s write=%7.1fMB/s\n", $1, ($7-$4)/(s*10), ($5-$2)*512/1e6/s, ($6-$3)*512/1e6/s}'
