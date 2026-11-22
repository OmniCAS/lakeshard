# lakeshard

Split a Lean 4 / Lake project along its import graph, build the pieces on several workers, move
the build outputs between them through a content-addressed store, then assemble one build
directory and check it.

The aim is to make very large Lean repositories (hundreds of thousands of modules) buildable in
the time of their critical path instead of the time one machine needs for all the work.

Status: 0.1, tested on one machine simulating several workers (see "Stages" below). Remote
workers are not implemented yet; the core is a library with plain JSON messages so that an HTTP
or queue front end can be added without rewriting it.

## Install

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e ".[dev]"
```

No runtime dependencies beyond the Python standard library (3.11+). Building needs the project's
Lean toolchain installed through elan.

## Commands

```bash
lakeshard analyze PROJECT                      # modules, edges, depth, critical-path estimates
lakeshard plan PROJECT --shards 32 --out plan.json
lakeshard reference PROJECT --build-dir REF    # plain single-machine lake build, timed
lakeshard build PROJECT --work WORK --workers 4 --threads 3
lakeshard assemble PROJECT --store WORK/store --dest OUT
lakeshard verify PROJECT --build-dir OUT --reference REF --leanchecker-jobs 8 [--prefix-modules skip]
lakeshard synth OUT --modules 200              # synthetic project for tests and benchmarks
```

`build` options: `--schedule dynamic|static`, `--batch-cost SECONDS` (dynamic), `--plan` /
`--shards` / `--target-cost` (static), `--retries`, `--timeout`, `--store DIR`,
`--fail-once ID` (test hook: the first attempt of that batch or shard fails).

All commands print JSON. `build` writes `WORK/report.json`, `WORK/events.jsonl` (one line per
job), `WORK/history.json` (measured seconds per module, used for later cost estimates) and keeps
each worker's workspace under `WORK/workers/`.

## How it works

1. **Graph.** `lakefile.toml` is read with `tomllib`; the modules of the default targets and their
   imports are found by reading only the import headers of the source files. Nothing from the
   project is executed. (`lakefile.lean` is not supported: convert it with
   `lake translate-config toml`.)
2. **Costs.** Measured seconds per module when known (`history.json`), otherwise
   `2 s + 0.04 s/line + 0.5 ms per imported project module`.
3. **Schedule.**
   - `dynamic` (default): when a worker is free, the coordinator takes the ready module with the
     longest remaining path (preferring modules whose imports the worker already holds) and
     extends the batch along its import chain, then with other ready modules, up to a cost budget.
     After a failed batch its modules are retried one by one on another worker.
   - `static`: a precomputed plan. Modules are visited in topological order and join the shard of
     their imports while that keeps the shard under the target cost and cannot create a cycle
     between shards (shard ancestry is tracked exactly with bitsets). `check_plan` verifies the
     partition and acyclicity.
4. **Worker.** Each worker has a persistent workspace with its own build directory. For a job it
   writes a copy of the project's `lakefile.toml` with `buildDir` set to that directory, fetches
   the outputs of every module the batch imports (`.olean`, `.ilean`, `.c`, Lake's `.trace` and
   `.hash` files) from the store, checking their sha256, runs stock `lake build +M ...` for the
   batch, and uploads the new outputs. Lake accepts the fetched outputs as up to date because the
   traces depend on content hashes, not on paths; the worker fails the job if Lake rebuilds
   anything outside the batch.
5. **Store.** Objects named by sha256 plus one record per module (`ModuleRecord`: file paths and
   hashes). `LocalStore` is a directory; other stores implement the same five methods.
6. **Assemble and verify.** All records are copied into one build directory. `verify` runs
   `lake build --no-build --rehash` against it, compares `.olean`/`.ilean`/`.c` byte for byte with a
   reference build, and replays every module through the kernel with `leanchecker` (optionally
   `leanchecker --fresh` on a root module).

### Rules that make outputs identical

- The project source must be at the **same absolute path** on every worker. Lean 4.33 stores the
  source path of modules with linter messages in their `.olean` (`lintLogExt`). On one machine
  all simulated workers share the project directory and differ only in `buildDir`.
- Same toolchain and same dependency builds (e.g. Mathlib from its official cache) everywhere.
- Lake's own hashes are 64-bit and not meant to resist an adversary; the store uses sha256.

### Trust

Workers run the project's code (its tactics and metaprograms), so treat them as untrusted and run
them in sandboxes for untrusted projects. A wrong or tampered output can only make the final
check fail: the result should be judged by the kernel on a trusted machine (`leanchecker`, an
export plus independent kernels, `lake comparator`), not by the fact that it was built.

## Library and protocol

```python
from lakeshard import api
api.analyze(project)                      # dict
plan = api.plan(project, shards=32)       # lakeshard.protocol.Plan
report = api.build(project, work, workers=4, threads=3)
api.assemble(project, store, dest)
api.verify(project, dest, reference=ref, leanchecker_jobs=8)
```

Messages (`lakeshard.protocol`, all JSON-serialisable dataclasses): `Plan` / `ShardSpec`,
`ShardJob` (modules to build, modules needed, project and toolchain, store location),
`ShardResult` (records produced, timings, CPU, peak RSS, bytes moved, error),
`ModuleRecord` / `FileRef`. The coordinator talks to workers only through an `Executor`
(`submit(worker, job)`, `poll()`); `LocalProcessExecutor` starts one OS process per job with
`python -m lakeshard worker run --job job.json --result result.json --workspace DIR`.
A remote executor or an HTTP service would send the same job JSON.

## Stages

| Stage | What | Result |
|---|---|---|
| a | tiny synthetic project (tests) | `pytest`: 40 modules, 3 worker processes, static and dynamic schedules, injected failure retried, assembled build identical to `lake build`, `--no-build --rehash` and `leanchecker` pass |
| b | one machine simulating several workers on synthetic projects with the shape of real import graphs | 2,613 modules: 4 workers x 3 threads, 343 s (`lake build` 278 s); 7,839 files identical; `--no-build --rehash` and `leanchecker` (2,613/2,613) pass |
| c | a real Mathlib-based project (odd-singular-vector, 230 modules) | 4 x 2: 597 s (`lake build` 8 threads 503 s; critical path 444 s, so at most 4.65x on any number of machines); 690 files identical; `--no-build --rehash` and `leanchecker` pass |
| d | sandboxes / remote workers | not started |

## Tests

```bash
.venv/bin/python -m pytest -q          # tests marked `lean` need leanprover/lean4:v4.33.1 via elan
```
