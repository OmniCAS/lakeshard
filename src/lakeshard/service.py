"""The coordinator of the direct backend as a small HTTP service; workers pull tasks from it.

Why HTTP and not task files in the shared directory: the coordinator must decide, at the moment
a slot frees up, which ready module goes next (longest remaining path first), and every module of
a deep import chain waits for that decision. A long-polled HTTP request delivers it in
milliseconds and keeps all state in one process. Task files on NFS would need atomic claiming by
rename across clients and directory polling, and NFS attribute caching (typically 3-60 s) would
add that delay to every link of the chain (the simulations show 5-30 s per dependency doubling
the critical path of the Riemann-hypothesis archive). The service uses only the Python standard
library; the messages are the JSON of `lakeshard.protocol`. If sandboxes can reach the shared
directory but not each other over TCP, a file transport can implement the same four calls.

Endpoints (POST, JSON in and out; `Authorization: Bearer <token>` when a token is set):
- `/v1/register` `{worker, slots, threads, host, pid}` -> `{config: BuildConfig}`
- `/v1/claim` `{worker, n, wait_s}` -> `{tasks: [ModuleTask], shutdown}` (waits up to `wait_s`)
- `/v1/report` `{result: ModuleResult}` -> `{accepted}`; a successful result is accepted only
  after the coordinator sees the reported outputs (shared directory) or records (store)
- `/v1/heartbeat` `{worker, running: [task ids]}` -> `{cancel: [task ids], shutdown}`
- GET `/v1/status` -> counts
"""
from __future__ import annotations

import hmac
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .direct import wait_visible
from .project import ProjectGraph
from .protocol import BuildConfig, ModuleResult, dumps, to_dict
from .scheduler import ModuleScheduler, lower_bound
from .store import open_store


