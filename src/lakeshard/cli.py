"""Command line: `lakeshard <command> ...` (see `lakeshard --help`)."""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__, api
from .protocol import Plan


def _print(obj) -> None:
    print(json.dumps(obj, indent=1, ensure_ascii=False) if not isinstance(obj, str) else obj)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lakeshard", description=__doc__)
    ap.add_argument("--version", action="version", version=f"lakeshard {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="import graph statistics and time estimates")
    a.add_argument("project")
    a.add_argument("--history", help="JSON of measured seconds per module")
    a.add_argument("--slots", default="1,4,16,64,256")

    p = sub.add_parser("plan", help="split the project into shards")
    p.add_argument("project")
    p.add_argument("--target-cost", type=float, help="seconds of work per shard")
    p.add_argument("--shards", type=int, help="number of shards (alternative to --target-cost)")
    p.add_argument("--history")
    p.add_argument("--out", required=True)

    b = sub.add_parser("build", help="build all shards on local worker processes")
    b.add_argument("project")
    b.add_argument("--work", required=True, help="work directory (plan, store, workers, logs)")
    b.add_argument("--workers", type=int, default=2)
    b.add_argument("--threads", type=int, default=0, help="LEAN_NUM_THREADS per worker (0 = default)")
    b.add_argument("--schedule", choices=["dynamic", "static"], default="dynamic")
    b.add_argument("--batch-cost", type=float, help="dynamic: estimated seconds of work per batch")
    b.add_argument("--plan", help="static: use this plan.json instead of planning")
    b.add_argument("--target-cost", type=float)
    b.add_argument("--shards", type=int)
    b.add_argument("--store", help="artifact store directory (default <work>/store)")
    b.add_argument("--history")
    b.add_argument("--retries", type=int, default=2)
    b.add_argument("--timeout", type=float, default=7200.0, help="seconds per shard job")
    b.add_argument("--fail-once", action="append", default=[], help="shard id whose first attempt fails (test)")
    b.add_argument("--toolchain-bin")

    s = sub.add_parser("assemble", help="put every module's outputs from the store into one build dir")
    s.add_argument("project")
    s.add_argument("--store", required=True)
    s.add_argument("--dest", required=True)

    v = sub.add_parser("verify", help="check an assembled build")
    v.add_argument("project")
    v.add_argument("--build-dir", required=True)
    v.add_argument("--reference", help="build dir of a plain lake build to compare with")
    v.add_argument("--no-rehash", action="store_true")
    v.add_argument("--leanchecker-jobs", type=int, default=0)
    v.add_argument("--fresh-root", help="also run leanchecker --fresh on this module")
    v.add_argument("--prefix-modules", choices=["run", "skip"], default="run",
                   help="modules that are also a name prefix of others (leanchecker re-checks their subtree)")
    v.add_argument("--toolchain-bin")

    r = sub.add_parser("reference", help="plain single-machine lake build into a build dir (timed)")
    r.add_argument("project")
    r.add_argument("--build-dir", required=True)
    r.add_argument("--threads", type=int, default=0)
    r.add_argument("--toolchain-bin")

    y = sub.add_parser("synth", help="write a synthetic Lake project")
    y.add_argument("out")
    y.add_argument("--modules", type=int, default=100)
    y.add_argument("--max-imports", type=int, default=4)
    y.add_argument("--locality", type=int, default=30)
    y.add_argument("--work", type=int, default=3, help="small theorems per module")
    y.add_argument("--toolchain", default="leanprover/lean4:v4.33.1")
    y.add_argument("--random-state", type=int, default=1)

    w = sub.add_parser("worker", help="worker side (used by executors)")
    wsub = w.add_subparsers(dest="wcmd", required=True)
    wr = wsub.add_parser("run", help="run one shard job file")
    wr.add_argument("--job", required=True)
    wr.add_argument("--result", required=True)
    wr.add_argument("--workspace", required=True)
    wr.add_argument("--worker-id", required=True)

    args = ap.parse_args(argv)
    if args.cmd == "analyze":
        _print(api.analyze(args.project, args.history, [int(x) for x in args.slots.split(",")]))
    elif args.cmd == "plan":
        pl = api.plan(args.project, args.target_cost, args.shards, args.history)
        pl.save(args.out)
        _print(pl.stats)
    elif args.cmd == "build":
        rep = api.build(args.project, args.work, workers=args.workers, threads=args.threads,
                        plan_obj=Plan.load(args.plan) if args.plan else None, schedule=args.schedule,
                        batch_cost=args.batch_cost, target_cost=args.target_cost,
                        shards=args.shards, store=args.store, history=args.history, retries=args.retries,
                        timeout_s=args.timeout, fail_once=set(args.fail_once), toolchain=args.toolchain_bin)
        _print({k: v for k, v in rep.items() if k not in ("per_worker",)})
        return 0 if rep["ok"] else 1
    elif args.cmd == "assemble":
        res = api.assemble(args.project, args.store, args.dest)
        _print(res)
        return 0 if res["ok"] else 1
    elif args.cmd == "verify":
        res = api.verify(args.project, args.build_dir, args.reference, not args.no_rehash,
                         args.leanchecker_jobs, args.fresh_root, args.toolchain_bin, args.prefix_modules)
        _print(res)
        return 0 if res["ok"] else 1
    elif args.cmd == "reference":
        res = api.reference_build(args.project, args.build_dir, args.threads, args.toolchain_bin)
        _print(res)
        return 0 if res["ok"] else 1
    elif args.cmd == "synth":
        from .synth import random_dag, write_project
        imps = random_dag(args.modules, args.max_imports, args.locality, random_state=args.random_state)
        _print(write_project(args.out, imps, toolchain=args.toolchain, work=[args.work] * args.modules))
    elif args.cmd == "worker" and args.wcmd == "run":
        from .worker import main_run
        return main_run(args.job, args.result, args.workspace, args.worker_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
