"""定时搜索任务：存储、到点判断、执行与新增命中检测（crawl/AI 全 mock）。"""
import asyncio
from datetime import datetime, timedelta

import pytest

import jobs
from crawler import CrawledPage, CrawlResult
from discovery.engine import DiscoveryRun
from discovery.models import DiscoveryStats


def _job(schedule, **kw):
    base = {
        "id": "j1", "name": "", "startUrl": "https://x.test/",
        "keywords": ["alpha"], "depth": 1, "render": "auto",
        "enabled": True, "createdAt": "2026-07-28T08:00:00",
        "lastRunAt": None, "prevKeys": [], "lastResult": None,
        "lastError": None, "running": False,
    }
    base["schedule"] = schedule
    base.update(kw)
    return base


NOW = datetime(2026, 7, 28, 10, 0, 0)


def test_validate_schedule_ok():
    assert jobs.validate_schedule({"kind": "daily", "time": "09:30"})
    assert jobs.validate_schedule({"kind": "interval", "hours": 6})


def test_validate_schedule_bad():
    for bad in ({"kind": "daily", "time": "9:70"},
                {"kind": "daily", "time": "25:00"},
                {"kind": "interval", "hours": 0},
                {"kind": "interval", "hours": 200},
                {"kind": "weekly"}):
        with pytest.raises(ValueError):
            jobs.validate_schedule(bad)


def test_daily_due_rules():
    sch = {"kind": "daily", "time": "09:30"}
    # 还没到 9:30
    assert not jobs.is_due(_job(sch), NOW.replace(hour=9, minute=0))
    # 过了 9:30 且从未运行 → 到期
    assert jobs.is_due(_job(sch), NOW)
    # 今天 9:40 已跑过 → 不到期
    assert not jobs.is_due(
        _job(sch, lastRunAt="2026-07-28T09:40:00"), NOW)
    # 昨天跑过 → 到期
    assert jobs.is_due(
        _job(sch, lastRunAt="2026-07-27T09:40:00"), NOW)
    # 停用 → 永不到期
    assert not jobs.is_due(_job(sch, enabled=False), NOW)


def test_interval_due_rules():
    sch = {"kind": "interval", "hours": 6}
    assert jobs.is_due(_job(sch), NOW)  # 从未运行立即到期
    assert not jobs.is_due(_job(sch, lastRunAt="2026-07-28T08:00:00"), NOW)
    assert jobs.is_due(_job(sch, lastRunAt="2026-07-28T03:59:00"), NOW)


def test_next_run_at_display():
    sch = {"kind": "daily", "time": "09:30"}
    assert jobs.next_run_at(_job(sch), NOW.replace(hour=8)) == \
        "2026-07-28T09:30:00"
    assert jobs.next_run_at(_job(sch), NOW) == "2026-07-28T10:00:00"  # 立即
    sch2 = {"kind": "interval", "hours": 6}
    assert jobs.next_run_at(
        _job(sch2, lastRunAt="2026-07-28T08:00:00"), NOW) == \
        "2026-07-28T14:00:00"


def test_hit_keys_diff():
    r1 = [{"pageUrl": "https://x/a", "pageTitle": "a",
           "hits": [{"kind": "text", "snippet": "alpha 一"}]}]
    r2 = [{"pageUrl": "https://x/b", "pageTitle": "b",
           "hits": [{"kind": "text", "snippet": "alpha 二"}]}]
    k1, k2 = jobs.hit_keys(r1), jobs.hit_keys(r2)
    assert len(k1) == 1 and k1 != k2
    assert jobs.hit_keys(r1) == k1  # 稳定


def test_store_roundtrip(tmp_path):
    store = jobs.JobStore(tmp_path / "jobs.json")
    job = _job({"kind": "daily", "time": "09:30"})
    store.add(job)
    store2 = jobs.JobStore(tmp_path / "jobs.json")
    assert store2.get("j1")["startUrl"] == "https://x.test/"
    store2.remove("j1")
    assert jobs.JobStore(tmp_path / "jobs.json").list() == []


async def _noop_discovery(*args, **kwargs):
    return DiscoveryRun(pages=[], failed=[], stats=DiscoveryStats())


def _fake_pages(html):
    return CrawlResult(pages=[CrawledPage(url="https://x.test/", html=html)])


