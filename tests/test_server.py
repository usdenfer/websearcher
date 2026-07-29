"""server.py 的接口测试：端到端搜索、未命中、参数校验、起始页失败。"""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

import search_budget
import server
from crawler import CrawledPage, CrawlResult
from discovery.engine import DiscoveryRun
from discovery.models import Candidate, DiscoveryStats
from server import app

client = TestClient(app)


def search(site_server, keywords, path="/index.html"):
    return client.post("/api/search", json={
        "startUrl": f"{site_server}{path}",
        "keywords": keywords,
    })


@pytest.mark.parametrize(
    "start_url",
    [
        "https://zycg.gov.cn/",
        "https://www.zycg.gov.cn/search",
        "https://www.zycg.gov.cn./",
        "https://www.zycg.gov.cn.:8443/search",
        "https://zycg.gov.cn。/",
        "https://zycg。gov。cn。/",
    ],
)
def test_search_budget_expands_for_zycg_hosts(start_url):
    started_at = time.monotonic()

    budget = server._search_budget(start_url, False, started_at)

    assert budget.initial_pages == 150
    assert budget.max_pages == 300
    assert budget.timeout_seconds == 300
    assert budget.started_at == started_at


def test_normalize_dns_hostname_handles_ascii_and_idna():
    assert search_budget._normalize_dns_hostname(
        "WWW.ZYCG.GOV.CN"
    ) == "www.zycg.gov.cn"
    assert search_budget._normalize_dns_hostname(
        "例子.中国"
    ) == "xn--fsqu00a.xn--fiqs8s"


@pytest.mark.parametrize(
    "start_url",
    [
        "https://example.com/",
        "https://evilzycg.gov.cn/",
        "https://zycg.gov.cn.evil.example/",
        "https://[broken",
        "https://foo..zycg.gov.cn/",
        "https://-bad.zycg.gov.cn/",
        "https://bad-.zycg.gov.cn/",
        "https://.zycg.gov.cn/",
        "https://www.zycg.gov.cn../",
        "https://www.zycg.gov.cn。。/",
        f"https://{'a' * 64}.zycg.gov.cn/",
        (
            "https://"
            + ".".join(["a" * 63] * 4)
            + ".zycg.gov.cn/"
        ),
        "https://bad_name.zycg.gov.cn/",
        "https://user:secret@www.zycg.gov.cn/",
        "https://www.zycg.gov.cn:99999/",
        "https://www.zycg.gov.cn:not-a-port/",
        "ftp://www.zycg.gov.cn/",
        "https://例子.中国/",
    ],
)
def test_search_budget_uses_generic_limits_for_other_or_invalid_hosts(
    start_url,
):
    started_at = time.monotonic()

    budget = server._search_budget(start_url, False, started_at)

    assert budget.initial_pages == 60
    assert budget.max_pages == 120
    assert budget.timeout_seconds == search_budget.SEARCH_BUDGET_SECONDS
    assert budget.started_at == started_at


def test_search_budget_keeps_archive_limits_for_zycg():
    started_at = time.monotonic()

    budget = server._search_budget(
        "https://zycg.gov.cn/", True, started_at
    )

    assert budget.initial_pages == search_budget.ARCHIVE_MAX_PAGES
    assert budget.max_pages == search_budget.ARCHIVE_MAX_PAGES
    assert (
        budget.timeout_seconds
        == search_budget.ARCHIVE_BUDGET_SECONDS
    )
    assert budget.started_at == started_at


def test_search_hit_structure(site_server):
    resp = search(site_server, ["alpha", "beta"])
    assert resp.status_code == 200
    data = resp.json()
    assert {
        "startUrl",
        "keywords",
        "expandedKeywords",
        "depth",
        "render",
        "renderMode",
        "autoNote",
        "siteSearch",
        "pagesCrawled",
        "crawledPages",
        "pagesFailed",
        "totalHits",
        "results",
        "searchId",
        "discovery",
    } <= data.keys()
    assert data["pagesCrawled"] == 3
    assert len(data["crawledPages"]) == 3
    assert data["pagesFailed"] == [
        {"url": f"{site_server}/missing.html", "reason": "HTTP 404"}]
    assert data["totalHits"] > 0
    page = next(r for r in data["results"]
                if r["pageUrl"].endswith("/index.html"))
    assert page["pageTitle"] == "Fixture Home"
    hit_kinds = {h["kind"] for h in page["hits"]}
    assert hit_kinds == {"text"}
    for hit in page["hits"]:
        assert set(hit) == {"kind", "snippet", "keyword", "href", "linkHref"}
    text_hit = next(h for h in page["hits"] if h["kind"] == "text")
    assert "#:~:text=" in text_hit["href"]


