# Stages and measurements

All runs on one machine: Apple M-series laptop, 12 cores, 16 GB RAM, macOS, Lean via elan.
"Workers" are separate OS processes with separate workspaces and build directories; they share
the project source directory (same absolute path, as on real machines) and one local store.
Times are wall-clock seconds unless marked CPU. Every distributed run was compared with a plain
`lake build` of the same sources on the same machine.

## Stage a: tiny synthetic project (tests)

`tests/test_end_to_end.py`: 40 generated modules (every 5th with a linter warning), 3 worker
processes x 2 threads, static plan (9 shards) and dynamic schedule, one injected failure each.

- all modules built; the injected failure was retried on another worker;
- assembled `.olean`/`.ilean`/`.c` identical to `lake build`;
- `lake build --no-build --rehash` accepts the assembled build directory;
- `leanchecker` accepts every module.

## Stage b: synthetic projects with the shape of a real import graph

Import structure copied from two sub-graphs of the Riemann-hypothesis archive's import graph
(175,187 modules); file contents generated (they import only Lean core). Lean v4.33.1.

### b1: 316 modules, 648 edges, depth 60

| Run | Batches / shards | Wall | Notes |
|---|---:|---:|---|
| `lake build` (one process, 12 threads) | — | 15.9 s | reference |
| static plan, 4 workers x 3 threads | 72 shards | 31.3 s | shard-level critical path 3.5x the module-level one |
| dynamic, 4 x 3, batch budget 60 | 20 | 21.1 s | |
| dynamic, 4 x 3, batch budget 20 | 47 | 20.5 s | |
| dynamic, 12 x 1, batch budget 20 | 51 | 24.7 s | |

Modules here take about 0.2 s each, so the fixed cost of a Lake invocation (about 0.5-1 s; median
lake wall time per batch 1.1 s for 8 modules) dominates. This stage checks mechanics, not speed.
The static schedule is slower because a shard must finish completely before any importer starts;
this is why `dynamic` is the default.

### b2: 2,613 modules, 7,522 edges, depth 95

One generated module imports 370 modules and its `omega` proof takes about 90 s; it is on the
critical path of both runs.

| | `lake build` | `lakeshard build`, dynamic, 4 workers x 3 threads |
|---|---:|---:|
| wall | 277.6 s | 342.7 s |
| CPU (user + sys) | 1,187 s | 1,128 s (workers) |
| jobs | — | 37 batches, 0 failures |
| bytes fetched by workers / uploaded to the store | — | 888 MB / 405 MB |
| peak RSS of one job | — | 6.2 GB |

After the build:

- `assemble`: 18,291 files, 405 MB, 3.6 s;
- `verify --reference`: 7,839 `.olean`/`.ilean`/`.c` files identical, none different or missing;
- `lake build --no-build --rehash` on the assembled directory: "All targets up-to-date (2615 jobs)", 0.45 s;
- `leanchecker` on every module, 8 at a time: 2,613 / 2,613 accepted, 1,085 s (mostly loading
  imports; macOS maps files slowly), peak RSS 6.7 GB.

On one machine the distributed build cannot be faster than `lake build`: the workers share the
same cores. The overhead here is about 23 % (Lake start-up per batch, copying outputs, idle time
at batch boundaries).

## Stage c: a real Mathlib-based project

A mid-sized private project that depends on Mathlib (a copy; Mathlib v4.35.0 from its cache,
cloned with APFS copy-on-write), Lean v4.35.0: 230 modules, 442 internal edges, 67,807 lines, depth 33,
widest level 20 modules.