def test_run_job_and_new_hits(tmp_path, monkeypatch):
    store = jobs.JobStore(tmp_path / "jobs.json")
    store.add(_job({"kind": "daily", "time": "09:30"}))

    pages = {"extra": False}

    async def fake_crawl(url, **kw):
        result = _fake_pages("<html><main>alpha 正文</main></html>")
        if pages["extra"]:
            result.pages.append(CrawledPage(
                url="https://x.test/new.html",
                html="<html><main>alpha 新正文</main></html>",
            ))
        return result

    async def fake_expand(kw, host):
        return []

    # 首次运行：建立基线，newHits 为 None
    r1 = asyncio.run(jobs.run_job(store, "j1",
                                  crawl_fn=fake_crawl, expand_fn=fake_expand,
                                  discovery_fn=_noop_discovery))
    assert r1["totalHits"] == 1 and r1["newHits"] is None
    job = store.get("j1")
    assert job["lastRunAt"] and len(job["prevKeys"]) == 1

    # 内容不变：newHits = 0
    r2 = asyncio.run(jobs.run_job(store, "j1",
                                  crawl_fn=fake_crawl, expand_fn=fake_expand,
                                  discovery_fn=_noop_discovery))
    assert r2["newHits"] == 0

    # 出现新命中：newHits = 1
    pages["extra"] = True
    r3 = asyncio.run(jobs.run_job(store, "j1",
                                  crawl_fn=fake_crawl, expand_fn=fake_expand,
                                  discovery_fn=_noop_discovery))
    assert r3["newHits"] == 1


def test_run_job_reuses_base_and_deduplicates_discovery_pages(tmp_path):
    store = jobs.JobStore(tmp_path / "jobs.json")
    store.add(_job(
        {"kind": "daily", "time": "09:30"},
        render="off",
    ))
    base = CrawlResult(pages=[
        CrawledPage(
            "https://x.test/old?utm_source=crawl",
            "<html><main>alpha base</main></html>",
        ),
    ])
    calls = {}

    async def fake_crawl(url, **kwargs):
        return base

    async def fake_expand(keywords, host):
        return []

    async def fake_discovery(
        url, keywords, base_result, depth, render_mode,
    ):
        calls["base"] = base_result
        calls["args"] = (url, keywords, depth, render_mode)
        return DiscoveryRun(
            pages=[
                CrawledPage(
                    "https://x.test/old?utm_campaign=discovery",
                    "<html><main>alpha duplicate</main></html>",
                ),
                CrawledPage(
                    "https://x.test/new.html",
                    "<html><main>alpha new</main></html>",
                ),
            ],
            failed=[{"url": "https://x.test/bad", "reason": "HTTP 500"}],
            stats=DiscoveryStats(),
        )

    result = asyncio.run(jobs.run_job(
        store,
        "j1",
        crawl_fn=fake_crawl,
        expand_fn=fake_expand,
        discovery_fn=fake_discovery,
    ))

    assert calls["base"] is base
    assert calls["args"] == (
        "https://x.test/", ["alpha"], 1, "off",
    )
    assert result["pagesCrawled"] == 2
    assert result["totalHits"] == 2
    assert base.failed == [
        {"url": "https://x.test/bad", "reason": "HTTP 500"},
    ]


def test_run_job_failure_records_error(tmp_path):
    store = jobs.JobStore(tmp_path / "jobs.json")
    store.add(_job({"kind": "daily", "time": "09:30"}))

    async def boom(url, **kw):
        raise ValueError("起始页无法访问：连接失败")

    async def fake_expand(kw, host):
        return []

    r = asyncio.run(jobs.run_job(store, "j1",
                                 crawl_fn=boom, expand_fn=fake_expand,
                                 discovery_fn=_noop_discovery))
    assert r["error"]
    assert "连接失败" in store.get("j1")["lastError"]


def test_run_job_auto_escalates(tmp_path):
    store = jobs.JobStore(tmp_path / "jobs.json")
    store.add(_job({"kind": "daily", "time": "09:30"}))
    calls = []

    async def fake_crawl(url, **kw):
        render = bool(kw.get("render", False))
        calls.append(render)
        html = ("<html><body>alpha 渲染命中</body></html>" if render
                else "<html><body>无命中</body></html>")
        return _fake_pages(html)

    async def fake_expand(kw, host):
        return []

    r = asyncio.run(jobs.run_job(store, "j1",
                                 crawl_fn=fake_crawl, expand_fn=fake_expand,
                                  discovery_fn=_noop_discovery))
    assert calls == [False, True]
    assert r["renderUsed"] is True
    assert r["totalHits"] == 1