def test_search_follows_redirect_and_finds_relative_article_body(
        monkeypatch, redirect_site):
    async def fake_expand(keywords, host):
        return []

    async def no_discovery(*args, **kwargs):
        return DiscoveryRun(
            pages=[],
            failed=[],
            stats=DiscoveryStats(profile="generic"),
        )

    monkeypatch.setattr(server, "expand_keywords", fake_expand)
    monkeypatch.setattr(server, "discover_pages", no_discovery)
    response = client.post("/api/search", json={
        "startUrl": redirect_site["start"],
        "keywords": [redirect_site["keyword"]],
        "depth": 1,
        "render": "off",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["crawledPages"][:2] == [
        redirect_site["home"],
        redirect_site["article"],
    ]
    assert any(
        item["pageUrl"] == redirect_site["article"]
        for item in data["results"]
    )


def test_search_response_contains_discovery_diagnostics(site_server):
    resp = client.post("/api/search", json={
        "startUrl": f"{site_server}/index.html",
        "keywords": ["alpha"],
        "depth": 1,
        "render": "off",
    })
    assert resp.status_code == 200
    discovery = resp.json()["discovery"]
    assert discovery["profile"]
    assert isinstance(discovery["sourcesTried"], list)
    assert discovery["candidatesFound"] >= discovery["candidatesFetched"]
    assert discovery["elapsedMs"] >= 0
    assert isinstance(discovery["partial"], bool)


def test_search_matches_keyword_only_in_discovered_body(
        monkeypatch, site_server):
    async def fake_expand(keywords, host):
        return []

    async def fake_discover(*args, **kwargs):
        stats = DiscoveryStats(
            profile="generic",
            sources_tried={"sitemap"},
            sources_succeeded={"sitemap"},
            candidates_found=1,
            candidates_fetched=1,
        )
        return DiscoveryRun(
            pages=[CrawledPage(
                url=f"{site_server}/opaque-article.html",
                html=("<html><head><title>普通标题</title></head>"
                      "<body><main>正文中的 BODY-MARK-9184</main></body>"
                      "</html>"),
            )],
            failed=[],
            stats=stats,
        )

    monkeypatch.setattr(server, "expand_keywords", fake_expand)
    monkeypatch.setattr(server, "discover_pages", fake_discover,
                        raising=False)
    resp = client.post("/api/search", json={
        "startUrl": f"{site_server}/index.html",
        "keywords": ["BODY-MARK-9184"],
        "depth": 1,
        "render": "off",
    })
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert any(
        item["pageUrl"].endswith("/opaque-article.html")
        for item in results
    )


def test_freecms_search_exposes_strong_and_unfetched_weak_results(
        monkeypatch):
    async def fake_expand(keywords, host):
        return []

    async def fake_crawl(url, **kwargs):
        return CrawlResult(pages=[
            CrawledPage(url, "<title>正文页</title><main>alpha 正文</main>")
        ])

    async def fake_discover(*args, **kwargs):
        return DiscoveryRun(
            pages=[],
            failed=[],
            stats=DiscoveryStats(
                profile="freecms",
                candidates_found=1,
                candidates_fetched=0,
            ),
            candidates=[
                Candidate(
                    "https://freecms.test/strong",
                    "freecms-recent",
                    title_hint="alpha 强标题",
                ),
                Candidate(
                    "https://freecms.test/unfetched",
                    "freecms-recent",
                    title_hint="alpha 弱标题",
                ),
            ],
        )

    monkeypatch.setattr(server, "expand_keywords", fake_expand)
    monkeypatch.setattr(server, "crawl", fake_crawl)
    monkeypatch.setattr(server, "discover_pages", fake_discover)
    response = client.post("/api/search", json={
        "startUrl": "https://freecms.test/strong",
        "keywords": ["alpha"],
        "render": "off",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["totalHits"] == 1
    assert data["weakHits"] == 1
    assert [item["matchStrength"] for item in data["results"]] == [
        "strong", "weak",
    ]
    assert data["results"][1]["hits"][0]["kind"] == "title-recall"
    assert data["discovery"]["weakCandidates"] == 1
    assert data["pagesCrawled"] == 1
    assert data["crawledPages"] == ["https://freecms.test/strong"]


def test_freecms_search_can_return_only_weak_results(monkeypatch):
    async def fake_expand(keywords, host):
        return []

    async def fake_crawl(url, **kwargs):
        return CrawlResult(pages=[
            CrawledPage(url, "<main>正文没有命中</main>")
        ])

    async def fake_discover(*args, **kwargs):
        return DiscoveryRun(
            pages=[],
            failed=[],
            stats=DiscoveryStats(profile="freecms"),
            candidates=[
                Candidate(
                    "https://freecms.test/weak-only",
                    "freecms-recent",
                    title_hint="alpha 弱标题",
                ),
            ],
        )

    monkeypatch.setattr(server, "expand_keywords", fake_expand)
    monkeypatch.setattr(server, "crawl", fake_crawl)
    monkeypatch.setattr(server, "discover_pages", fake_discover)
    response = client.post("/api/search", json={
        "startUrl": "https://freecms.test/start",
        "keywords": ["alpha"],
        "render": "off",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["totalHits"] == 0
    assert data["weakHits"] == 1
    assert len(data["results"]) == 1
    assert data["results"][0]["matchStrength"] == "weak"


def test_generic_search_marks_results_strong_and_has_no_weak_hits(
        monkeypatch):
    async def fake_expand(keywords, host):
        return []

    async def fake_crawl(url, **kwargs):
        return CrawlResult(pages=[
            CrawledPage(url, "<main>alpha 正文</main>")
        ])

    async def fake_discover(*args, **kwargs):
        return DiscoveryRun(
            pages=[],
            failed=[],
            stats=DiscoveryStats(profile="generic"),
            candidates=[
                Candidate(
                    "https://generic.test/not-used",
                    "sitemap",
                    title_hint="alpha 标题",
                ),
            ],
        )

    monkeypatch.setattr(server, "expand_keywords", fake_expand)
    monkeypatch.setattr(server, "crawl", fake_crawl)
    monkeypatch.setattr(server, "discover_pages", fake_discover)
    response = client.post("/api/search", json={
        "startUrl": "https://generic.test/start",
        "keywords": ["alpha"],
        "render": "off",
    })

    assert response.status_code == 200
    data = response.json()
    assert data["weakHits"] == 0
    assert all(
        item["matchStrength"] == "strong"
        for item in data["results"]
    )


def test_api_reserves_budget_for_structured_discovery(
        monkeypatch, site_server):
    crawl_calls = []
    discovery_calls = []

    async def fake_expand(keywords, host):
        return []

    async def fake_crawl(url, **kwargs):
        crawl_calls.append(kwargs)
        return CrawlResult(pages=[
            CrawledPage(url=url, html="<html><main>alpha</main></html>")
        ])

    async def fake_discover(*args, **kwargs):
        discovery_calls.append((args, kwargs))
        return DiscoveryRun(
            pages=[],
            failed=[],
            stats=DiscoveryStats(profile="generic"),
        )

    monkeypatch.setattr(server, "expand_keywords", fake_expand)
    monkeypatch.setattr(server, "crawl", fake_crawl)
    monkeypatch.setattr(server, "discover_pages", fake_discover,
                        raising=False)
    before = time.monotonic()
    response = client.post("/api/search", json={
        "startUrl": f"{site_server}/index.html",
        "keywords": ["alpha"],
        "depth": 3,
        "render": "off",
    })
    assert response.status_code == 200
    data = response.json()
    assert data["pagesCrawled"] <= 120
    assert "discovery" in data
    assert crawl_calls[0]["max_pages"] == server.BASE_BFS_PAGE_BUDGET
    assert crawl_calls[0]["deadline"] >= before
    assert crawl_calls[0]["budget"].initial_pages == 60
    assert crawl_calls[0]["budget"].max_pages == 120
    args, kwargs = discovery_calls[0]
    assert args[0] == f"{site_server}/index.html"
    assert args[1] == ["alpha"]
    assert args[3:5] == (3, "off")
    assert kwargs["budget"] is crawl_calls[0]["budget"]


def test_api_shares_special_zycg_budget_without_expanding_base_bfs(
    monkeypatch,
):
    crawl_calls = []
    discovery_calls = []

    async def fake_expand(keywords, host):
        return []

    async def fake_crawl(url, **kwargs):
        crawl_calls.append(kwargs)
        return CrawlResult(pages=[
            CrawledPage(url=url, html="<html><main>alpha</main></html>")
        ])

    async def fake_discover(*args, **kwargs):
        discovery_calls.append((args, kwargs))
        return DiscoveryRun(
            pages=[],
            failed=[],
            stats=DiscoveryStats(profile="freecms"),
        )

    monkeypatch.setattr(server, "expand_keywords", fake_expand)
    monkeypatch.setattr(server, "crawl", fake_crawl)
    monkeypatch.setattr(server, "discover_pages", fake_discover)

    response = client.post("/api/search", json={
        "startUrl": "https://search.zycg.gov.cn/",
        "keywords": ["alpha"],
        "depth": 3,
        "render": "off",
    })

    assert response.status_code == 200
    crawl_budget = crawl_calls[0]["budget"]
    discovery_budget = discovery_calls[0][1]["budget"]
    assert crawl_budget is discovery_budget
    assert crawl_budget.initial_pages == 150
    assert crawl_budget.max_pages == 300
    assert crawl_budget.timeout_seconds == 300
    assert crawl_calls[0]["max_pages"] == server.BASE_BFS_PAGE_BUDGET


def test_discovery_failure_returns_base_body_results(
        monkeypatch, site_server):
    async def fake_expand(keywords, host):
        return []

    async def fake_crawl(url, **kwargs):
        return CrawlResult(pages=[
            CrawledPage(
                url=url,
                html="<html><main>alpha 基础正文</main></html>",
            )
        ])

    async def fake_discover(*args, **kwargs):
        raise RuntimeError("secret-token-must-not-leak")

    monkeypatch.setattr(server, "expand_keywords", fake_expand)
    monkeypatch.setattr(server, "crawl", fake_crawl)
    monkeypatch.setattr(server, "discover_pages", fake_discover)
    response = client.post("/api/search", json={
        "startUrl": f"{site_server}/index.html",
        "keywords": ["alpha"],
        "depth": 1,
        "render": "off",
    })
    assert response.status_code == 200
    data = response.json()
    assert data["totalHits"] == 1
    assert data["discovery"]["profile"] == "generic"
    assert data["discovery"]["partial"] is True
    assert data["discovery"]["elapsedMs"] >= 0
    assert data["discovery"]["warnings"] == [
        "discovery: RuntimeError"
    ]
    assert "secret-token" not in response.text
    assert data["siteSearch"] == {
        "available": False,
        "linksFound": 0,
        "pagesFetched": 0,
        "deprecated": True,
    }


def test_discovery_cancellation_propagates(monkeypatch):
    async def fake_expand(keywords, host):
        return []

    async def fake_crawl(url, **kwargs):
        return CrawlResult(pages=[
            CrawledPage(url=url, html="<html><main>alpha</main></html>")
        ])

    async def fake_discover(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(server, "expand_keywords", fake_expand)
    monkeypatch.setattr(server, "crawl", fake_crawl)
    monkeypatch.setattr(server, "discover_pages", fake_discover)
    request = server.SearchRequest(
        startUrl="https://cancel.test/",
        keywords=["alpha"],
        depth=1,
        render="off",
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(server.search(request))


def test_empty_start_page_result_returns_502(monkeypatch):
    async def fake_crawl(url, **kwargs):
        return CrawlResult(
            pages=[],
            failed=[{"url": url, "reason": "搜索截止时间已到"}],
        )

    monkeypatch.setattr(server, "crawl", fake_crawl)
    with pytest.raises(server.HTTPException) as raised:
        asyncio.run(server._crawl_or_502(
            "https://deadline.test/",
            depth=1,
            render=False,
            deadline=time.monotonic(),
        ))
    assert raised.value.status_code == 502
    assert "搜索截止时间已到" in raised.value.detail


def test_keyword_expansion_obeys_end_to_end_deadline(monkeypatch):
    cancelled = False

    async def slow_expand(keywords, host):
        nonlocal cancelled
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled = True
            raise

    async def fake_crawl(url, **kwargs):
        return CrawlResult(pages=[
            CrawledPage(url, "<main>alpha</main>")
        ])

    async def fake_discover(*args, **kwargs):
        return DiscoveryRun(
            pages=[],
            failed=[],
            stats=DiscoveryStats(),
        )

    monkeypatch.setattr(
        search_budget, "SEARCH_BUDGET_SECONDS", 0.02
    )
    monkeypatch.setattr(server, "expand_keywords", slow_expand)
    monkeypatch.setattr(server, "crawl", fake_crawl)
    monkeypatch.setattr(server, "discover_pages", fake_discover)
    request = server.SearchRequest(
        startUrl="https://deadline.test/",
        keywords=["alpha"],
        render="off",
    )

    response = asyncio.run(server.search(request))

    assert cancelled is True
    assert response["expandedKeywords"] == []


def test_search_no_hit(site_server):
    resp = search(site_server, ["absent-word"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["totalHits"] == 0
    assert data["results"] == []
    assert len(data["crawledPages"]) == 3


def test_search_keywords_as_string(site_server):
    resp = client.post("/api/search", json={
        "startUrl": f"{site_server}/index.html",
        "keywords": "alpha beta",
    })
    assert resp.status_code == 200
    assert resp.json()["keywords"] == ["alpha", "beta"]


def test_invalid_url_rejected(site_server):
    resp = client.post("/api/search", json={
        "startUrl": "ftp://bad", "keywords": ["x"]})
    assert resp.status_code == 422


def test_empty_keywords_rejected(site_server):
    resp = client.post("/api/search", json={
        "startUrl": f"{site_server}/index.html", "keywords": ["  "]})
    assert resp.status_code == 422


def test_start_page_failure_returns_502(site_server):
    resp = search(site_server, ["x"], path="/missing.html")
    assert resp.status_code == 502
    assert "404" in resp.json()["detail"]


def test_depth2_finds_deep_page(site_server):
    resp = client.post("/api/search", json={
        "startUrl": f"{site_server}/index.html",
        "keywords": ["omega"],
        "depth": 2,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["depth"] == 2
    assert data["totalHits"] >= 1
    assert any(r["pageUrl"].endswith("/deep.html")
               for r in data["results"])


def test_default_depth_is_1(site_server):
    resp = search(site_server, ["omega"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["depth"] == 1
    assert data["totalHits"] == 0


def test_invalid_depth_rejected(site_server):
    for bad in (0, 4):
        resp = client.post("/api/search", json={
            "startUrl": f"{site_server}/index.html",
            "keywords": ["x"], "depth": bad})
        assert resp.status_code == 422


def test_index_page_served():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "站内关键词搜索" in resp.text


def test_proactor_loop_factory_returns_loop_instance():
    """自定义 loop factory 会被 asyncio.Runner 零参数直接调用，必须返回
    事件循环实例（返回类会导致 reload 子进程启动失败）。"""
    import asyncio
    import sys

    from server import proactor_loop_factory
    loop = proactor_loop_factory()
    try:
        assert isinstance(loop, asyncio.AbstractEventLoop)
        if sys.platform == "win32":
            assert isinstance(loop, asyncio.ProactorEventLoop)
    finally:
        loop.close()
