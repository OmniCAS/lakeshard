"""Tests for infra/scripts/stability.py with the in-memory backend.

Run: python -m pytest infra/tests
"""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "stability.py"
spec = importlib.util.spec_from_file_location("stability", SCRIPT)
stability = importlib.util.module_from_spec(spec)
sys.modules["stability"] = stability
spec.loader.exec_module(stability)

POOL = "worker-1,worker-2,worker-3"


def run(tmp_path, *tiers, config=None, extra=()):
    cfg = tmp_path / "fake.json"
    cfg.write_text(json.dumps(config or {}))
    out = tmp_path / "report.json"
    argv = ["--backend", "fake", "--backend-config", str(cfg), "--allowed-nodes", POOL,
            "--op-timeout", "5", "--out", str(out), *extra]
    for t in tiers:
        argv += ["--tier", t]
    code = stability.main(argv)
    return code, json.loads(out.read_text())


def test_all_tiers_pass_and_leave_nothing(tmp_path):
    code, report = run(tmp_path, "serial:3", "ramp:1,2,4,8", "repeat:4x2", "soak:3:0.3:0.1",
                       extra=["--require-all-nodes"])
    assert code == 0 and report["passed"]
    assert report["left_over"] == []
    tiers = {t["tier"]: t for t in report["tiers"]}
    assert tiers["serial:3"]["instances"] == 3
    assert tiers["ramp:1,2,4,8"]["instances"] == 8
    assert [s["live"] for s in tiers["ramp:1,2,4,8"]["stages"]] == [1, 2, 4, 8]
    assert tiers["repeat:4x2"]["instances"] == 8
    assert set(tiers["ramp:1,2,4,8"]["nodes"]) == {"worker-1", "worker-2", "worker-3"}
    assert report["missing_nodes"] == []
    soak = tiers["soak:3:0.3:0.1"]["records"]
    assert all(r["checks"] >= 2 for r in soak)


def test_instance_outside_pool_fails_but_is_cleaned_up(tmp_path):
    code, report = run(tmp_path, "ramp:1,3", config={"off_pool": [1]})
    assert code == 1 and not report["passed"]
    assert report["left_over"] == []
    bad = [r for r in report["tiers"][0]["records"] if r["errors"]]
    assert len(bad) == 1 and bad[0]["node"] == "outside" and bad[0]["absent"] is True


def test_failed_create_is_reported(tmp_path):
    code, report = run(tmp_path, "serial:3", config={"fail_create": [1]})
    assert code == 1
    recs = report["tiers"][0]["records"]
    assert recs[1]["id"] is None and recs[1]["errors"][0].startswith("create: RuntimeError")
    assert recs[0]["absent"] is True and recs[2]["absent"] is True
    assert report["left_over"] == []


def test_leak_is_detected(tmp_path):
    code, report = run(tmp_path, "repeat:2x1", config={"leak": [0]})
    assert code == 1
    assert len(report["left_over"]) == 1
    assert any("still present" in e for r in report["tiers"][0]["records"] for e in r["errors"])


def test_create_is_idempotent_per_run_and_index():
    backend = stability.FakeBackend({})

    async def twice():
        a = await backend.create("run", 0, 60)
        b = await backend.create("run", 0, 60)
        return a, b

    a, b = asyncio.run(twice())
    assert a.id == b.id


@pytest.mark.parametrize("bad", ["ramp:4,2", "repeat:3", "soak:1:2", "nope:1"])
def test_bad_tier_text_is_rejected(bad):
    with pytest.raises(SystemExit):
        stability.parse_tier(bad)


def test_missing_node_fails_the_run_not_the_tier(tmp_path):
    code, report = run(tmp_path, "serial:2", config={"nodes": ["worker-1", "worker-2"]}, extra=["--require-all-nodes"])
    assert code == 1 and report["tiers"][0]["passed"]
    assert report["missing_nodes"] == ["worker-3"]
