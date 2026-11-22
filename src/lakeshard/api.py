"""Library entry points. The CLI is a thin layer over these; a service would call them too."""
from __future__ import annotations

import json
import os
import time

from .assemble import assemble as _assemble
from .coordinator import BuildOptions, Coordinator, DynamicCoordinator, LocalProcessExecutor, WorkerSpec
from .plan import estimate_costs, load_history, make_plan
from .project import LakeProject, toolchain_bin
from .protocol import Plan
from .sim import simulate
from .verify import compare_builds, lake_no_build, leanchecker_all
from .worker import BUILT_RE, _seconds, lake_env, run_tracked


def analyze(project_dir: str, history: str | None = None, slots: list[int] | None = None) -> dict:
    proj = LakeProject.load(project_dir)
    g = proj.graph()
    depth = g.depth()
    clos = g.closure_sizes()
    costs = estimate_costs(g, load_history(history))
    width: dict[int, int] = {}
    for d in depth.values():
        width[d] = width.get(d, 0) + 1
    cs = sorted(clos.values())
    out = {"project": proj.package, "modules": len(g.modules), "targets": len(g.targets),
           "edges": sum(len(i.imports) for i in g.modules.values()),
           "lines": sum(i.lines for i in g.modules.values()),
           "external_imports": sorted({x for i in g.modules.values() for x in i.external})[:20],
           "depth": max(depth.values()), "max_width": max(width.values()),
           "closure_median": cs[len(cs) // 2], "closure_max": cs[-1],
           "cost_source": "history" if history else "estimate (lines)",
           "estimates": [simulate(g, costs, p) for p in (slots or [1, 4, 16, 64, 256])]}
    return out


def plan(project_dir: str, target_cost: float | None = None, shards: int | None = None,
         history: str | None = None) -> Plan:
    g = LakeProject.load(project_dir).graph()
    return make_plan(g, estimate_costs(g, load_history(history)), target_cost=target_cost, shards=shards)


def build(project_dir: str, work_dir: str, workers: int = 2, threads: int = 0, plan_obj: Plan | None = None,
          schedule: str = "dynamic", batch_cost: float | None = None, target_cost: float | None = None,
          shards: int | None = None, store: str | None = None, history: str | None = None, retries: int = 2,
          timeout_s: float = 7200.0, fail_once: set | None = None, toolchain: str | None = None) -> dict:
    """Build every module on `workers` local worker processes and record what happened.

    schedule="dynamic" forms batches at run time (cost budget `batch_cost`, default: total
    estimated work / (6 * workers)); schedule="static" builds the shards of a plan (`plan_obj`, or
    one made with `target_cost`/`shards`).
    """
    proj = LakeProject.load(project_dir)
    g = proj.graph()
    work_dir = os.path.abspath(work_dir)
    os.makedirs(work_dir, exist_ok=True)
    hist_out = os.path.join(work_dir, "history.json")  # never overwrite a history given as input
    hist_in = load_history(history) if history else load_history(hist_out)
    costs = estimate_costs(g, hist_in)
    store = store or os.path.join(work_dir, "store")
    opts = BuildOptions(proj.project_dir, proj.lakefile, toolchain_bin(proj.project_dir, toolchain), store,
                        retries=retries, timeout_s=timeout_s, fail_once=set(fail_once or ()))
    ws = [WorkerSpec(f"w{i}", threads) for i in range(workers)]
    ex = LocalProcessExecutor(work_dir)
    events = os.path.join(work_dir, "events.jsonl")
    if schedule == "static" or plan_obj is not None:
        if plan_obj is None:
            plan_obj = make_plan(g, costs, target_cost=target_cost, shards=shards or max(1, workers * 4))
        plan_obj.save(os.path.join(work_dir, "plan.json"))
        report = Coordinator(g, plan_obj, ex, ws, opts, events_path=events).run()
        report["plan"] = plan_obj.stats
    elif schedule == "dynamic":
        total = sum(costs.values())
        opts.batch_cost = batch_cost or min(600.0, max(10.0, total / (6 * max(1, workers))))
        report = DynamicCoordinator(g, costs, ex, ws, opts, events_path=events).run()
    else:
        raise ValueError(f"unknown schedule {schedule!r}")
    report["store"] = store
    hist = dict(hist_in)  # measured per-module times, for the next run's estimates
    hist.update(report.pop("module_seconds"))
    with open(hist_out, "w", encoding="utf-8") as fh:
        json.dump(hist, fh, indent=0, sort_keys=True)
    report["history"] = hist_out
    with open(os.path.join(work_dir, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    return report


def assemble(project_dir: str, store: str, dest_build_dir: str) -> dict:
    g = LakeProject.load(project_dir).graph()
    return _assemble(g, store, dest_build_dir)


def verify(project_dir: str, build_dir: str, reference: str | None = None, rehash: bool = True,
           leanchecker_jobs: int = 0, fresh_root: str | None = None, toolchain: str | None = None,
           prefix_modules: str = "run") -> dict:
    proj = LakeProject.load(project_dir)
    g = proj.graph()
    tb = toolchain_bin(proj.project_dir, toolchain)
    out = {"lake_no_build": lake_no_build(proj, build_dir, tb, rehash=rehash)}
    if reference:
        out["compare"] = compare_builds(g, reference, build_dir)
    if leanchecker_jobs or fresh_root:
        out["leanchecker"] = leanchecker_all(proj, g, build_dir, tb, jobs=max(1, leanchecker_jobs),
                                             fresh_root=fresh_root, prefix_modules=prefix_modules)
    out["ok"] = all(v.get("ok", True) for v in out.values() if isinstance(v, dict))
    return out


def reference_build(project_dir: str, build_dir: str, threads: int = 0, toolchain: str | None = None) -> dict:
    """A plain single-machine `lake build` into `build_dir` (for comparison and timing)."""
    proj = LakeProject.load(project_dir)
    tb = toolchain_bin(proj.project_dir, toolchain)
    os.makedirs(build_dir, exist_ok=True)
    cfg = os.path.abspath(build_dir).rstrip("/") + ".lakefile.toml"
    with open(cfg, "w", encoding="utf-8") as fh:
        fh.write(proj.worker_lakefile(build_dir))
    t0 = time.time()
    code, out, wall, cpu, rss, _ = run_tracked(
        [os.path.join(tb, "lake"), "-d", proj.project_dir, "-f", cfg, "--no-ansi", "build"],
        lake_env(tb, threads), 24 * 3600)
    secs = {}
    for mm in BUILT_RE.finditer(out):
        sec = _seconds(mm.group(2), mm.group(3))
        if sec is not None:
            secs[mm.group(1)] = sec
    base = os.path.abspath(build_dir).rstrip("/")
    hist = base + ".history.json"
    with open(hist, "w", encoding="utf-8") as fh:
        json.dump(secs, fh, indent=0, sort_keys=True)
    with open(base + ".log", "w", encoding="utf-8") as fh:  # full Lake output, for later analysis
        fh.write(out)
    return {"ok": code == 0, "exit": code, "wall_s": round(wall, 2), "cpu_s": round(cpu, 2),
            "max_rss_bytes": rss, "threads": threads, "build_dir": os.path.abspath(build_dir),
            "modules_built": len(secs), "module_seconds_sum": round(sum(secs.values()), 2), "history": hist,
            "output_tail": "\n".join(out.strip().splitlines()[-3:]), "started": t0}
