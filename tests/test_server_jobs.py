"""定时任务 API 测试（调度循环不启动，run_job mock）。"""
import pytest
from fastapi.testclient import TestClient

import jobs as jobs_mod
import server
from server import app

client = TestClient(app)


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = jobs_mod.JobStore(tmp_path / "jobs.json")
    monkeypatch.setattr(server, "job_store", s)
    return s


def _create(**over):
    payload = {
        "startUrl": "https://dct.yn.gov.cn/",
        "keywords": ["王润梅"], "depth": 1, "render": "auto",
        "schedule": {"kind": "daily", "time": "09:30"},
        "name": "人事任免监控",
    }
    payload.update(over)
    return client.post("/api/jobs", json=payload)


def test_create_list_toggle_delete(store):
    resp = _create()
    assert resp.status_code == 200
    job = resp.json()["job"]
    assert job["id"] and job["enabled"] is True

    lst = client.get("/api/jobs").json()["jobs"]
    assert len(lst) == 1
    assert lst[0]["scheduleText"] == "每天 09:30"
    assert lst[0]["nextRun"]

    t = client.post(f"/api/jobs/{job['id']}/toggle").json()
    assert t["enabled"] is False
    t = client.post(f"/api/jobs/{job['id']}/toggle").json()
    assert t["enabled"] is True

    assert client.delete(f"/api/jobs/{job['id']}").status_code == 200
    assert client.get("/api/jobs").json()["jobs"] == []
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 404


def test_create_job_validation(store):
    assert _create(schedule={"kind": "daily", "time": "25:00"}
                   ).status_code == 422
    assert _create(schedule={"kind": "interval", "hours": 0}
                   ).status_code == 422
    assert _create(startUrl="ftp://bad").status_code == 422
    assert _create(keywords=["  "]).status_code == 422
    assert _create(render="maybe").status_code == 422


def test_interval_schedule_text(store):
    resp = _create(schedule={"kind": "interval", "hours": 6})
    lst = client.get("/api/jobs").json()["jobs"]
    assert lst[0]["scheduleText"] == "每 6 小时"


def test_run_now(store, monkeypatch):
    job = _create().json()["job"]
    called = []

    async def fake_run(store_arg, job_id, **kw):
        called.append(job_id)
        return {"totalHits": 1}

    monkeypatch.setattr(server, "run_job", fake_run)
    resp = client.post(f"/api/jobs/{job['id']}/run")
    assert resp.status_code == 200
    assert resp.json()["started"] is True
    assert client.post("/api/jobs/nope/run").status_code == 404