class BuildService:
    def __init__(self, graph: ProjectGraph, scheduler: ModuleScheduler, config: BuildConfig, work_dir: str,
                 token: str | None = None, host: str = "127.0.0.1", port: int = 0, claim_wait_cap_s: float = 30.0):
        self.g, self.s, self.cfg = graph, scheduler, config
        self.work = os.path.abspath(work_dir)
        os.makedirs(self.work, exist_ok=True)
        self.token = token
        self.cond = threading.Condition()
        self.t0 = time.time()
        self.t_done: float | None = None
        self.claim_wait_cap_s = claim_wait_cap_s
        self.events = open(os.path.join(self.work, "events.jsonl"), "a", encoding="utf-8")
        self.worker_info: dict[str, dict] = {}
        self.module_seconds: dict[str, float] = {}
        self.ok_results: dict[str, dict] = {}
        self.failures: list[dict] = []
        self.totals = {"attempts": 0, "failed_attempts": 0, "stale_reports": 0, "lean_s": 0.0, "cpu_s": 0.0,
                       "max_rss_bytes": 0, "wait_s": 0.0, "fetch_s": 0.0, "fetched_bytes": 0, "publish_s": 0.0,
                       "confirm_s": 0.0, "output_bytes": 0}
        self.store = open_store(config.store) if config.storage == "store" else None
        self._stop = threading.Event()
        svc = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # quiet
                pass

            def _reply(self, code: int, obj) -> None:
                data = json.dumps(to_dict(obj)).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self) -> bool:
                if not svc.token:
                    return True
                got = self.headers.get("Authorization", "")
                return hmac.compare_digest(got, f"Bearer {svc.token}")

            def do_GET(self):
                if not self._authorized():
                    return self._reply(401, {"error": "unauthorized"})
                if self.path == "/v1/status":
                    return self._reply(200, svc.status())
                return self._reply(404, {"error": "not found"})

            def do_POST(self):
                if not self._authorized():
                    return self._reply(401, {"error": "unauthorized"})
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                    fn = {"/v1/register": svc.register, "/v1/claim": svc.claim, "/v1/report": svc.report,
                          "/v1/heartbeat": svc.heartbeat}.get(self.path)
                    if fn is None:
                        return self._reply(404, {"error": "not found"})
                    return self._reply(200, fn(body))
                except Exception as e:  # noqa: BLE001
                    return self._reply(500, {"error": f"{type(e).__name__}: {e}"})

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self._threads: list[threading.Thread] = []

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        if host in ("0.0.0.0", "::"):
            host = socket.gethostname()
        return f"http://{host}:{port}"

    # --- lifecycle

    def start(self) -> None:
        self.t0 = time.time()
        self._log({"event": "start", "modules": len(self.g.modules), "storage": self.cfg.storage})
        for target in (self.httpd.serve_forever, self._housekeeping):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    def wait(self, timeout_s: float | None = None) -> bool:
        deadline = None if timeout_s is None else time.time() + timeout_s
        with self.cond:
            while not self.s.done():
                left = None if deadline is None else deadline - time.time()
                if left is not None and left <= 0:
                    return False
                self.cond.wait(timeout=1.0 if left is None else min(1.0, left))
        return True

    def stop(self, linger_s: float = 0.0) -> None:
        if linger_s:  # let workers see `shutdown` in their next claim or heartbeat
            time.sleep(linger_s)
        self._stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.events.close()

    def _housekeeping(self) -> None:
        while not self._stop.wait(1.0):
            with self.cond:
                evs = self.s.expire(time.time())
                for ev in evs:
                    self.totals["failed_attempts"] += 1
                    self.failures.append({k: ev.get(k) for k in ("module", "worker", "attempt", "reason")})
                    self._log({"event": "expired", **ev})
                if evs:
                    self._mark_done()
                    self.cond.notify_all()

    def _log(self, ev: dict) -> None:
        ev = {"t": round(time.time() - self.t0, 3), **ev}
        self.events.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self.events.flush()

    def _mark_done(self) -> None:
        if self.t_done is None and self.s.done():
            self.t_done = time.time()
            self._log({"event": "done", "counts": self.s.counts()})

    # --- endpoints

    def register(self, body: dict) -> dict:
        wid = body["worker"]
        with self.cond:
            self.s.register(wid, int(body.get("slots", 1)), time.time())
            self.worker_info[wid] = {"slots": int(body.get("slots", 1)), "threads": int(body.get("threads", 0)),
                                     "host": body.get("host", ""), "pid": body.get("pid"), "tasks": 0,
                                     "failed": 0, "lean_s": 0.0, "cpu_s": 0.0, "max_rss_bytes": 0}
            self._log({"event": "register", "worker": wid, "slots": body.get("slots"), "host": body.get("host")})
            self.cond.notify_all()
        return {"config": self.cfg}

    def claim(self, body: dict) -> dict:
        wid, n = body["worker"], max(0, int(body.get("n", 1)))
        deadline = time.time() + min(float(body.get("wait_s", 0.0)), self.claim_wait_cap_s)
        with self.cond:
            while True:
                if self.s.done():
                    return {"tasks": [], "shutdown": True}
                tasks = self.s.claim(wid, n, time.time()) if n else []
                if tasks:
                    for t in tasks:
                        self._log({"event": "dispatch", "task_id": t.task_id, "module": t.module, "worker": wid,
                                   "attempt": t.attempt, "threads": t.threads})
                    return {"tasks": tasks, "shutdown": False}
                left = deadline - time.time()
                if left <= 0:
                    return {"tasks": [], "shutdown": False}
                self.cond.wait(timeout=left)

    def heartbeat(self, body: dict) -> dict:
        with self.cond:
            cancel = self.s.heartbeat(body["worker"], list(body.get("running", [])), time.time())
            return {"cancel": cancel, "shutdown": self.s.done()}

    def _confirm(self, res: ModuleResult) -> str:
        """'' if the outputs are visible to the coordinator, else the reason."""
        if self.cfg.storage == "store":
            rec = self.store.get_records([res.module]).get(res.module)
            if rec is None or not any(f.path.endswith(".olean") for f in rec.files):
                return "no record with an .olean in the store"
            return ""
        paths = {os.path.join(self.cfg.build_dir, f.path): f.size for f in res.outputs}
        if not any(p.endswith(".olean") for p in paths):
            return "no .olean among the reported outputs"
        missing, waited = wait_visible(list(paths), self.cfg.visibility_timeout_s, self.cfg.visibility_poll_s,
                                       sizes=paths)
        if missing:
            return f"{len(missing)} outputs not visible to the coordinator after {waited:.1f} s, e.g. {missing[0]}"
        return ""

    def report(self, body: dict) -> dict:
        res = ModuleResult.from_dict(body["result"])
        with self.cond:  # a report is a sign of life; its task must not expire while we check outputs
            self.s.touch(res.worker, time.time())
            live = res.task_id in self.s.running and self.s.running[res.task_id].worker == res.worker
            if live:
                self.s.confirming.add(res.task_id)
        t = time.time()
        if res.ok and live:
            why = self._confirm(res)
            if why:
                res.ok, res.error = False, why
        confirm_s = time.time() - t
        with self.cond:
            self.s.confirming.discard(res.task_id)
            ev = self.s.complete(res.task_id, res.ok, time.time(), res.error) if live else None
            rec = {k: v for k, v in json.loads(dumps(res)).items() if k not in ("outputs", "log_tail")}
            rec["confirm_s"] = round(confirm_s, 4)
            rec["accepted"] = ev is not None
            if ev is not None:
                rec.update({k: ev[k] for k in ("action", "released", "skipped") if k in ev})
            if not res.ok:
                rec["log_tail"] = res.log_tail[-1500:]
            self._log({"event": "result", **rec})
            if ev is None:
                self.totals["stale_reports"] += 1
            else:
                self._account(res, confirm_s)
            self._mark_done()
            self.cond.notify_all()
        return {"accepted": ev is not None}

    def _account(self, res: ModuleResult, confirm_s: float) -> None:
        tt = self.totals
        tt["attempts"] += 1
        for k in ("lean_s", "cpu_s", "wait_s", "fetch_s", "fetched_bytes", "publish_s"):
            tt[k] += getattr(res, k)
        tt["confirm_s"] += confirm_s
        tt["max_rss_bytes"] = max(tt["max_rss_bytes"], res.max_rss_bytes)
        wi = self.worker_info.setdefault(res.worker, {"slots": 0, "threads": 0, "host": "", "pid": None, "tasks": 0,
                                                      "failed": 0, "lean_s": 0.0, "cpu_s": 0.0, "max_rss_bytes": 0})
        wi["tasks"] += 1
        wi["lean_s"] += res.lean_s
        wi["cpu_s"] += res.cpu_s
        wi["max_rss_bytes"] = max(wi["max_rss_bytes"], res.max_rss_bytes)
        if res.ok:
            self.module_seconds[res.module] = round(res.lean_s, 3)
            tt["output_bytes"] += sum(f.size for f in res.outputs)
            self.ok_results[res.module] = {"worker": res.worker, "start": round(res.started - self.t0, 3),
                                           "lean_s": round(res.lean_s, 3), "wall_s": round(res.wall_s, 3)}
        else:
            tt["failed_attempts"] += 1
            wi["failed"] += 1
            self.failures.append({"module": res.module, "worker": res.worker, "attempt": res.attempt,
                                  "error": res.error[:500]})

    # --- results

    def status(self) -> dict:
        with self.cond:
            return {"counts": self.s.counts(), "running": len(self.s.running), "workers": len(self.s.workers),
                    "elapsed_s": round(time.time() - self.t0, 1), "done": self.s.done()}

    def report_dict(self) -> dict:
        with self.cond:
            end = self.t_done or time.time()
            wall = end - self.t0
            counts = self.s.counts()
            slots = sum(w["slots"] for w in self.worker_info.values())
            lb = lower_bound(self.g, self.module_seconds, slots)
            lb["ratio"] = round(wall / lb["bound_s"], 3) if lb["bound_s"] else None
            tt = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in self.totals.items()}
            return {"ok": counts.get("done", 0) == len(self.g.modules), "backend": "lean",
                    "storage": self.cfg.storage, "build_dir": self.cfg.build_dir, "store": self.cfg.store,
                    "wall_s": round(wall, 2), "modules": len(self.g.modules), "status": counts,
                    "modules_built": counts.get("done", 0), "slots": slots, "lower_bound": lb, "totals": tt,
                    "workers": self.worker_info, "failures": self.failures[:100],
                    "module_seconds": dict(self.module_seconds)}