Per-module times measured in the reference build (Lake's own "Built X (time)" lines) sum to
2,066 s. Their critical path is **444 s**, and one file takes
317 s of it. So no number of machines can build this project in less than about 444 s: the
largest possible speed-up over one core is 4.65, and 8 slots already reach 451 s in simulation
(`lakeshard analyze --history`).

| | `lake build`, 8 threads | `lakeshard build`, dynamic, 4 workers x 2 threads |
|---|---:|---:|
| wall | 503.2 s | 596.7 s |
| CPU (user + sys) | 2,219 s | 2,160 s |
| peak RSS (one process tree) | 3.6 GB | 3.8 GB |
| jobs | — | 27 batches, 0 failures |
| bytes fetched by workers / uploaded | — | 391 MB / 418 MB |
| per-batch Lake start-up (checks ~1,300 Mathlib traces) | — | about 4-6 s |

Where the extra 94 s go (from `events.jsonl`): the batch holding that 317-second file started at 161 s and
ended at 506 s; the chain of batches leading to it (47 s, 52 s, 62 s) ran slower than the chain
modules alone because each batch also carried other ready modules on only 2 threads, and the
modules after it waited for its whole batch (11 modules). A single-machine `lake build` has
neither effect.

Correctness, on the assembled build directory (230 modules, 1,610 files, 418 MB, assembled in 1.0 s):

- `verify --reference`: 690 `.olean`/`.ilean`/`.c` files identical to the reference, none different or missing;
- `lake build --no-build --rehash`: "All targets up-to-date (3412 jobs)" (Mathlib included), 5.9 s;
- `leanchecker`, 4 at a time: 229 / 229 runs accepted, 1,368 s, peak RSS 3.3 GB. The library's root module
  was skipped (`--prefix-modules skip`): its file has only 69 `import` lines and
  no declarations, and `leanchecker` on it would re-check all 200-odd modules below it
  in one process (on macOS that run stalled in unmapping memory; it was stopped after 33 minutes).

What this stage shows: the tool handles a real Mathlib project (dependencies shared read-only,
outputs identical to `lake build`), and its overhead on one machine is about 19 % here. What it
cannot show: a speed-up, because this project is bound by one 5-minute file. Distribution pays
off only when total work / critical path is large: about 400 for the Riemann-hypothesis archive
(175,187 modules), 4.65 here.

## Stage S1 (M1): direct `lean` backend, pull workers, shared build directory (one machine)

Same machine as above (12 cores, 16 GB, macOS). During all S1 runs an unrelated single-threaded
job kept one core busy, so about 11 cores were free. Each row is one run (no repetitions yet).

What was built (see README, "Direct `lean` backend"): an HTTP coordinator (`service.py`) around a
pure scheduling state machine (`scheduler.py`), resident workers that pull tasks
(`pullworker.py`), one `lean` process per module with Lake's exact arguments (`lakeenv.py`,
`direct.py`), outputs written to a temporary directory and renamed into one shared build
directory, `.olean` last, and the coordinator releasing importers only after it sees the files.

### Are the `lean` calls the same as Lake's?

`lakeshard check-setup` compares, for every module, the computed `.setup.json` (name, package,
`isModule`, options, dynlibs, plugins, `importArts`), the full command line and `LEAN_PATH` with
what a real `lake build` wrote (`ir/<M>.setup.json`, and the command logged in `<M>.trace`):

| project | Lean | modules checked | mismatches |
|---|---|---:|---:|
| synthetic, 40 modules | v4.33.1 | 40 / 40 | 0 |
| synthetic, 316 modules (shape of a sub-graph of the RH archive) | v4.33.1 | 316 / 316 | 0 |
| `module`-system test project (`public`/`meta`/`all` imports, two libraries with different `leanOptions`, `weakLeanArgs`, `moreLeanArgs`) | v4.35.0 | 6 / 6 | 0 |
| the Mathlib-based project of stage c (230 modules; Mathlib entries in `importArts` with `.olean`, `.olean.server`, `.olean.private`, `.ir.sig`, `.ir`) | v4.35.0 | 230 / 230 | 0 |

A plain `cd project && lake build` also passes absolute source paths to `lean` (checked), so the
outputs can be compared with any ordinary Lake build of the same checkout.

### Outputs and times

Reference: `lake build` with Lean's default threads. Direct backend: 4 workers x 3 slots,
`LEAN_NUM_THREADS=3` per `lean`, shared build directory. Lake batches: 4 workers x 3 threads
(batch budget 15 s and 20 s). All runs were given the reference's per-module times as history
(priorities). "wall" is the whole command; "coordinator" is from coordinator start to the last
confirmed module. Lower bound = max(critical path, work / slots) from the run's own per-module
times; Lake's own job pool is counted as 12 slots.

