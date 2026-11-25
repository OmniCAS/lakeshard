#!/usr/bin/env python3
"""Run stability tiers against a sandbox platform and write a JSON report.

Tiers (see infra/stability.md):

    serial:N            N sequential lifecycles: create, write a file, run a command that hashes it
                        and writes the hash to a new file, read both back, delete, prove absence.
    ramp:1,2,4,8,12     keep instances alive and grow to each target in turn; at every stage all
                        live instances run a marker command; delete all at the end.
    repeat:NxR          R rounds of "create N at once, check, delete all".
    soak:N:SECONDS:EVERY  N instances alive for SECONDS, each doing a command and a file round
                        trip every EVERY seconds.

A tier passes when every operation succeeds, every instance ran on a node in --allowed-nodes (when
given) and every deleted instance is proven absent. The run passes when every tier passes, the
backend reports nothing left for the run, and (with --require-all-nodes) the run as a whole placed
at least one instance on every allowed node.

Backend: ``--backend fake`` (in memory; for dry runs and tests) or ``--backend module:factory``.
``factory(config: dict)`` returns an object with these coroutine methods::

    create(run_id: str, index: int, ttl_seconds: int) -> handle   # handle.id is a string
    node(handle) -> str | None
    write(handle, path: str, data: bytes) -> None
    read(handle, path: str) -> bytes
    run(handle, argv: list[str], timeout: float) -> tuple[int, str]   # exit code, stdout
    delete(handle) -> None
    absent(handle) -> bool        # positive proof that the instance and its resources are gone
    owned(run_id: str) -> list[str]   # ids the platform still holds for this run
    close() -> None

The adapter keeps its own credentials; never put them in --backend-config. ``create`` must be safe
to retry with the same (run_id, index): adapters should send a fixed idempotency key.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import re
import secrets
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

PAYLOAD_BYTES = 64 * 1024


# ---------------------------------------------------------------- fake backend (tests, dry runs)

@dataclass
class _FakeInstance:
    id: str
    run_id: str
    node_name: str
    files: dict[str, bytes] = field(default_factory=dict)
    alive: bool = True


class FakeBackend:
    """In-memory stand-in. config: nodes (list), fail_create (list of indexes), off_pool (list of
    indexes placed on node "outside"), leak (list of indexes that delete() leaves behind),
    latency (seconds per call)."""

    def __init__(self, config: dict[str, Any]):
        self.nodes = list(config.get("nodes", ["worker-1", "worker-2", "worker-3"]))
        self.fail_create = set(config.get("fail_create", []))
        self.off_pool = set(config.get("off_pool", []))
        self.leak = set(config.get("leak", []))
        self.latency = float(config.get("latency", 0.0))
        self.instances: dict[str, _FakeInstance] = {}
        self.by_key: dict[tuple[str, int], str] = {}
        self.counter = 0

    async def _tick(self):
        if self.latency:
            await asyncio.sleep(self.latency)

    async def create(self, run_id, index, ttl_seconds):
        await self._tick()
        if index in self.fail_create:
            raise RuntimeError("fake create refused")
        key = (run_id, index)
        if key in self.by_key:  # idempotent replay
            return self.instances[self.by_key[key]]
        node = "outside" if index in self.off_pool else self.nodes[self.counter % len(self.nodes)]
        self.counter += 1
        inst = _FakeInstance(id=str(uuid.uuid4()), run_id=run_id, node_name=node)
        self.instances[inst.id] = inst
        self.by_key[key] = inst.id
        return inst

    async def node(self, handle):
        await self._tick()
        return handle.node_name

    async def write(self, handle, path, data):
        await self._tick()
        handle.files[path] = bytes(data)

    async def read(self, handle, path):
        await self._tick()
        return handle.files[path]

    async def run(self, handle, argv, timeout):
        await self._tick()
        script = argv[-1]
        m = re.fullmatch(r"sha256sum (\S+) \| cut -d' ' -f1 > (\S+) && cat \2", script)
        if m:
            digest = hashlib.sha256(handle.files[m.group(1)]).hexdigest()
            handle.files[m.group(2)] = (digest + "\n").encode()
            return 0, digest + "\n"
        m = re.fullmatch(r"echo (\S+)", script)
        if m:
            return 0, m.group(1) + "\n"
        return 127, ""

    async def delete(self, handle):
        await self._tick()
        if self._index_of(handle) in self.leak:
            return
        handle.alive = False

    def _index_of(self, handle):
        for (_, index), ident in self.by_key.items():
            if ident == handle.id:
                return index
        return None

    async def absent(self, handle):
        await self._tick()
        return not handle.alive

    async def owned(self, run_id):
        return [i.id for i in self.instances.values() if i.run_id == run_id and i.alive]

    async def close(self):
        return None


def load_backend(spec: str, config: dict[str, Any]):
    if spec == "fake":
        return FakeBackend(config)
    module, _, attr = spec.partition(":")
    if not module or not attr:
        raise SystemExit("--backend must be 'fake' or 'module:factory'")
    sys.path.insert(0, os.getcwd())
    return getattr(importlib.import_module(module), attr)(config)


# ---------------------------------------------------------------- runner

@dataclass
class Record:
    tier: str
    index: int
    id: str | None = None
    node: str | None = None
    timings: dict[str, float] = field(default_factory=dict)
    checks: int = 0
    deleted: bool = False
    absent: bool | None = None
    errors: list[str] = field(default_factory=list)

    def ok(self, allowed: set[str] | None) -> bool:
        placed = allowed is None or (self.node in allowed)
        return bool(self.id) and placed and not self.errors and self.deleted and self.absent is True


class Runner:
    def __init__(self, backend, *, run_id: str, allowed: set[str] | None, ttl: int, op_timeout: float,
                 create_parallel: int):
        self.backend = backend
        self.run_id = run_id
        self.allowed = allowed
        self.ttl = ttl
        self.op_timeout = op_timeout
        self.create_gate = asyncio.Semaphore(create_parallel)
        self.next_index = 0
        self.live: dict[int, tuple[Any, Record]] = {}
        self.stages: list[dict[str, Any]] = []

    async def _op(self, rec: Record, name: str, coro):
        start = time.monotonic()
        try:
            return await asyncio.wait_for(coro, self.op_timeout)
        finally:
            rec.timings[name] = round(rec.timings.get(name, 0.0) + time.monotonic() - start, 3)

    @staticmethod
    def _error(name: str, exc: BaseException) -> str:
        text = str(exc).splitlines()[0][:200] if str(exc) else ""
        return f"{name}: {type(exc).__name__}{': ' + text if text else ''}"

    async def open(self, tier: str) -> Record:
        index = self.next_index
        self.next_index += 1
        rec = Record(tier=tier, index=index)
        try:
            async with self.create_gate:
                handle = await self._op(rec, "create", self.backend.create(self.run_id, index, self.ttl))
            rec.id = str(handle.id)
            self.live[index] = (handle, rec)
            rec.node = await self._op(rec, "node", self.backend.node(handle))
            if self.allowed is not None and rec.node not in self.allowed:
                rec.errors.append(f"placement: node {rec.node!r} is outside the allowed pool")
        except Exception as exc:  # noqa: BLE001 - every failure is recorded, never hidden
            rec.errors.append(self._error("create", exc))
        return rec

    async def check(self, index: int):
        handle, rec = self.live[index]
        src = f"/tmp/stability-{self.run_id[:8]}-{index}.bin"
        dst = src + ".sha256"
        payload = secrets.token_bytes(PAYLOAD_BYTES)
        try:
            await self._op(rec, "write", self.backend.write(handle, src, payload))
            code, out = await self._op(rec, "run", self.backend.run(
                handle, ["sh", "-c", f"sha256sum {src} | cut -d' ' -f1 > {dst} && cat {dst}"], self.op_timeout))
            want = hashlib.sha256(payload).hexdigest()
            if code != 0 or out.strip() != want:
                raise RuntimeError(f"hash command exit {code}, output mismatch={out.strip() != want}")
            back = await self._op(rec, "read", self.backend.read(handle, src))
            made = await self._op(rec, "read", self.backend.read(handle, dst))
            if back != payload or made.decode().strip() != want:
                raise RuntimeError("file round trip mismatch")
            rec.checks += 1
        except Exception as exc:  # noqa: BLE001
            rec.errors.append(self._error("check", exc))

    async def marker(self, index: int):
        handle, rec = self.live[index]
        token = secrets.token_hex(8)
        try:
            code, out = await self._op(rec, "run", self.backend.run(handle, ["sh", "-c", f"echo {token}"], self.op_timeout))
            if code != 0 or out.strip() != token:
                raise RuntimeError(f"marker exit {code}")
            rec.checks += 1
        except Exception as exc:  # noqa: BLE001
            rec.errors.append(self._error("marker", exc))

    async def close(self, index: int):
        handle, rec = self.live.pop(index)
        try:
            await self._op(rec, "delete", self.backend.delete(handle))
            rec.deleted = True
        except Exception as exc:  # noqa: BLE001
            rec.errors.append(self._error("delete", exc))
        deadline = time.monotonic() + self.op_timeout
        while True:
            try:
                rec.absent = await self.backend.absent(handle)
            except Exception as exc:  # noqa: BLE001
                rec.errors.append(self._error("absent", exc))
                rec.absent = None
                return
            if rec.absent or time.monotonic() > deadline:
                if not rec.absent:
                    rec.errors.append("absent: instance still present after delete")
                return
            await asyncio.sleep(min(2.0, self.op_timeout / 10))

    async def close_all(self):
        await asyncio.gather(*(self.close(i) for i in list(self.live)))

    # ---- tiers

    async def serial(self, n: int) -> list[Record]:
        out = []
        for _ in range(n):
            rec = await self.open("serial")
            if rec.index in self.live:
                if not rec.errors:
                    await self.check(rec.index)
                await self.close(rec.index)
            out.append(rec)
        return out

    async def ramp(self, targets: list[int], hold: float) -> list[Record]:
        out: list[Record] = []
        stages = []
        try:
            for target in targets:
                need = target - len(self.live)
                new = await asyncio.gather(*(self.open(f"ramp:{target}") for _ in range(max(0, need))))
                out.extend(new)
                await asyncio.gather(*(self.check(r.index) for r in new if r.index in self.live and not r.errors))
                await asyncio.gather(*(self.marker(i) for i in list(self.live)))
                stages.append({"target": target, "live": len(self.live), "at": round(time.time(), 1)})
                if hold:
                    await asyncio.sleep(hold)
        finally:
            await self.close_all()
        self.stages = stages
        return out

    async def repeat(self, n: int, rounds: int) -> list[Record]:
        out: list[Record] = []
        for r in range(rounds):
            try:
                new = await asyncio.gather(*(self.open(f"repeat:{r + 1}") for _ in range(n)))
                out.extend(new)
                await asyncio.gather(*(self.check(x.index) for x in new if x.index in self.live and not x.errors))
            finally:
                await self.close_all()
        return out

    async def soak(self, n: int, seconds: float, every: float) -> list[Record]:
        out: list[Record] = []
        try:
            new = await asyncio.gather(*(self.open("soak") for _ in range(n)))
            out.extend(new)
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                tick = time.monotonic()
                await asyncio.gather(*(self.check(i) for i, (_, rec) in list(self.live.items()) if not rec.errors))
                await asyncio.sleep(max(0.0, every - (time.monotonic() - tick)))
        finally:
            await self.close_all()
        return out


def parse_tier(text: str) -> tuple[str, tuple]:
    kind, _, arg = text.partition(":")
    try:
        if kind == "serial":
            return kind, (int(arg),)
        if kind == "ramp":
            targets = [int(x) for x in arg.split(",")]
            if targets != sorted(targets) or targets[0] < 1:
                raise ValueError
            return kind, (targets,)
        if kind == "repeat":
            n, rounds = arg.split("x")
            return kind, (int(n), int(rounds))
        if kind == "soak":
            n, seconds, every = arg.split(":")
            return kind, (int(n), float(seconds), float(every))
    except ValueError:
        pass
    raise SystemExit(f"cannot parse tier {text!r}")


async def main_async(args) -> dict[str, Any]:
    config = json.loads(open(args.backend_config).read()) if args.backend_config else {}
    backend = load_backend(args.backend, config)
    allowed = set(args.allowed_nodes.split(",")) if args.allowed_nodes else None
    run_id = args.run_id or uuid.uuid4().hex
    report: dict[str, Any] = {"run_id": run_id, "backend": args.backend, "allowed_nodes": sorted(allowed) if allowed else None,
                              "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tiers": []}
    runner = Runner(backend, run_id=run_id, allowed=allowed, ttl=args.ttl, op_timeout=args.op_timeout,
                    create_parallel=args.create_parallel)
    try:
        for text in args.tier:
            kind, params = parse_tier(text)
            start = time.monotonic()
            if kind == "ramp":
                records = await runner.ramp(*params, hold=args.hold)
            else:
                records = await getattr(runner, kind)(*params)
            entry = {
                "tier": text,
                "seconds": round(time.monotonic() - start, 1),
                "instances": len(records),
                "passed_instances": sum(r.ok(allowed) for r in records),
                "nodes": dict(Counter(r.node or "unknown" for r in records)),
                "records": [r.__dict__ for r in records],
            }
            if kind == "ramp":
                entry["stages"] = runner.stages
            entry["passed"] = entry["passed_instances"] == len(records)
            report["tiers"].append(entry)
            if not entry["passed"] and args.stop_on_failure:
                break
    finally:
        await runner.close_all()
        try:
            report["left_over"] = await backend.owned(run_id)
        except Exception as exc:  # noqa: BLE001
            report["left_over"] = None
            report["left_over_error"] = Runner._error("owned", exc)
        await backend.close()
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    seen: Counter = Counter()
    for t in report["tiers"]:
        seen.update(t["nodes"])
    report["nodes_seen"] = dict(seen)
    report["missing_nodes"] = sorted(allowed - set(seen)) if (args.require_all_nodes and allowed) else []
    report["passed"] = (bool(report["tiers"]) and all(t["passed"] for t in report["tiers"])
                        and report["left_over"] == [] and not report["missing_nodes"])
    return report


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--backend", default="fake", help="'fake' or 'module:factory'")
    p.add_argument("--backend-config", help="JSON file passed to the factory (no credentials)")
    p.add_argument("--tier", action="append", required=True, help="serial:N | ramp:1,2,4 | repeat:NxR | soak:N:SECONDS:EVERY")
    p.add_argument("--allowed-nodes", help="comma-separated nodes instances may run on")
    p.add_argument("--require-all-nodes", action="store_true", help="the run as a whole must reach every allowed node")
    p.add_argument("--ttl", type=int, default=900, help="instance time to live in seconds")
    p.add_argument("--op-timeout", type=float, default=120.0, help="seconds per platform operation")
    p.add_argument("--create-parallel", type=int, default=4, help="creates in flight at once")
    p.add_argument("--hold", type=float, default=0.0, help="seconds to hold each ramp stage")
    p.add_argument("--stop-on-failure", action="store_true")
    p.add_argument("--run-id", help="reuse a run id (to reconcile after an interrupted run)")
    p.add_argument("--out", help="write the JSON report here")
    args = p.parse_args(argv)
    report = asyncio.run(main_async(args))
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
    summary = {t["tier"]: {"passed": t["passed"], "instances": t["instances"], "nodes": t["nodes"]} for t in report["tiers"]}
    print(json.dumps({"run_id": report["run_id"], "passed": report["passed"], "left_over": report["left_over"],
                      "missing_nodes": report["missing_nodes"], "tiers": summary}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
