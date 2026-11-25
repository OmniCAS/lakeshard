"""A resident worker for the direct backend: registers, pulls module tasks, reports, heartbeats.

    lakeshard worker serve --coordinator http://HOST:PORT --id NAME --slots 3 --threads 1 \
        --workspace DIR [--token-file FILE]

`--slots` is how many `lean` processes run at once; `--threads` is `LEAN_NUM_THREADS` for each
(0 = Lean's default, all cores), unless the coordinator sets it for a task. The worker exits when
the coordinator says the build is finished, or when it cannot reach the coordinator for
`--give-up-s` seconds. The project, the toolchain and the dependencies' build outputs must be at
the same absolute paths as on the coordinator (see README, "Remote workers").
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

from .direct import ModuleBuilder
from .protocol import BuildConfig, ModuleTask, dumps


class CoordinatorUnreachable(Exception):
    pass


class PullWorker:
    def __init__(self, url: str, worker_id: str, slots: int, threads: int, workspace: str,
                 token: str | None = None, claim_wait_s: float = 20.0, give_up_s: float = 300.0):
        self.url = url.rstrip("/")
        self.id, self.slots, self.threads = worker_id, max(1, slots), threads
        self.workspace = os.path.abspath(workspace)
        self.token = token
        self.claim_wait_s, self.give_up_s = claim_wait_s, give_up_s
        self.cond = threading.Condition()
        self.running: set[str] = set()  # task ids not yet acknowledged by the coordinator
        self.stop = threading.Event()
        self.builder: ModuleBuilder | None = None
        self.done_tasks = 0

    def _post(self, path: str, body: dict, timeout: float = 60.0) -> dict:
        data = dumps(body).encode()
        t0 = time.time()
        delay = 0.2
        while True:
            req = urllib.request.Request(self.url + path, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            if self.token:
                req.add_header("Authorization", f"Bearer {self.token}")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code in (400, 401, 403, 404):
                    raise RuntimeError(f"coordinator refused {path}: {e.code} {e.read()[:300]!r}") from e
                err = e
            except (urllib.error.URLError, ConnectionError, TimeoutError, socket.timeout) as e:
                err = e
            if time.time() - t0 > self.give_up_s:
                raise CoordinatorUnreachable(f"{path}: {err}")
            time.sleep(delay)
            delay = min(5.0, delay * 2)

    def run(self) -> int:
        os.makedirs(self.workspace, exist_ok=True)
        resp = self._post("/v1/register", {"worker": self.id, "slots": self.slots, "threads": self.threads,
                                           "host": socket.gethostname(), "pid": os.getpid()})
        cfg = BuildConfig.from_dict(resp["config"])
        self.builder = ModuleBuilder(cfg, self.id, self.workspace, self.threads)
        hb = threading.Thread(target=self._heartbeats, args=(cfg.heartbeat_s,), daemon=True)
        hb.start()
        workers: list[threading.Thread] = []
        try:
            while not self.stop.is_set():
                with self.cond:
                    while len(self.running) >= self.slots and not self.stop.is_set():
                        self.cond.wait(1.0)
                    free = self.slots - len(self.running)
                if self.stop.is_set():
                    break
                resp = self._post("/v1/claim", {"worker": self.id, "n": free, "wait_s": self.claim_wait_s},
                                  timeout=self.claim_wait_s + 60)
                for td in resp.get("tasks", []):
                    task = ModuleTask.from_dict(td)
                    with self.cond:
                        self.running.add(task.task_id)
                    t = threading.Thread(target=self._run_task, args=(task,), daemon=True)
                    t.start()
                    workers.append(t)
                if resp.get("shutdown"):
                    break
        except CoordinatorUnreachable as e:
            print(f"worker {self.id}: giving up: {e}", file=sys.stderr)
            return 2
        finally:
            self.stop.set()
            for t in workers:
                t.join()
        return 0

    def _run_task(self, task: ModuleTask) -> None:
        res = self.builder.build(task)
        try:
            self._post("/v1/report", {"result": res})
        except Exception as e:  # noqa: BLE001
            print(f"worker {self.id}: could not report {task.task_id}: {e}", file=sys.stderr)
        with self.cond:
            self.running.discard(task.task_id)
            self.done_tasks += 1
            self.cond.notify_all()

    def _heartbeats(self, every_s: float) -> None:
        while not self.stop.wait(every_s):
            with self.cond:
                ids = sorted(self.running)
            try:
                resp = self._post("/v1/heartbeat", {"worker": self.id, "running": ids}, timeout=30)
            except Exception:  # noqa: BLE001 - the main loop notices an unreachable coordinator
                continue
            for tid in resp.get("cancel", []):
                self.builder.cancel(tid)
            if resp.get("shutdown"):
                with self.cond:
                    idle = not self.running
                if idle:
                    self.stop.set()
                    with self.cond:
                        self.cond.notify_all()


def read_token(path: str | None) -> str | None:
    if not path:
        return os.environ.get("LAKESHARD_TOKEN") or None
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip() or None