| project (modules) | run | wall s | coordinator s | CPU s | largest RSS | slots | critical path s | work / slots s | bound s | wall / bound | files same / different / missing |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| synthetic (40) | `lake build` | 2.4 | - | 8 | 0.43 GB | 12 | 2.4 | 0.7 | 2.4 | 1.03 | - |
| synthetic (40) | direct `lean`, shared dir, 4 x 3 slots | 3.1 | 2.5 | 9 | 0.43 GB | 12 | 2.4 | 0.7 | 2.4 | 1.27 | 120 / 0 / 0 |
| synthetic (40) | Lake batches, 4 x 3 threads (1 batch) | 3.0 | 3.0 | 8 | 0.43 GB | 12 | 2.2 | 0.6 | 2.2 | 1.35 | 120 / 0 / 0 |
| synthetic (316) | `lake build` | 17.8 | - | 103 | 0.52 GB | 12 | 16.2 | 9.5 | 16.2 | 1.10 | - |
| synthetic (316) | direct `lean`, shared dir, 4 x 3 slots | 18.3 | 17.7 | 108 | 0.52 GB | 12 | 17.4 | 9.4 | 17.4 | 1.05 | 948 / 0 / 0 |
| synthetic (316) | Lake batches, 4 x 3 threads (9 batches) | 24.4 | 24.3 | 80 | 0.52 GB | 12 | 13.0 | 6.2 | 13.0 | 1.87 | 948 / 0 / 0 |

Checks on the direct backend's outputs:

- byte for byte identical to `lake build` (`.olean`, `.ilean`, `.c`): 120 / 120 and 948 / 948 files;
  for the `module`-system project also `.olean.server`, `.olean.private`, `.ir`, `.ir.sig`:
  34 / 34 (tests);
- `leanchecker` on every module: 40 / 40 (6 s) and 316 / 316 (91 s, 6 at a time);
- per-directory store mode (`--storage store`, the control): 120 / 120 identical after `assemble`;
- injected failure (`--fail-once Synth.M7`): retried on another worker, build complete;
- a worker that takes tasks and disappears: its tasks are retried elsewhere after the lease (3 s
  in the test), its late report is ignored; a separately started worker joins with URL and token
  and finishes the build (test).

Where the direct backend's time goes (316 modules, from `events.jsonl`): per task, everything
outside the `lean` process (setup computation, visibility check, renames, report) takes 4 ms
(median; 1.3 s for all 316); a module whose imports are all confirmed is dispatched at once
(median 0 s, 90th percentile 0.12 s when all slots were busy); confirming outputs takes 0.1 ms.
The whole command adds about 0.5 s of start-up (Python, graph, worker processes) before the
coordinator starts. Both synthetic graphs are bound by their critical path (16-17 s of 18 s for
316 modules), so on one machine the direct backend can only match `lake build`, which it does
(1.05 of its own bound against 1.10 for `lake build`); the Lake batch backend stays about 1.4-1.9
times its bound (Lake start-up per batch, whole batch before importers).

Not done in S1: the 2,613-module synthetic project and the Mathlib-based project were started
and stopped (priority changed to "first make it run"); they will be measured on the
cross-machine form of S2. The direct backend writes no Lake `.trace`/`.hash` files, so
`lake build --no-build` does not accept its build directory yet (S3).

## Known limits and next steps

S1 (direct backend) next: S2 per-worker local build directories with a store interface (shared
directory and HTTP); S3 `run` (repository in, cache out) and `restore`, Lake traces for the direct
backend, Mathlib-style `lakefile.lean` projects; S4 three machines; S5 scale tests with fake
workers. The items below are about the Lake batch backend.

- Batches report only when complete; streaming each module's outputs as soon as Lake reports
  it built would remove most of the waiting seen in stage c.
- Critical-path batches should get more threads and no unrelated modules.
- One Lake invocation per batch re-checks the traces of the whole import closure (seconds for
  Mathlib; about 44 s for a full no-build check of the 175,187-module archive, as reported by its
  maintainers), so batches for huge projects must be coarse, or a direct-`lean` backend (one
  `lean` process per module with a Lake-style `--setup` file, as in the prototype) is needed.
- Remote workers (stage d) need the project at the same absolute path, the same toolchain and
  the dependencies' build outputs (e.g. `lake exe cache get`) on each machine, and a store
  reachable over the network (HTTP or S3-compatible).
