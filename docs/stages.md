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

`odd-singular-vector/lean` at commit `a032dae` (a copy; Mathlib v4.35.0 from its cache, cloned
with APFS copy-on-write), Lean v4.35.0: 230 modules, 442 internal edges, 67,807 lines, depth 33,
widest level 20 modules.

Per-module times measured in the reference build (Lake's own "Built X (time)" lines) sum to
2,066 s. Their critical path is **444 s**, and one module, `OddSingularVector.F4.CertR7`, takes
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

Where the extra 94 s go (from `events.jsonl`): the batch holding `F4.CertR7` started at 161 s and
ended at 506 s; the chain of batches leading to it (47 s, 52 s, 62 s) ran slower than the chain
modules alone because each batch also carried other ready modules on only 2 threads, and the
modules after it waited for its whole batch (11 modules). A single-machine `lake build` has
neither effect.

Correctness: VERIFY_PLACEHOLDER

What this stage shows: the tool handles a real Mathlib project (dependencies shared read-only,
outputs identical to `lake build`), and its overhead on one machine is about 19 % here. What it
cannot show: a speed-up, because this project is bound by one 5-minute file. Distribution pays
off only when total work / critical path is large: about 400 for the Riemann-hypothesis archive
(175,187 modules), 4.65 here.

## Known limits and next steps

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
