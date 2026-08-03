import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

import server
from crawler import CrawlResult
from jobs import JobStore


client = TestClient(server.app)


def test_filter_off_topic_handles_empty_english_and_chinese_terms():
    assert server._filter_off_topic(["教育"], []) == []
    assert server._filter_off_topic(
        ["school"], ["travel", "academy"]
    ) == ["travel", "academy"]
    assert server._filter_off_topic(
        ["教育"], ["学校教育", "医疗服务"]
    ) == ["学校教育"]


def test_summary_helpers_filter_noise_and_build_sorted_entries():
    pages = [
        {"pageUrl": "", "hits": [{"keyword": "教育"}]},
        {"pageUrl": "https://x.test/no-hits", "hits": []},
        {
            "pageUrl": "https://x.test/short",
            "pageTitle": "短词",
            "hits": [{"keyword": "x", "snippet": "x"}],
        },
        {
            "pageUrl": "https://x.test/project.html",
            "pageTitle": "某政府采购网",
            "publishedDate": "2026-08-03",
            "hits": [
                {
                    "keyword": "教育采购",
                    "snippet": "教育设备采购项目公告",
                }
            ],
        },
    ]
    texts = {
        "https://x.test/project.html": (
            "首页\n咨询电话 010-12345678\n"
            "项目名称：某学校智慧教室设备采购项目。"
            "预算金额：120万元。计划采购时间：2026年9月"
        )
    }
    entries = server._build_entries_from_pages(pages, texts)
    assert len(entries) == 1
    assert entries[0]["date"] == "2026-08-03"
    assert entries[0]["link"] == "https://x.test/project.html"
    assert "120万元" in entries[0]["summary"]
    assert "咨询电话" not in entries[0]["summary"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/jobs/missing/toggle",
        "/api/jobs/missing/run",
    ],
)
def test_missing_job_post_endpoints_return_404(
    monkeypatch,
    tmp_path,
    path,
):
    monkeypatch.setattr(
        server,
        "job_store",
        JobStore(tmp_path / "jobs.json"),
    )
    response = client.post(path)
    assert response.status_code == 404
    assert response.json()["detail"] == "任务不存在"


def test_missing_job_delete_returns_404(monkeypatch, tmp_path):
    monkeypatch.setattr(
        server,
        "job_store",
        JobStore(tmp_path / "jobs.json"),
    )
    response = client.delete("/api/jobs/missing")
    assert response.status_code == 404


def test_locate_maps_http_and_transport_failures(monkeypatch):
    request = httpx.Request("GET", "https://x.test/page")
    response = httpx.Response(503, request=request)

    async def http_failure(*args, **kwargs):
        raise httpx.HTTPStatusError(
            "private",
            request=request,
            response=response,
        )

    monkeypatch.setattr(server, "fetch_html", http_failure)
    result = client.get(
        "/api/locate",
        params={"url": str(request.url), "keyword": "x"},
    )
    assert result.status_code == 502
    assert result.json()["detail"] == "目标页返回 HTTP 503"

    async def connect_failure(*args, **kwargs):
        raise httpx.ConnectError("private", request=request)

    monkeypatch.setattr(server, "fetch_html", connect_failure)
    result = client.get(
        "/api/locate",
        params={"url": str(request.url), "keyword": "x"},
    )
    assert result.status_code == 502
    assert result.json()["detail"] == "目标页无法访问：连接失败"


def test_archive_search_maps_failure_and_empty_result(monkeypatch):
    async def no_expand(*args, **kwargs):
        return []

    monkeypatch.setattr(server, "expand_keywords", no_expand)
    request = server.SearchRequest(
        startUrl="https://x.test/",
        keywords=["alpha"],
        render="archive",
    )

    async def fail_archive(*args, **kwargs):
        raise ValueError("archive failed")

    monkeypatch.setattr(server, "crawl_archive", fail_archive)
    with pytest.raises(server.HTTPException) as raised:
        asyncio.run(server.search(request))
    assert raised.value.status_code == 502
    assert raised.value.detail == "起始页无法访问：archive failed"

    async def empty_archive(*args, **kwargs):
        return CrawlResult()

    monkeypatch.setattr(server, "crawl_archive", empty_archive)
    with pytest.raises(server.HTTPException) as raised:
        asyncio.run(server.search(request))
    assert raised.value.status_code == 502
    assert raised.value.detail == "起始页无法访问，无法归档深扫"
