# 泛用化站点正文关键词发现实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 将当前固定绑定少数政府 CMS 的站内搜索补充逻辑，改造成“通用自动探测 + 可选 CMS 适配器”的正文关键词发现系统；以 Gamersky 作为无专用适配器的通用能力实验站，并首批保留中央政府采购网和云南政府站 CMS 适配。

**架构：** 保留现有 `crawler.py` 作为基础 BFS 与底层 HTTP 抓取能力，新建独立 `discovery` 包，负责站点范围、搜索入口、sitemap、Feed、栏目分页、适配器、候选排序和统一预算。所有发现结果都只是候选 URL，最终必须抓取目标页面并由正文匹配器确认关键词。

**技术栈：** Python 3.12、FastAPI、httpx、BeautifulSoup4、Playwright、pytest、原生 HTML/CSS/JavaScript。

**设计依据：** `docs/superpowers/specs/2026-07-28-generalized-site-discovery-design.md`

**版本控制说明：** 当前仓库尚无首次提交，且用户要求暂不提交。本计划列出每个阶段建议的提交命令，但执行时默认跳过；只有用户明确授权后才运行 `git add` 和 `git commit`。

**预算口径：** 起始页、BFS 页、搜索结果 HTML、栏目列表 HTML、Playwright
渲染页和目标正文页共享最多 120 个 HTML 页面请求；API、sitemap 和 Feed
使用各 Provider 的独立小额请求配额。单次搜索总时间不超过 120 秒。

---

## 文件结构

新增文件：

```text
discovery/
  __init__.py        对外导出 discover_pages 和公共数据类型
  models.py          Candidate、SearchSpec、DomainPolicy、DiscoveryStats、BudgetManager
  urltools.py        URL 归一化、语义参数保留、站点范围判断
  parsers.py         搜索表单、搜索结果、sitemap、Feed、分页的纯解析函数
  adapters.py        SiteAdapter 协议及 FreeCMS、云南 CMS 适配器
  fetcher.py         受预算控制的静态/渲染抓取
  providers.py       通用发现 Provider
  engine.py          Provider 调度、候选排序、去重、正文抓取和诊断汇总
tests/
  test_discovery_models.py
  test_discovery_urltools.py
  test_discovery_parsers.py
  test_discovery_adapters.py
  test_discovery_fetcher.py
  test_discovery_engine.py
  test_discovery_integration.py
  fixtures/discovery/
    generic_search.html
    generic_results.html
    sitemap.xml
    feed.xml
    portal_search_home.html
    portal_search_results.html
    freecms_home.html
    freecms_search.html
    freecms_failure.json
scripts/
  smoke_discovery.py  接收 URL 和关键词的手动线上冒烟工具
```

修改文件：

```text
crawler.py            复用统一 URL 归一化和站点范围规则
matcher.py            新增正文容器提取和正文专用匹配
sitesearch.py         保留旧 API 的兼容层，内部转发至 discovery
server.py             接入统一发现引擎并返回 discovery 诊断字段
jobs.py               定时任务复用统一发现引擎
static/index.html     展示发现来源、部分结果和预算提示
README.md             更新架构、运行方式和能力边界
```

---

### 任务 1：建立公共数据模型与统一预算

**文件：**

- 创建：`discovery/__init__.py`
- 创建：`discovery/models.py`
- 创建：`tests/test_discovery_models.py`

- [ ] **步骤 1：编写失败测试**

在 `tests/test_discovery_models.py` 写入：

```python
from discovery.models import (
    BudgetManager, Candidate, DiscoveryStats, DomainPolicy, SearchSpec,
)


def test_candidate_identity_ignores_source_metadata():
    a = Candidate("https://x.test/a.html", "site-search", keyword="alpha")
    b = Candidate("https://x.test/a.html", "sitemap", keyword="beta")
    assert a.url == b.url


def test_domain_policy_allows_only_declared_hosts():
    policy = DomainPolicy(
        root_host="www.example.test",
        allowed_hosts=frozenset({"www.example.test", "search.example.test"}),
        excluded_hosts=frozenset({"ads.example.test"}),
    )
    assert policy.allows("www.example.test")
    assert policy.allows("search.example.test")
    assert not policy.allows("ads.example.test")
    assert not policy.allows("example.com")


def test_budget_expands_only_for_high_value_candidates():
    budget = BudgetManager(initial_pages=60, max_pages=120, timeout_seconds=120)
    budget.used_html_pages = 60
    assert not budget.reserve_html()
    assert budget.expand(high_value_remaining=1)
    assert budget.page_limit == 120
    assert budget.reserve_html()
    assert budget.used_html_pages == 61


def test_stats_serializes_api_shape():
    stats = DiscoveryStats(profile="generic")
    stats.sources_tried.add("site-search")
    stats.sources_succeeded.add("site-search")
    data = stats.as_dict()
    assert data["profile"] == "generic"
    assert data["sourcesTried"] == ["site-search"]
    assert data["sourcesSucceeded"] == ["site-search"]
    assert data["partial"] is False


def test_search_spec_builds_query_parameters():
    spec = SearchSpec(
        source="site-search",
        url="https://search.example.test/",
        query_param="s",
    )
    assert spec.params_for("黑神话", page=2) == {"s": "黑神话"}
```

- [ ] **步骤 2：运行测试并确认失败**

运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_discovery_models.py -q
```

预期：收集阶段失败，提示 `ModuleNotFoundError: No module named 'discovery'`。

- [ ] **步骤 3：实现数据模型**

在 `discovery/models.py` 实现：

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Candidate:
    url: str
    source: str
    keyword: str = ""
    title_hint: str = ""
    score: int = 0
    requires_render: bool = False
    section: str = ""


@dataclass(frozen=True)
class SearchSpec:
    source: str
    url: str
    query_param: str
    page_param: str | None = None
    fixed_params: tuple[tuple[str, str], ...] = ()
    result_selector: str = "a[href]"

    def params_for(self, keyword: str, page: int = 1) -> dict[str, str | int]:
        params: dict[str, str | int] = dict(self.fixed_params)
        params[self.query_param] = keyword
        if self.page_param:
            params[self.page_param] = page
        return params


@dataclass(frozen=True)
class DomainPolicy:
    root_host: str
    allowed_hosts: frozenset[str]
    excluded_hosts: frozenset[str] = frozenset()
    allow_related_hosts: bool = False

    def allows(self, host: str) -> bool:
        host = host.lower()
        return host in self.allowed_hosts and host not in self.excluded_hosts


@dataclass
class DiscoveryStats:
    profile: str = "generic"
    sources_tried: set[str] = field(default_factory=set)
    sources_succeeded: set[str] = field(default_factory=set)
    candidates_found: int = 0
    candidates_fetched: int = 0
    rendered_pages: int = 0
    budget_expanded: bool = False
    partial: bool = False
    elapsed_ms: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "profile": self.profile,
            "sourcesTried": sorted(self.sources_tried),
            "sourcesSucceeded": sorted(self.sources_succeeded),
            "candidatesFound": self.candidates_found,
            "candidatesFetched": self.candidates_fetched,
            "renderedPages": self.rendered_pages,
            "budgetExpanded": self.budget_expanded,
            "partial": self.partial,
            "elapsedMs": self.elapsed_ms,
            "warnings": list(self.warnings),
        }


@dataclass
class BudgetManager:
    initial_pages: int = 60
    max_pages: int = 120
    timeout_seconds: float = 120.0
    used_html_pages: int = 0
    page_limit: int = field(init=False)
    started_at: float = field(default_factory=time.monotonic)
    provider_requests: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.page_limit = self.initial_pages

    def expired(self) -> bool:
        return time.monotonic() - self.started_at >= self.timeout_seconds

    def reserve_html(self) -> bool:
        if self.expired() or self.used_html_pages >= self.page_limit:
            return False
        self.used_html_pages += 1
        return True

    def reserve_provider(self, source: str, limit: int) -> bool:
        used = self.provider_requests.get(source, 0)
        if self.expired() or used >= limit:
            return False
        self.provider_requests[source] = used + 1
        return True

    def expand(self, high_value_remaining: int) -> bool:
        if high_value_remaining <= 0 or self.page_limit >= self.max_pages:
            return False
        self.page_limit = self.max_pages
        return True
```

在 `discovery/__init__.py` 暂时导出公共模型：

```python
from discovery.models import (
    BudgetManager, Candidate, DiscoveryStats, DomainPolicy, SearchSpec,
)

__all__ = [
    "BudgetManager", "Candidate", "DiscoveryStats", "DomainPolicy",
    "SearchSpec",
]
```

- [ ] **步骤 4：运行测试并确认通过**

运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_discovery_models.py -q
```

预期：`5 passed`。

- [ ] **步骤 5：记录检查点**

运行：

```powershell
git status --short
```

确认只出现本任务文件。若用户之后授权提交，建议命令：

```powershell
git add discovery/__init__.py discovery/models.py tests/test_discovery_models.py
git commit -m "feat: add discovery models and budget"
```

---

### 任务 2：实现 URL 归一化与站点范围

**文件：**

- 创建：`discovery/urltools.py`
- 创建：`tests/test_discovery_urltools.py`
- 修改：`crawler.py:34-58`

- [ ] **步骤 1：编写失败测试**

```python
from discovery.models import DomainPolicy
from discovery.urltools import (
    canonical_url, extend_policy_with_declared_urls, is_html_candidate,
    normalize_candidate_url, registrable_domain, url_allowed,
)


def test_normalize_drops_tracking_but_keeps_semantic_parameters():
    url = (
        "HTTPS://WWW.EXAMPLE.COM/a/?utm_source=x&id=7&page=2"
        "&spm=ad#section"
    )
    assert normalize_candidate_url(url) == (
        "https://www.example.com/a?id=7&page=2"
    )


def test_normalize_sorts_query_for_stable_deduplication():
    assert normalize_candidate_url("https://x.test/a?b=2&id=1") == (
        "https://x.test/a?b=2&id=1"
    )
    assert normalize_candidate_url("https://x.test/a?id=1&b=2") == (
        "https://x.test/a?b=2&id=1"
    )


def test_url_allowed_uses_explicit_content_hosts():
    policy = DomainPolicy(
        "www.example.test",
        frozenset({"www.example.test", "content.example.test"}),
    )
    assert url_allowed("https://content.example.test/2024/x/", policy)
    assert not url_allowed("https://ads.example.test/x", policy)
    assert not url_allowed("javascript:void(0)", policy)


def test_binary_and_auth_pages_are_not_html_candidates():
    assert not is_html_candidate("https://x.test/a.pdf")
    assert not is_html_candidate("https://x.test/login")
    assert is_html_candidate("https://x.test/news/123.shtml?id=7")


def test_canonical_is_used_only_inside_domain_policy():
    policy = DomainPolicy("x.test", frozenset({"x.test"}))
    assert canonical_url(
        '<link rel="canonical" href="/clean.html?id=7">',
        "https://x.test/a?utm_source=x",
        policy,
    ) == "https://x.test/clean.html?id=7"
    assert canonical_url(
        '<link rel="canonical" href="https://outside.test/a">',
        "https://x.test/original.html",
        policy,
    ) == "https://x.test/original.html"


def test_declared_search_subdomain_can_extend_generic_policy():
    policy = DomainPolicy("www.example.co.uk", frozenset({"www.example.co.uk"}))
    extended = extend_policy_with_declared_urls(
        policy, ["https://search.example.co.uk/find?q=x"])
    assert extended.allows("search.example.co.uk")
    assert extended.allow_related_hosts is True
    assert url_allowed("https://content.example.co.uk/article.html", extended)
    assert not url_allowed("https://ads.example.co.uk/banner.html", extended)
    assert not extended.allows("attacker.co.uk")
    assert registrable_domain("www.example.co.uk") == "example.co.uk"
```

- [ ] **步骤 2：运行测试并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_discovery_urltools.py -q
```

预期：失败并提示 `discovery.urltools` 不存在。

- [ ] **步骤 3：实现 URL 工具**

在 `discovery/urltools.py` 实现：

```python
from __future__ import annotations

import re
from urllib.parse import (
    parse_qsl, urlencode, urldefrag, urljoin, urlsplit, urlunsplit,
)

from bs4 import BeautifulSoup

from discovery.models import DomainPolicy

TRACKING_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term",
    "utm_content", "spm", "from", "source",
}
MULTIPART_SUFFIXES = {
    "com.cn", "net.cn", "org.cn", "gov.cn",
    "co.uk", "org.uk", "com.au", "co.jp",
}
BINARY_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".pdf",
    ".zip", ".rar", ".7z", ".mp4", ".mp3", ".css", ".js",
}
AUTH_SEGMENTS = {"/login", "/signin", "/register", "/passport", "/user/"}
EXCLUDED_HOST_LABELS = {
    "ad", "ads", "advert", "login", "passport", "account",
    "shop", "mall", "pay", "cps", "tracking",
}


def normalize_candidate_url(url: str) -> str:
    clean, _fragment = urldefrag(url.strip())
    parts = urlsplit(clean)
    scheme = parts.scheme.lower()
    host = parts.netloc.lower()
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_KEYS
    ]
    query.sort()
    return urlunsplit((scheme, host, path, urlencode(query, doseq=True), ""))


def is_html_candidate(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return False
    path = parts.path.lower()
    if any(path.endswith(suffix) for suffix in BINARY_SUFFIXES):
        return False
    return not any(segment in path for segment in AUTH_SEGMENTS)


def url_allowed(url: str, policy: DomainPolicy) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not is_html_candidate(url) or not host:
        return False
    if policy.allows(host):
        return True
    labels = set(re.split(r"[-.]", host))
    return (
        policy.allow_related_hosts
        and not labels.intersection(EXCLUDED_HOST_LABELS)
        and registrable_domain(host) == registrable_domain(policy.root_host)
    )


def registrable_domain(host: str) -> str:
    labels = host.lower().strip(".").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    suffix = ".".join(labels[-2:])
    take = 3 if suffix in MULTIPART_SUFFIXES else 2
    return ".".join(labels[-take:])


def extend_policy_with_declared_urls(
    policy: DomainPolicy, urls: list[str],
) -> DomainPolicy:
    root_domain = registrable_domain(policy.root_host)
    allowed = set(policy.allowed_hosts)
    for url in urls:
        host = (urlsplit(url).hostname or "").lower()
        if host and registrable_domain(host) == root_domain:
            allowed.add(host)
    return DomainPolicy(
        policy.root_host,
        frozenset(allowed),
        policy.excluded_hosts,
        allow_related_hosts=True,
    )


def canonical_url(
    html: str, page_url: str, policy: DomainPolicy,
) -> str:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.find("link", rel=lambda value: value and "canonical" in value)
    original = normalize_candidate_url(page_url)
    if not node or not node.get("href"):
        return original
    candidate = normalize_candidate_url(
        urljoin(page_url, str(node["href"])))
    return candidate if url_allowed(candidate, policy) else original
```

将 `crawler.normalize_url` 改为薄封装，保持现有调用兼容：

```python
from discovery.urltools import (
    extend_policy_with_declared_urls, normalize_candidate_url,
)


def normalize_url(url: str) -> str:
    return normalize_candidate_url(url)
```

- [ ] **步骤 4：运行新旧 URL 测试**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_discovery_urltools.py tests/test_crawler.py -q
```

预期：全部通过。若旧测试对查询参数顺序有断言，应只更新预期顺序，不改变语义参数。

- [ ] **步骤 5：记录检查点**

```powershell
git diff -- discovery/urltools.py crawler.py tests/test_discovery_urltools.py
```

授权后建议提交信息：`feat: add semantic URL normalization and domain policy`。

---

### 任务 3：实现通用搜索、sitemap、Feed 与分页解析

**文件：**

- 创建：`discovery/parsers.py`
- 创建：`tests/test_discovery_parsers.py`
- 创建：`tests/fixtures/discovery/generic_search.html`
- 创建：`tests/fixtures/discovery/generic_results.html`
- 创建：`tests/fixtures/discovery/sitemap.xml`
- 创建：`tests/fixtures/discovery/feed.xml`

- [ ] **步骤 1：创建解析 fixture**

`generic_search.html`：

```html
<html><body>
  <link rel="alternate" type="application/rss+xml" href="/feed.xml">
  <form action="/search" method="get">
    <input name="keyword" placeholder="请输入关键字">
    <button type="submit">搜索</button>
  </form>
  <script>
    function runSearch(value) {
      location.href = "/find?query=" + encodeURIComponent(value);
    }
  </script>
  <a href="/news/index.html">新闻资讯</a>
</body></html>
```

`generic_results.html`：

```html
<html><body>
  <main>
    <article><a href="/news/1.html">普通标题</a></article>
    <article><a href="/news/2.html">正文标记对应页面</a></article>
  </main>
  <nav class="pagination">
    <a href="?keyword=alpha&page=2">下一页</a>
  </nav>
</body></html>
```

`sitemap.xml`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.test/news/1.html</loc></url>
  <url><loc>https://example.test/news/2.html</loc></url>
</urlset>
```

`feed.xml`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item><title>条目一</title><link>https://example.test/news/3.html</link></item>
</channel></rss>
```

- [ ] **步骤 2：编写失败测试**

```python
from pathlib import Path

from discovery.models import DomainPolicy
from discovery.parsers import (
    detect_category_urls, detect_feed_urls, detect_search_specs, parse_feed,
    parse_pagination, parse_result_candidates, parse_sitemap,
    parse_sitemap_index,
)

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"
POLICY = DomainPolicy("example.test", frozenset({"example.test"}))


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_detects_get_search_form():
    specs = detect_search_specs(
        fixture("generic_search.html"), "https://example.test/")
    assert specs[0].url == "https://example.test/search"
    assert specs[0].query_param == "keyword"
    assert any(
        spec.url == "https://example.test/find"
        and spec.query_param == "query"
        for spec in specs
    )
    assert detect_feed_urls(
        fixture("generic_search.html"), "https://example.test/"
    ) == ["https://example.test/feed.xml"]
    assert detect_category_urls(
        fixture("generic_search.html"),
        "https://example.test/",
        POLICY,
    ) == ["https://example.test/news/index.html"]


def test_parses_results_and_pagination():
    html = fixture("generic_results.html")
    candidates = parse_result_candidates(
        html, "https://example.test/search?keyword=alpha", POLICY,
        source="site-search", keyword="alpha",
    )
    assert [c.url for c in candidates] == [
        "https://example.test/news/1.html",
        "https://example.test/news/2.html",
    ]
    assert parse_pagination(
        html, "https://example.test/search?keyword=alpha", POLICY
    ) == ["https://example.test/search?keyword=alpha&page=2"]


def test_parses_sitemap_and_feed():
    assert parse_sitemap(fixture("sitemap.xml"), POLICY) == [
        "https://example.test/news/1.html",
        "https://example.test/news/2.html",
    ]
    feed = parse_feed(fixture("feed.xml"), POLICY)
    assert [(item.url, item.title_hint) for item in feed] == [
        ("https://example.test/news/3.html", "条目一")
    ]
    index = """<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://example.test/sitemap-news.xml</loc></sitemap>
    </sitemapindex>"""
    assert parse_sitemap_index(index, POLICY) == [
        "https://example.test/sitemap-news.xml"
    ]
```

- [ ] **步骤 3：运行测试并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_discovery_parsers.py -q
```

预期：失败并提示 `discovery.parsers` 不存在。

- [ ] **步骤 4：实现纯解析函数**

`discovery/parsers.py` 的公共签名和核心实现：

```python
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from discovery.models import Candidate, DomainPolicy, SearchSpec
from discovery.urltools import normalize_candidate_url, url_allowed

SEARCH_NAMES = {
    "q", "s", "key", "keyword", "keywords", "query", "searchcontent",
}
SEARCH_WORDS = ("搜索", "检索", "search")
CATEGORY_WORDS = (
    "新闻", "资讯", "公告", "采购", "攻略", "法规", "信息公开",
    "news", "article", "notice", "guide",
)
SCRIPT_SEARCH_RE = re.compile(
    r"""(?P<quote>['"])(?P<url>[^'"]*(?:search|find|query)[^'"]*)"""
    r"""\?(?P<param>q|s|key|keyword|keywords|query|searchContent)=""",
    re.IGNORECASE,
)


def detect_search_specs(html: str, base_url: str) -> list[SearchSpec]:
    soup = BeautifulSoup(html, "html.parser")
    specs: list[SearchSpec] = []
    for form in soup.find_all("form"):
        method = (form.get("method") or "get").lower()
        if method != "get":
            continue
        action = urljoin(base_url, form.get("action") or base_url)
        inputs = form.find_all("input", attrs={"name": True})
        query = next(
            (item["name"] for item in inputs
             if item["name"].lower() in SEARCH_NAMES),
            None,
        )
        form_text = form.get_text(" ", strip=True).lower()
        if query and (
            any(word in form_text for word in SEARCH_WORDS)
            or any(word in str(form).lower() for word in SEARCH_WORDS)
        ):
            specs.append(SearchSpec("site-search", action, query))
    for node in soup.select("[data-action]"):
        query_input = node.find("input", attrs={"name": True})
        if query_input and query_input["name"].lower() in SEARCH_NAMES:
            specs.append(SearchSpec(
                "site-search",
                urljoin(base_url, node["data-action"]),
                query_input["name"],
            ))
    for match in SCRIPT_SEARCH_RE.finditer(html):
        specs.append(SearchSpec(
            "site-search",
            urljoin(base_url, match.group("url")),
            match.group("param"),
        ))
    unique = {
        (spec.url, spec.query_param, spec.page_param): spec for spec in specs
    }
    return list(unique.values())


def detect_feed_urls(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    result: list[str] = []
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel", [])).lower()
        media_type = (link.get("type") or "").lower()
        if "alternate" not in rel or not (
            "rss" in media_type or "atom" in media_type
        ):
            continue
        url = normalize_candidate_url(urljoin(base_url, link["href"]))
        if url not in result:
            result.append(url)
    return result


def detect_category_urls(
    html: str, base_url: str, policy: DomainPolicy,
) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    result: list[str] = []
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True).lower()
        href = str(anchor["href"]).lower()
        if not any(word in text or word in href for word in CATEGORY_WORDS):
            continue
        url = normalize_candidate_url(urljoin(base_url, anchor["href"]))
        if url_allowed(url, policy) and url not in result:
            result.append(url)
    return result[:12]


def parse_result_candidates(
    html: str,
    base_url: str,
    policy: DomainPolicy,
    source: str,
    keyword: str,
) -> list[Candidate]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    result: list[Candidate] = []
    containers = soup.select("article, main li, .result, .search-result, .titlist li")
    anchors = [item.find("a", href=True) for item in containers]
    if not any(anchors):
        anchors = soup.find_all("a", href=True)
    for anchor in anchors:
        if not anchor:
            continue
        url = normalize_candidate_url(urljoin(base_url, anchor["href"]))
        if url in seen or not url_allowed(url, policy):
            continue
        seen.add(url)
        title = anchor.get_text(" ", strip=True)
        score = 100 if keyword.lower() in title.lower() else 70
        result.append(Candidate(url, source, keyword, title, score))
    return result


def parse_pagination(
    html: str, base_url: str, policy: DomainPolicy,
) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True)
        classes = " ".join(anchor.get("class", []))
        if not (
            text in {"下一页", "下页", ">", "»"}
            or text.isdigit()
            or "page" in classes.lower()
        ):
            continue
        url = normalize_candidate_url(urljoin(base_url, anchor["href"]))
        if url_allowed(url, policy) and url not in links:
            links.append(url)
    return links


def parse_sitemap(xml: str, policy: DomainPolicy) -> list[str]:
    soup = BeautifulSoup(xml, "xml")
    result: list[str] = []
    for loc in soup.find_all("loc"):
        url = normalize_candidate_url(loc.get_text(strip=True))
        if url_allowed(url, policy) and url not in result:
            result.append(url)
    return result


def parse_sitemap_index(xml: str, policy: DomainPolicy) -> list[str]:
    soup = BeautifulSoup(xml, "xml")
    if soup.find("sitemapindex") is None:
        return []
    result: list[str] = []
    for sitemap in soup.find_all("sitemap"):
        loc = sitemap.find("loc")
        if not loc:
            continue
        url = normalize_candidate_url(loc.get_text(strip=True))
        if url_allowed(url, policy) and url not in result:
            result.append(url)
    return result


def parse_feed(xml: str, policy: DomainPolicy) -> list[Candidate]:
    soup = BeautifulSoup(xml, "xml")
    result: list[Candidate] = []
    for item in soup.find_all(["item", "entry"]):
        link = item.find("link")
        raw = link.get("href") if link and link.get("href") else (
            link.get_text(strip=True) if link else "")
        url = normalize_candidate_url(raw)
        if not raw or not url_allowed(url, policy):
            continue
        title = item.title.get_text(" ", strip=True) if item.title else ""
        result.append(Candidate(url, "feed", title_hint=title, score=65))
    return result
```

- [ ] **步骤 5：运行测试并确认通过**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_discovery_parsers.py -q
```

预期：`3 passed`。

- [ ] **步骤 6：记录检查点**

检查 fixture 不含目标站 Cookie、认证头或个人数据。授权后建议提交信息：
`feat: add generic discovery parsers`。

---

### 任务 4：实现站点适配器注册表

**文件：**

- 创建：`discovery/adapters.py`
- 创建：`tests/test_discovery_adapters.py`
- 创建：`tests/fixtures/discovery/portal_search_home.html`
- 创建：`tests/fixtures/discovery/portal_search_results.html`
- 创建：`tests/fixtures/discovery/freecms_home.html`
- 创建：`tests/fixtures/discovery/freecms_search.html`
- 创建：`tests/fixtures/discovery/freecms_failure.json`

- [ ] **步骤 1：保存最小脱敏 fixture**

fixture 只保留用于识别的 DOM 和接口结构：

```html
<!-- portal_search_home.html -->
<div id="search-form">
  <div class="form" data-action="https://search.portal.test/">
    <input name="s"><input type="submit" value="搜本站">
  </div>
</div>
```

```html
<!-- portal_search_results.html -->
<ul class="titlist">
  <li><div class="tit">
    <a href="https://www.portal.test/news/202607/1.shtml">普通标题</a>
  </div></li>
  <li><div class="tit">
    <a href="https://content.portal.test/2026/example/">资料页</a>
  </div></li>
</ul>
<div class="Page"><a data-page="2" href="?s=alpha&amp;p=2">下一页</a></div>
```

```html
<!-- freecms_home.html -->
<input id="searchinput" name="key">
<a id="megaloscopeHref"
   href="/freecms/site/zygjjgzfcgzx/searchlist/index.html"></a>
<a href="/freecms/site/zygjjgzfcgzx/cggg/index.html">采购公告</a>
<a href="/freecms/site/zygjjgzfcgzx/zdfg/index.html">法规制度</a>
```

```html
<!-- freecms_search.html -->
<script>
var data = '?&title=' + searchContent + '&currPage=' + currPage +
           '&pageSize=' + pageSize;
$.ajax({url: '/freecms/rest/v1/notice/searchAll.do' + data});
</script>
```

```json
{"msg":"公告列表查询失败","code":"-1"}
```

- [ ] **步骤 2：编写失败测试**

```python
from pathlib import Path

from discovery.adapters import (
    FreeCmsAdapter, SiteAdapter, YunnanCmsAdapter, select_adapter,
)
from discovery.parsers import detect_search_specs, parse_result_candidates
from discovery.urltools import extend_policy_with_declared_urls

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_experimental_portal_uses_generic_adapter():
    homepage = fixture("portal_search_home.html")
    adapter = select_adapter(
        "https://www.portal.test/", homepage)
    assert type(adapter) is SiteAdapter
    assert adapter.profile == "generic"
    assert adapter.search_specs() == []
    specs = detect_search_specs(homepage, "https://www.portal.test/")
    policy = extend_policy_with_declared_urls(
        adapter.domain_policy("https://www.portal.test/"),
        [spec.url for spec in specs],
    )
    candidates = parse_result_candidates(
        fixture("portal_search_results.html"),
        "https://search.portal.test/?s=alpha",
        policy,
        "site-search",
        "alpha",
    )
    assert {item.url for item in candidates} == {
        "https://www.portal.test/news/202607/1.shtml",
        "https://content.portal.test/2026/example",
    }


def test_freecms_adapter_exposes_search_and_fallback_categories():
    adapter = select_adapter(
        "https://www.zycg.gov.cn/", fixture("freecms_home.html"))
    assert isinstance(adapter, FreeCmsAdapter)
    assert adapter.search_specs()[0].url.endswith(
        "/freecms/rest/v1/notice/searchAll.do")
    assert any("/cggg/" in url for url in adapter.category_urls())


def test_freecms_business_failure_is_not_empty_success():
    adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
    ok, rows, warning = adapter.parse_api_response(
        fixture("freecms_failure.json"))
    assert ok is False
    assert rows == []
    assert "公告列表查询失败" in warning


def test_yunnan_adapter_remains_available():
    html = '<script src="/searchN.aspx"></script>'
    adapter = select_adapter("https://dct.yn.gov.cn/", html)
    assert isinstance(adapter, YunnanCmsAdapter)
```

- [ ] **步骤 3：运行测试并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_discovery_adapters.py -q
```

预期：失败并提示 `discovery.adapters` 不存在。

- [ ] **步骤 4：实现适配器**

在 `discovery/adapters.py` 定义：

```python
from __future__ import annotations

import json
from urllib.parse import urljoin, urlsplit

from discovery.models import DomainPolicy, SearchSpec


class SiteAdapter:
    profile = "generic"

    def domain_policy(self, start_url: str) -> DomainPolicy:
        host = (urlsplit(start_url).hostname or "").lower()
        return DomainPolicy(host, frozenset({host}))

    def search_specs(self) -> list[SearchSpec]:
        return []

    def category_urls(self) -> list[str]:
        return []


class FreeCmsAdapter(SiteAdapter):
    profile = "freecms"

    def __init__(self, start_url: str):
        parts = urlsplit(start_url)
        self.origin = f"{parts.scheme}://{parts.netloc}"

    def search_specs(self) -> list[SearchSpec]:
        return [SearchSpec(
            "site-search-api",
            self.origin + "/freecms/rest/v1/notice/searchAll.do",
            "title",
            "currPage",
            (("pageSize", "10"),),
        )]

    def category_urls(self) -> list[str]:
        base = self.origin + "/freecms/site/zygjjgzfcgzx/"
        return [
            urljoin(base, "cggg/index.html"),
            urljoin(base, "zxgklanmu/index.html"),
            urljoin(base, "zdfg/index.html"),
        ]

    def parse_api_response(self, body: str) -> tuple[bool, list[dict], str]:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return False, [], "FreeCMS 搜索接口返回了无效 JSON"
        if str(data.get("code")) not in {"0", "200"}:
            return False, [], str(data.get("msg") or "FreeCMS 搜索接口业务失败")
        payload = data.get("data") or {}
        rows = payload.get("rows") if isinstance(payload, dict) else payload
        return True, list(rows or []), ""


class YunnanCmsAdapter(SiteAdapter):
    profile = "yunnan-cms"

    def __init__(self, start_url: str):
        parts = urlsplit(start_url)
        self.origin = f"{parts.scheme}://{parts.netloc}"

    def search_specs(self) -> list[SearchSpec]:
        return [SearchSpec(
            "site-search", self.origin + "/searchN.aspx", "tags", "page",
            (("type", ""),),
        )]


def select_adapter(start_url: str, homepage: str) -> SiteAdapter:
    host = urlsplit(start_url).netloc.lower()
    lowered = homepage.lower()
    if host == "zycg.gov.cn" or host.endswith(".zycg.gov.cn") \
            or "searchall.do" in lowered:
        return FreeCmsAdapter(start_url)
    if "searchn.aspx" in lowered or "searchclasscount.aspx" in lowered:
        return YunnanCmsAdapter(start_url)
    return SiteAdapter()
```

- [ ] **步骤 5：运行适配器测试**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_discovery_adapters.py -q
```

预期：`4 passed`。

- [ ] **步骤 6：记录检查点**

授权后建议提交信息：`feat: add site adapter registry`。

---

### 任务 5：实现受预算控制的页面抓取器

**文件：**

- 创建：`discovery/fetcher.py`
- 创建：`tests/test_discovery_fetcher.py`

- [ ] **步骤 1：编写失败测试**

```python
import asyncio

import httpx

from discovery.fetcher import DiscoveryFetcher
from discovery.models import BudgetManager, DiscoveryStats


def test_fetch_html_consumes_shared_budget():
    async def run():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/html"},
                text="<main>正文标记</main>",
            )
        )
        budget = BudgetManager(initial_pages=1, max_pages=1)
        stats = DiscoveryStats()
        async with httpx.AsyncClient(transport=transport) as client:
            fetcher = DiscoveryFetcher(client, budget, stats)
            assert "正文标记" in await fetcher.fetch_html("https://x.test/a")
            assert await fetcher.fetch_html("https://x.test/b") is None
        assert budget.used_html_pages == 1
    asyncio.run(run())


def test_non_html_is_recorded_without_crashing():
    async def run():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=b"x",
            )
        )
        stats = DiscoveryStats()
        async with httpx.AsyncClient(transport=transport) as client:
            fetcher = DiscoveryFetcher(client, BudgetManager(), stats)
            assert await fetcher.fetch_html("https://x.test/a.pdf") is None
        assert any("非 HTML" in warning for warning in stats.warnings)
    asyncio.run(run())
```

- [ ] **步骤 2：运行测试并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_discovery_fetcher.py -q
```

预期：失败并提示 `discovery.fetcher` 不存在。

- [ ] **步骤 3：实现抓取器**

```python
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import httpx

from crawler import PAGE_TIMEOUT, USER_AGENT, fetch_html_retry
from discovery.models import BudgetManager, DiscoveryStats


class DiscoveryFetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        budget: BudgetManager,
        stats: DiscoveryStats,
        concurrency: int = 8,
        per_host_concurrency: int = 4,
    ):
        self.client = client
        self.budget = budget
        self.stats = stats
        self.semaphore = asyncio.Semaphore(concurrency)
        self.per_host_concurrency = per_host_concurrency
        self.host_semaphores: dict[str, asyncio.Semaphore] = {}

    def host_semaphore(self, url: str) -> asyncio.Semaphore:
        host = urlsplit(url).netloc.lower()
        return self.host_semaphores.setdefault(
            host, asyncio.Semaphore(self.per_host_concurrency))

    async def fetch_html(self, url: str) -> str | None:
        if not self.budget.reserve_html():
            self.stats.partial = True
            return None
        async with self.semaphore, self.host_semaphore(url):
            try:
                return await fetch_html_retry(
                    self.client, url, attempts=2, base_delay=1.0)
            except ValueError as exc:
                self.stats.warnings.append(f"{url}: 非 HTML 内容")
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                self.stats.warnings.append(f"{url}: {type(exc).__name__}")
            return None

    async def fetch_rendered(
        self, url: str,
    ) -> tuple[str, list[str]] | None:
        if not self.budget.reserve_html():
            self.stats.partial = True
            return None
        async with self.semaphore, self.host_semaphore(url):
            try:
                import renderer
                html, links = await renderer.render_page(url)
                self.stats.rendered_pages += 1
                return html, links
            except Exception as exc:
                self.stats.warnings.append(
                    f"{url}: render {type(exc).__name__}")
                return None


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=PAGE_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )
```

动态 Provider 统一调用 `fetch_rendered`，不得直接调用
`renderer.render_page`，从而确保 Playwright 页面也计入同一预算。

- [ ] **步骤 4：运行测试并确认通过**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_discovery_fetcher.py -q
```

预期：`2 passed`。

- [ ] **步骤 5：记录检查点**

授权后建议提交信息：`feat: add budget-aware discovery fetcher`。

---

### 任务 6：实现通用 Provider

**文件：**

- 创建：`discovery/providers.py`
- 创建：`tests/test_discovery_providers.py`

- [ ] **步骤 1：编写 Provider 隔离测试**

```python
import asyncio

import httpx

from discovery.models import (
    BudgetManager, DiscoveryStats, DomainPolicy, SearchSpec,
)
from discovery.providers import SearchProvider, SitemapProvider


def test_search_provider_returns_candidates_and_pagination():
    html = """
    <main><article><a href="/article.html">标题 alpha</a></article></main>
    <a class="page" href="?q=alpha&page=2">下一页</a>
    """
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html"}, text=html))

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            provider = SearchProvider(
                client,
                BudgetManager(),
                DiscoveryStats(),
                DomainPolicy("x.test", frozenset({"x.test"})),
                [SearchSpec("site-search", "https://x.test/search", "q", "page")],
            )
            result = await provider.discover(["alpha"])
            assert [item.url for item in result] == [
                "https://x.test/article.html"
            ]
    asyncio.run(run())


def test_provider_failure_becomes_warning():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(503, text="unavailable"))

    async def run():
        stats = DiscoveryStats()
        async with httpx.AsyncClient(transport=transport) as client:
            provider = SitemapProvider(
                client, BudgetManager(), stats,
                DomainPolicy("x.test", frozenset({"x.test"})),
                "https://x.test/",
            )
            assert await provider.discover(["alpha"]) == []
        assert stats.sources_tried == {"sitemap"}
        assert stats.sources_succeeded == set()
        assert stats.warnings
    asyncio.run(run())
```

- [ ] **步骤 2：运行测试并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_discovery_providers.py -q
```

预期：失败并提示 `discovery.providers` 不存在。

- [ ] **步骤 3：实现 Provider 公共约定**

在 `discovery/providers.py` 定义：

```python
from __future__ import annotations

import httpx

from discovery.models import (
    BudgetManager, Candidate, DiscoveryStats, DomainPolicy, SearchSpec,
)
from discovery.parsers import (
    parse_feed, parse_pagination, parse_result_candidates, parse_sitemap,
    parse_sitemap_index,
)


class Provider:
    source = "unknown"

    def __init__(
        self,
        client: httpx.AsyncClient,
        budget: BudgetManager,
        stats: DiscoveryStats,
        policy: DomainPolicy,
    ):
        self.client = client
        self.budget = budget
        self.stats = stats
        self.policy = policy

    async def get_text(
        self,
        url: str,
        *,
        limit: int = 10,
        params: dict | None = None,
        counts_as_html: bool = True,
        source: str | None = None,
    ) -> tuple[str, str] | None:
        source = source or self.source
        self.stats.sources_tried.add(source)
        if not self.budget.reserve_provider(source, limit):
            return None
        if counts_as_html and not self.budget.reserve_html():
            self.stats.partial = True
            return None
        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            self.stats.sources_succeeded.add(source)
            return response.text, str(response.url)
        except Exception as exc:
            self.stats.warnings.append(
                f"{source}: {type(exc).__name__}")
            return None
```

随后实现：

```python
class SearchProvider(Provider):
    source = "site-search"

    def __init__(self, client, budget, stats, policy, specs):
        super().__init__(client, budget, stats, policy)
        self.specs: list[SearchSpec] = specs

    async def discover(self, keywords: list[str]) -> list[Candidate]:
        result: list[Candidate] = []
        seen: set[str] = set()
        for spec in self.specs:
            for keyword in keywords[:6]:
                loaded = await self.get_text(
                    spec.url,
                    limit=10,
                    params=spec.params_for(keyword),
                    counts_as_html=True,
                    source=spec.source,
                )
                if loaded is None:
                    continue
                html, final_url = loaded
                for item in parse_result_candidates(
                    html, final_url, self.policy,
                    spec.source, keyword,
                ):
                    if item.url not in seen:
                        seen.add(item.url)
                        result.append(item)
                for page_url in parse_pagination(
                    html, final_url, self.policy
                )[:9]:
                    page = await self.get_text(
                        page_url,
                        limit=10,
                        counts_as_html=True,
                        source=spec.source,
                    )
                    if page is None:
                        continue
                    page_html, page_final_url = page
                    for item in parse_result_candidates(
                        page_html, page_final_url, self.policy,
                        spec.source, keyword,
                    ):
                        if item.url not in seen:
                            seen.add(item.url)
                            result.append(item)
        return result


class SitemapProvider(Provider):
    source = "sitemap"

    def __init__(self, client, budget, stats, policy, origin):
        super().__init__(client, budget, stats, policy)
        self.origin = origin.rstrip("/")

    async def discover(self, keywords: list[str]) -> list[Candidate]:
        loaded = await self.get_text(
            self.origin + "/sitemap.xml", limit=5, counts_as_html=False)
        if loaded is None:
            return []
        body, _final_url = loaded
        urls = parse_sitemap(body, self.policy)
        for child_url in parse_sitemap_index(body, self.policy)[:4]:
            child = await self.get_text(
                child_url, limit=5, counts_as_html=False)
            if child:
                child_body, _child_final_url = child
                urls.extend(parse_sitemap(child_body, self.policy))
        return [
            Candidate(url, self.source, score=55)
            for url in dict.fromkeys(urls)
        ]


class FeedProvider(Provider):
    source = "feed"

    def __init__(self, client, budget, stats, policy, feed_urls):
        super().__init__(client, budget, stats, policy)
        self.feed_urls = feed_urls

    async def discover(self, keywords: list[str]) -> list[Candidate]:
        result: list[Candidate] = []
        for url in self.feed_urls[:5]:
            loaded = await self.get_text(
                url, limit=5, counts_as_html=False)
            if loaded:
                body, _final_url = loaded
                result.extend(parse_feed(body, self.policy))
        return result


class CategoryProvider(Provider):
    source = "category"

    def __init__(
        self, client, budget, stats, policy, category_urls,
        fetcher=None, render_mode="auto",
    ):
        super().__init__(client, budget, stats, policy)
        self.category_urls = category_urls
        self.fetcher = fetcher
        self.render_mode = render_mode

    async def discover(self, keywords: list[str]) -> list[Candidate]:
        result: list[Candidate] = []
        for url in self.category_urls[:8]:
            loaded = await self.get_text(
                url, limit=20, counts_as_html=True)
            if loaded is None:
                continue
            body, final_url = loaded
            batch = parse_result_candidates(
                body, final_url, self.policy, self.source, keywords[0])
            result.extend(batch)
            if (
                (not batch or looks_js_driven(body))
                and self.render_mode != "off"
                and self.fetcher is not None
            ):
                rendered = await self.fetcher.fetch_rendered(final_url)
                if rendered:
                    _rendered_html, links = rendered
                    result.extend(
                        Candidate(
                            normalize_candidate_url(link),
                            self.source,
                            score=60,
                            requires_render=False,
                        )
                        for link in links
                        if url_allowed(link, self.policy)
                    )
            for page_url in parse_pagination(
                    body, final_url, self.policy)[:12]:
                page = await self.get_text(
                    page_url, limit=20, counts_as_html=True)
                if page:
                    page_body, page_final_url = page
                    result.extend(parse_result_candidates(
                        page_body, page_final_url, self.policy,
                        self.source, keywords[0]))
        return result
```

同一文件增加 FreeCMS API Provider：

```python
from urllib.parse import urlencode, urljoin

from discovery.adapters import FreeCmsAdapter, YunnanCmsAdapter
from discovery.urltools import normalize_candidate_url, url_allowed
from matcher import looks_js_driven


class FreeCmsApiProvider(Provider):
    source = "site-search-api"

    def __init__(self, client, budget, stats, policy, adapter):
        super().__init__(client, budget, stats, policy)
        self.adapter: FreeCmsAdapter = adapter

    async def discover(self, keywords: list[str]) -> list[Candidate]:
        result: list[Candidate] = []
        spec = self.adapter.search_specs()[0]
        for keyword in keywords[:6]:
            loaded = await self.get_text(
                spec.url,
                limit=10,
                params=spec.params_for(keyword),
                counts_as_html=False,
            )
            if loaded is None:
                continue
            body, _final_url = loaded
            ok, rows, warning = self.adapter.parse_api_response(body)
            if not ok:
                self.stats.sources_succeeded.discard(self.source)
                self.stats.warnings.append(f"{self.source}: {warning}")
                continue
            for row in rows:
                raw = str(row.get("pageUrl") or "")
                if not raw:
                    continue
                url = urljoin(self.adapter.origin + "/", raw)
                item_id = row.get("id")
                if item_id and "id=" not in url:
                    separator = "&" if "?" in url else "?"
                    url += separator + urlencode({"id": item_id})
                url = normalize_candidate_url(url)
                if not url_allowed(url, self.policy):
                    continue
                title = str(row.get("title") or "")
                result.append(Candidate(
                    url, self.source, keyword, title,
                    100 if keyword.lower() in title.lower() else 75,
                ))
        return result


class YunnanCmsProvider(Provider):
    source = "site-search"

    def __init__(self, client, budget, stats, policy, adapter):
        super().__init__(client, budget, stats, policy)
        self.adapter: YunnanCmsAdapter = adapter

    async def discover(self, keywords: list[str]) -> list[Candidate]:
        result: list[Candidate] = []
        for keyword in keywords[:6]:
            count = await self.get_text(
                self.adapter.origin + "/searchClassCount.aspx",
                limit=1,
                params={"tags": keyword},
                counts_as_html=False,
            )
            if count is None:
                continue
            for page_no in range(1, 11):
                loaded = await self.get_text(
                    self.adapter.origin + "/searchN.aspx",
                    limit=10,
                    params={"page": page_no, "type": "", "tags": keyword},
                    counts_as_html=True,
                )
                if loaded is None:
                    break
                body, final_url = loaded
                batch = parse_result_candidates(
                    body, final_url, self.policy, self.source, keyword)
                result.extend(batch)
                if not batch:
                    break
        return result
```

- [ ] **步骤 4：运行 Provider 测试**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_discovery_providers.py -q
```

预期：`2 passed`。

- [ ] **步骤 5：运行解析和适配器回归**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_discovery_parsers.py tests/test_discovery_adapters.py -q
```

预期：全部通过。

- [ ] **步骤 6：记录检查点**

授权后建议提交信息：`feat: add generic discovery providers`。

---

### 任务 7：实现候选排序与发现调度引擎

**文件：**

- 创建：`discovery/engine.py`
- 创建：`tests/test_discovery_engine.py`
- 修改：`discovery/__init__.py`

- [ ] **步骤 1：编写候选排序和深度无关测试**

```python
import asyncio

from crawler import CrawledPage, CrawlResult
from discovery.engine import merge_candidates, rank_candidates
from discovery.models import Candidate


def test_rank_prioritizes_search_but_preserves_source_diversity():
    items = [
        Candidate("https://x.test/n1.html", "category", score=60,
                  section="news"),
        Candidate("https://x.test/n2.html", "site-search", score=100,
                  keyword="alpha", section="news"),
        Candidate("https://x.test/g1.html", "sitemap", score=55,
                  section="guide"),
    ]
    ranked = rank_candidates(items, per_source=1, per_section=2)
    assert ranked[0].url.endswith("/n2.html")
    assert {item.source for item in ranked} == {
        "site-search", "category", "sitemap"
    }


def test_merge_deduplicates_url_and_keeps_best_score():
    merged = merge_candidates([
        Candidate("https://x.test/a.html", "sitemap", score=55),
        Candidate("https://x.test/a.html", "site-search", score=100),
    ])
    assert len(merged) == 1
    assert merged[0].source == "site-search"


def test_structured_candidates_do_not_depend_on_bfs_depth():
    base = CrawlResult(pages=[
        CrawledPage("https://x.test/", "<html>首页</html>")
    ])
    candidate = Candidate(
        "https://x.test/archive/2000/deep.html", "sitemap", score=55)
    assert candidate.url not in {page.url for page in base.pages}
    assert candidate.source == "sitemap"
```

- [ ] **步骤 2：运行测试并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_discovery_engine.py -q
```

预期：失败并提示 `discovery.engine` 不存在。

- [ ] **步骤 3：实现排序与合并**

```python
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from crawler import CrawledPage, CrawlResult
from discovery.adapters import FreeCmsAdapter, select_adapter
from discovery.fetcher import DiscoveryFetcher, make_client
from discovery.models import (
    BudgetManager, Candidate, DiscoveryStats, DomainPolicy,
)
from discovery.parsers import (
    detect_category_urls, detect_feed_urls, detect_search_specs,
)
from discovery.providers import (
    CategoryProvider, FeedProvider, FreeCmsApiProvider, SearchProvider,
    SitemapProvider, YunnanCmsProvider,
)
from discovery.urltools import normalize_candidate_url


def merge_candidates(items: list[Candidate]) -> list[Candidate]:
    best: dict[str, Candidate] = {}
    for item in items:
        normalized = normalize_candidate_url(item.url)
        current = best.get(normalized)
        if current is None or item.score > current.score:
            best[normalized] = Candidate(
                normalized, item.source, item.keyword, item.title_hint,
                item.score, item.requires_render, item.section,
            )
    return list(best.values())


def rank_candidates(
    items: list[Candidate],
    per_source: int = 40,
    per_section: int = 40,
) -> list[Candidate]:
    ranked = sorted(items, key=lambda item: (-item.score, item.url))
    source_counts: dict[str, int] = {}
    section_counts: dict[str, int] = {}
    result: list[Candidate] = []
    for item in ranked:
        section = item.section or "_"
        if source_counts.get(item.source, 0) >= per_source:
            continue
        if section_counts.get(section, 0) >= per_section:
            continue
        source_counts[item.source] = source_counts.get(item.source, 0) + 1
        section_counts[section] = section_counts.get(section, 0) + 1
        result.append(item)
    return result


@dataclass
class DiscoveryRun:
    pages: list[CrawledPage]
    failed: list[dict]
    stats: DiscoveryStats
```

- [ ] **步骤 4：实现 `discover_pages` 调度**

使用如下公共签名：

```python
async def discover_pages(
    start_url: str,
    keywords: list[str],
    base_result: CrawlResult,
    depth: int,
    render_mode: str,
    timeout_seconds: float = 120.0,
    started_at: float | None = None,
) -> DiscoveryRun:
```

实现顺序必须明确：

1. `used_html_pages = len(base_result.pages)`，`started_at` 使用 API 搜索开始时间。
2. 从起始页 HTML 选择适配器和 `DomainPolicy`。
3. 将适配器搜索入口与 `detect_search_specs` 合并去重。
4. 并行运行 Search、Sitemap、Feed、Category Provider。
5. Provider 异常通过 `asyncio.gather(..., return_exceptions=True)` 隔离。
6. 合并和排序候选，删除 `base_result.pages` 已访问 URL。
7. 抓取前 60 页预算内候选；存在剩余高分候选才扩展到 120。
8. `render_mode` 仅决定动态入口是否调用 Playwright，不改变正文验证规则。
9. 以 API 搜索开始时间计算 `elapsed_ms` 并返回 `DiscoveryRun`。

Provider 构造和正文抓取必须放在同一个
`async with make_client() as client:` 块内。构造时使用以下分支，保证
FreeCMS 业务失败后栏目 Provider 仍会独立执行：

```python
    homepage = base_result.pages[0].html
    adapter = select_adapter(start_url, homepage)
    policy = adapter.domain_policy(start_url)
    stats = DiscoveryStats(profile=adapter.profile)
    budget = BudgetManager(
        timeout_seconds=timeout_seconds,
        used_html_pages=len(base_result.pages),
        started_at=started_at or time.monotonic(),
    )
    origin_parts = urlsplit(start_url)
    origin = f"{origin_parts.scheme}://{origin_parts.netloc}"
    detected_specs = detect_search_specs(homepage, start_url)
    feed_urls = detect_feed_urls(homepage, start_url)
    if adapter.profile == "generic":
        policy = extend_policy_with_declared_urls(
            policy,
            [spec.url for spec in detected_specs] + feed_urls,
        )
    specs = list({
        (spec.url, spec.query_param, spec.page_param): spec
        for spec in [*adapter.search_specs(), *detected_specs]
    }.values())
    category_urls = list(dict.fromkeys([
        *adapter.category_urls(),
        *detect_category_urls(homepage, start_url, policy),
    ]))

    async with make_client() as client:
        fetcher = DiscoveryFetcher(client, budget, stats)
        providers = [
            SitemapProvider(client, budget, stats, policy, origin),
            FeedProvider(client, budget, stats, policy, feed_urls),
            CategoryProvider(
                client, budget, stats, policy, category_urls,
                fetcher=fetcher, render_mode=render_mode),
        ]
        if isinstance(adapter, FreeCmsAdapter):
            providers.append(
                FreeCmsApiProvider(client, budget, stats, policy, adapter))
        elif isinstance(adapter, YunnanCmsAdapter):
            providers.append(
                YunnanCmsProvider(client, budget, stats, policy, adapter))
        elif specs:
            providers.append(
                SearchProvider(client, budget, stats, policy, specs))

        batches = await asyncio.gather(
            *(provider.discover(keywords) for provider in providers),
            return_exceptions=True,
        )
        all_candidates: list[Candidate] = []
        for provider, batch in zip(providers, batches):
            if isinstance(batch, Exception):
                stats.warnings.append(
                    f"{provider.source}: {type(batch).__name__}")
            else:
                all_candidates.extend(batch)
```

核心抓取循环：

```python
    ranked = rank_candidates(merge_candidates(all_candidates))
    visited = {
        normalize_candidate_url(page.url) for page in base_result.pages
    }
    pending = [item for item in ranked if item.url not in visited]
    stats.candidates_found = len(pending)
    pages: list[CrawledPage] = []

    # 以下代码继续位于上面的 make_client 上下文中
    async def fetch(item: Candidate) -> CrawledPage | None:
        html = await fetcher.fetch_html(item.url)
        if html is None:
            return None
        return CrawledPage(item.url, html)

    first_batch = pending[:max(0, budget.page_limit - budget.used_html_pages)]
    fetched = await asyncio.gather(*(fetch(item) for item in first_batch))
    pages.extend(page for page in fetched if page is not None)

    remaining = pending[len(first_batch):]
    high_value = [item for item in remaining if item.score >= 55]
    if budget.expand(len(high_value)):
        stats.budget_expanded = True
        second_batch = high_value[
            :max(0, budget.page_limit - budget.used_html_pages)
        ]
        fetched = await asyncio.gather(*(fetch(item) for item in second_batch))
        pages.extend(page for page in fetched if page is not None)

    stats.candidates_fetched = len(pages)
    attempted_capacity = budget.page_limit - len(base_result.pages)
    stats.partial = (
        stats.partial
        or budget.expired()
        or len(pending) > max(0, attempted_capacity)
    )
    stats.elapsed_ms = int(
        (time.monotonic() - budget.started_at) * 1000)
    return DiscoveryRun(pages=pages, failed=[], stats=stats)
```

在 `discovery/__init__.py` 增加：

```python
from discovery.engine import DiscoveryRun, discover_pages

__all__ += ["DiscoveryRun", "discover_pages"]
```

- [ ] **步骤 5：补充 Provider 失败隔离测试**

在 `tests/test_discovery_engine.py` 使用 monkeypatch 令一个 Provider 抛出
`RuntimeError`，另一个返回正文候选；断言任务返回候选且
`stats.warnings` 记录失败。

- [ ] **步骤 6：运行引擎测试**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_discovery_engine.py -q
```

预期：全部通过。

- [ ] **步骤 7：记录检查点**

授权后建议提交信息：`feat: orchestrate adaptive site discovery`。

---

### 任务 8：实现正文容器提取和最终正文匹配

**文件：**

- 修改：`matcher.py:10-170`
- 修改：`tests/test_matcher.py`

- [ ] **步骤 1：编写正文独有关键词测试**

在 `tests/test_matcher.py` 增加：

```python
from matcher import extract_main_text, match_body_page


def test_extract_main_text_removes_navigation_and_footer():
    html = """
    <html><body>
      <nav>导航伪命中词</nav>
      <main><article><p>真正正文随机标记 QX-7319</p></article></main>
      <footer>页脚伪命中词</footer>
    </body></html>
    """
    text = extract_main_text(html)
    assert "QX-7319" in text
    assert "导航伪命中词" not in text
    assert "页脚伪命中词" not in text


def test_match_body_page_ignores_title_url_and_search_metadata():
    html = """
    <html><head>
      <title>标题只有 TITLE-ONLY</title>
      <meta name="description" content="META-ONLY">
    </head><body>
      <nav><a href="/URL-ONLY">LINK-ONLY</a></nav>
      <article><p>正文唯一标记 BODY-ONLY-8472</p></article>
    </body></html>
    """
    hits = match_body_page(
        html, "https://x.test/URL-ONLY", [
            "TITLE-ONLY", "META-ONLY", "LINK-ONLY", "BODY-ONLY-8472",
        ])
    assert [hit.keyword for hit in hits] == ["BODY-ONLY-8472"]
    assert "正文唯一标记" in hits[0].snippet
```

- [ ] **步骤 2：运行测试并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_matcher.py -q
```

预期：失败并提示 `extract_main_text` 和 `match_body_page` 不存在。

- [ ] **步骤 3：实现正文提取**

在 `matcher.py` 增加：

```python
NOISE_SELECTORS = (
    "script, style, noscript, nav, header, footer, aside, "
    "[hidden], [aria-hidden='true'], .nav, .navbar, .footer, "
    ".sidebar, .advert, .ad, .login"
)


def extract_main_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select(NOISE_SELECTORS):
        node.decompose()
    preferred = soup.select_one(
        "article, main, [role='main'], .article-content, .content-main, "
        ".article_body, .detail-content"
    )
    root = preferred or soup.body or soup
    return re.sub(r"\s+", " ", root.get_text(" ", strip=True)).strip()


def match_body_page(
    html: str, page_url: str, keywords: list[str],
) -> list[Hit]:
    text = extract_main_text(html)
    result: list[Hit] = []
    for keyword in dict.fromkeys(k.strip() for k in keywords if k.strip()):
        snippet = make_snippet(text, keyword)
        if snippet is None:
            continue
        result.append(Hit(
            kind="text",
            snippet=snippet,
            keyword=keyword,
            href=f"{page_url}#:~:text={quote(keyword)}",
        ))
    return result
```

新增 `match_body_crawl_result`，返回结构与现有
`match_crawl_result` 一致：

```python
def match_body_crawl_result(
    pages, keywords: list[str],
) -> tuple[list[dict], int]:
    results: list[dict] = []
    total_hits = 0
    for page in pages:
        hits = match_body_page(page.html, page.url, keywords)
        if not hits:
            continue
        total_hits += len(hits)
        results.append({
            "pageUrl": page.url,
            "pageTitle": extract_title(page.html),
            "hits": [asdict(hit) for hit in hits],
        })
    return results, total_hits
```

- [ ] **步骤 4：运行 matcher 测试**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_matcher.py -q
```

预期：现有属性匹配测试和新增正文专用测试均通过。`match_page` 保持兼容；
只有搜索主流程改用 `match_body_crawl_result`。

- [ ] **步骤 5：记录检查点**

授权后建议提交信息：`feat: verify matches against article body`。

---

### 任务 9：接入 `/api/search` 并保持响应兼容

**文件：**

- 修改：`server.py:26-175`
- 修改：`tests/test_server.py`
- 修改：`tests/test_server_auto.py`
- 修改：`tests/test_sitesearch.py`

- [ ] **步骤 1：编写 API 诊断字段失败测试**

在 `tests/test_server.py` 增加：

```python
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
```

增加 monkeypatch 测试：伪造 `discover_pages` 返回一页正文只含
`BODY-MARK-9184`，标题和 URL 不含该词；断言 API 产生正文命中。

- [ ] **步骤 2：运行 API 测试并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_server.py -q
```

预期：失败，响应尚无 `discovery`。

- [ ] **步骤 3：替换固定 `sitesearch` 补充块**

在 `server.py` 导入：

```python
from discovery import discover_pages
from matcher import (
    extract_main_text, extract_title, looks_js_driven,
    match_body_crawl_result,
)
```

增加基础 BFS 配额：

```python
BASE_BFS_PAGE_BUDGET = 30
```

`_crawl_or_502` 调用 `crawl` 时显式传入
`max_pages=BASE_BFS_PAGE_BUDGET`，为搜索表单、栏目和 sitemap 发现保留首轮
60 页预算中的其余页面。同步修正 `crawler.crawl` 的渲染分支：

```python
if render:
    return await _crawl_render(
        start_url, depth, max_pages=max_pages, deadline=deadline)
```

这样普通 BFS 不会在结构化发现开始前独占全部 60 页预算。

将 `_match_crawl` 改为：

```python
def _match_crawl(crawl_result, all_keywords: list[str]) -> tuple[list, int]:
    return match_body_crawl_result(crawl_result.pages, all_keywords)
```

删除 `SITESEARCH_BUDGET_SECONDS` 和第 145–155 行固定补充逻辑，改为：

```python
    discovery_run = await discover_pages(
        req.startUrl,
        all_keywords,
        crawl_result,
        req.depth,
        req.render,
        timeout_seconds=SEARCH_BUDGET_SECONDS,
        started_at=search_started,
    )
    crawl_result.pages.extend(discovery_run.pages)
    crawl_result.failed.extend(discovery_run.failed)
```

响应中增加：

```python
        "discovery": discovery_run.stats.as_dict(),
```

保留旧 `siteSearch` 字段一个发布周期：

```python
        "siteSearch": {
            "available": "site-search" in discovery_run.stats.sources_succeeded,
            "linksFound": discovery_run.stats.candidates_found,
            "pagesFetched": discovery_run.stats.candidates_fetched,
            "deprecated": True,
        },
```

缓存正文改为：

```python
    texts = {p.url: extract_main_text(p.html) for p in crawl_result.pages}
```

- [ ] **步骤 4：让基础 BFS 协作式遵守总截止时间**

给 `crawler.crawl` 和 `_crawl_render` 增加可选参数：

```python
deadline: float | None = None
```

在 `crawler.py` 增加：

```python
import time


async def gather_before_deadline(coroutines, deadline):
    tasks = {asyncio.create_task(item) for item in coroutines}
    if not tasks:
        return [], False
    timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    results = [
        task.result() for task in done
        if not task.cancelled() and task.exception() is None
    ]
    return results, bool(pending)
```

将静态和渲染 BFS 中的批量 `asyncio.gather` 改为
`gather_before_deadline`。超时前已完成页面保留，未完成任务取消；不得因总
预算结束丢弃已有结果。

在 `server.search` 开头记录：

```python
import time

search_started = time.monotonic()
deadline = search_started + SEARCH_BUDGET_SECONDS
```

`_crawl_or_502` 将 `deadline` 传给 `crawl`，不再用一个会取消全部结果的
外层 `asyncio.timeout`。删除 `RENDER_BUDGET_SECONDS = 600`，静态、渲染和
结构化发现共享同一个 120 秒截止时间。起始页自身不可访问仍返回 502；后续
达到截止时间则返回已完成页面，并令 `discovery.partial = true`。

新增测试：

```python
def test_crawl_deadline_keeps_completed_pages(monkeypatch, site_server):
    result = asyncio.run(crawl(
        f"{site_server}/index.html",
        depth=3,
        deadline=time.monotonic() + 0.2,
    ))
    assert result.pages
    assert result.pages[0].url.endswith("/index.html")


def test_api_reserves_budget_for_structured_discovery(site_server):
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
```

- [ ] **步骤 5：更新旧断言**

`tests/test_sitesearch.py` 中旧接口测试保留，但断言新增：

```python
assert data["siteSearch"]["deprecated"] is True
assert "discovery" in data
```

不删除现有响应字段，确保前端和定时任务兼容。

- [ ] **步骤 6：运行 API 回归**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_server.py tests/test_server_auto.py tests/test_sitesearch.py -q
```

预期：全部通过。

- [ ] **步骤 7：记录检查点**

授权后建议提交信息：`feat: integrate generalized discovery API`。

---

### 任务 10：迁移旧云南 CMS 能力和定时任务

**文件：**

- 修改：`sitesearch.py`
- 修改：`jobs.py:190-260`
- 修改：`tests/test_sitesearch.py`
- 修改：`tests/test_jobs.py`

- [ ] **步骤 1：为兼容层编写测试**

```python
def test_legacy_sitesearch_delegates_to_discovery(monkeypatch):
    import asyncio
    import sitesearch
    from crawler import CrawledPage
    from discovery.engine import DiscoveryRun
    from discovery.models import DiscoveryStats

    async def fake_discover(*args, **kwargs):
        return DiscoveryRun(
            [CrawledPage("https://x.test/a.html", "<main>alpha</main>")],
            [],
            DiscoveryStats(profile="generic"),
        )

    monkeypatch.setattr(sitesearch, "discover_pages", fake_discover)
    pages, info = asyncio.run(sitesearch.collect_pages(
        "https://x.test/", ["alpha"]))
    assert [page.url for page in pages] == ["https://x.test/a.html"]
    assert info["deprecated"] is True
```

- [ ] **步骤 2：将 `sitesearch.py` 改为兼容层**

保留 `collect_pages` 公共签名，先抓取起始页建立有效站点画像，再调用
`discover_pages`。旧解析函数可保留至所有测试迁移完成，但不得再由
`server.py` 直接调用。

```python
from crawler import crawl
from discovery import discover_pages
from discovery.urltools import normalize_candidate_url


async def collect_pages(start_url, keywords, skip=frozenset()):
    base = await crawl(start_url, depth=0, max_pages=1, render=False)
    run = await discover_pages(
        start_url, keywords, base, depth=1, render_mode="off")
    skipped = {normalize_candidate_url(url) for url in skip}
    pages = [
        page for page in run.pages
        if normalize_candidate_url(page.url) not in skipped
    ]
    stats = run.stats
    return pages, {
        "available": bool(stats.sources_succeeded),
        "linksFound": stats.candidates_found,
        "pagesFetched": len(pages),
        "deprecated": True,
    }
```

- [ ] **步骤 3：让定时任务注入统一发现函数**

将 `jobs.run_job` 的 `sitesearch_fn` 参数改名为 `discovery_fn`，默认值为
`discover_pages`。测试中的 fake 函数返回 `DiscoveryRun`，最终结果仍保留
`pagesCrawled`、`totalHits` 和 `top`。

- [ ] **步骤 4：运行定时任务和兼容测试**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_sitesearch.py tests/test_jobs.py tests/test_server_jobs.py -q
```

预期：全部通过。

- [ ] **步骤 5：记录检查点**

授权后建议提交信息：`refactor: migrate legacy site search to discovery engine`。

---

### 任务 11：更新前端诊断和部分结果提示

**文件：**

- 修改：`static/index.html:286-325`
- 修改：`tests/test_frontend_smoke.py`

- [ ] **步骤 1：编写前端静态断言**

在 `tests/test_frontend_smoke.py` 增加：

```python
def test_frontend_renders_discovery_diagnostics():
    html = STATIC_INDEX.read_text(encoding="utf-8")
    assert "data.discovery" in html
    assert "sourcesSucceeded" in html
    assert "partial" in html
    assert "已达到搜索预算" in html
```

- [ ] **步骤 2：运行测试并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_frontend_smoke.py -q
```

预期：至少一个新增断言失败。

- [ ] **步骤 3：更新结果摘要**

将结果摘要改为：

```javascript
const discovery = data.discovery || {};
const sources = (discovery.sourcesSucceeded || []).join("、");
let html = `<p class="meta-line">
  共抓取 ${data.pagesCrawled} 个页面（深度 ${data.depth} 层），
  命中 ${data.totalHits} 处。
  ${sources ? `<br>有效发现来源：${escapeHtml(sources)}` : ""}
  ${discovery.budgetExpanded ? "<br>已启用扩展页面预算。" : ""}
  ${discovery.partial
    ? '<br><strong>已达到搜索预算，结果可能不完整。</strong>' : ""}
  ${data.autoNote ? `<br>${escapeHtml(data.autoNote)}` : ""}
</p>`;
```

warning 只展示通道名称和简短原因，继续使用 `escapeHtml`，不得输出认证信息。

- [ ] **步骤 4：运行前端测试**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_frontend_smoke.py -q
```

预期：全部通过。

- [ ] **步骤 5：记录检查点**

授权后建议提交信息：`feat: show discovery diagnostics in search results`。

---

### 任务 12：增加端到端正文发现 fixture

**文件：**

- 创建：`tests/test_discovery_integration.py`
- 修改：`tests/conftest.py`

- [ ] **步骤 1：构造本地多结构测试站**

测试 HTTP Handler 提供：

```text
/                         首页，不包含目标词
/search?q=BODY-ONLY-8472  返回候选标题，标题不含目标词
/category?page=1          返回下一页
/category?page=2          返回深层正文链接
/api/search               返回 {"code":"-1","msg":"业务失败"}
/deep/article.html        正文包含随机标记 BODY-ONLY-8472
/navigation.html          仅导航包含 BODY-ONLY-8472
```

正文页面：

```html
<html><head><title>普通公告</title></head><body>
  <nav>站点导航</nav>
  <article><p>采购说明中的随机正文标记 BODY-ONLY-8472。</p></article>
</body></html>
```

- [ ] **步骤 2：编写端到端测试**

```python
def test_body_only_keyword_is_found_independent_of_bfs_depth(discovery_site):
    from fastapi.testclient import TestClient
    from server import app

    client = TestClient(app)
    for depth in (1, 2, 3):
        response = client.post("/api/search", json={
            "startUrl": discovery_site + "/",
            "keywords": ["BODY-ONLY-8472"],
            "depth": depth,
            "render": "off",
        })
        assert response.status_code == 200
        data = response.json()
        urls = {item["pageUrl"] for item in data["results"]}
        assert discovery_site + "/deep/article.html" in urls
        assert discovery_site + "/navigation.html" not in urls


def test_failed_api_falls_back_to_category(discovery_site):
    response = TestClient(app).post("/api/search", json={
        "startUrl": discovery_site + "/",
        "keywords": ["BODY-ONLY-8472"],
        "depth": 1,
        "render": "off",
    })
    discovery = response.json()["discovery"]
    assert "category" in discovery["sourcesSucceeded"]
    assert any("业务失败" in item for item in discovery["warnings"])
```

- [ ] **步骤 3：运行测试并确认失败**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_discovery_integration.py -q
```

预期：在引擎完全接通前失败；失败原因必须是缺少发现或响应字段，而不是测试
服务器异常。

- [ ] **步骤 4：补齐引擎中的 FreeCMS 失败降级和栏目分页**

仅修改 `discovery/providers.py` 与 `discovery/engine.py`，直到两个集成测试通过。
具体完成条件：

```python
assert article_url in fetched_urls
assert navigation_only_url not in result_urls
assert stats.partial is False
assert "category" in stats.sources_succeeded
```

- [ ] **步骤 5：运行集成测试并确认通过**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_discovery_integration.py -q
```

预期：全部通过。

- [ ] **步骤 6：记录检查点**

授权后建议提交信息：`test: cover body-only discovery and provider fallback`。

---

### 任务 13：添加参数化线上冒烟工具

**文件：**

- 创建：`scripts/smoke_discovery.py`
- 修改：`README.md`

- [ ] **步骤 1：编写不含固定业务关键词的脚本**

```python
from __future__ import annotations

import argparse
import asyncio
import json

import httpx


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("start_url")
    parser.add_argument("keyword")
    parser.add_argument("--api", default="http://127.0.0.1:7100")
    parser.add_argument("--depth", type=int, default=1, choices=(1, 2, 3))
    args = parser.parse_args()

    payload = {
        "startUrl": args.start_url,
        "keywords": [args.keyword],
        "depth": args.depth,
        "render": "auto",
    }
    async with httpx.AsyncClient(timeout=130) as client:
        response = await client.post(args.api + "/api/search", json=payload)
        response.raise_for_status()
    data = response.json()
    summary = {
        "pagesCrawled": data["pagesCrawled"],
        "totalHits": data["totalHits"],
        "discovery": data["discovery"],
        "resultUrls": [item["pageUrl"] for item in data["results"]],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **步骤 2：更新 README 使用说明**

加入：

```powershell
.\.venv\Scripts\python.exe scripts\smoke_discovery.py `
  "https://www.gamersky.com/" "用户选择的正文关键词"

.\.venv\Scripts\python.exe scripts\smoke_discovery.py `
  "https://www.zycg.gov.cn/" "用户选择的正文关键词"
```

明确说明：

- 线上网站结构和内容会变化。
- 中央政府采购网搜索接口失败时应看到 category 等降级来源。
- 冒烟测试不会绕过验证码或登录。
- 不把真实姓名或固定关键词写入脚本。

- [ ] **步骤 3：验证脚本帮助**

```powershell
.\.venv\Scripts\python.exe scripts/smoke_discovery.py --help
```

预期：显示 `start_url`、`keyword`、`--api`、`--depth`。

- [ ] **步骤 4：记录检查点**

授权后建议提交信息：`docs: add generalized discovery smoke workflow`。

---

### 任务 14：全量回归、预算审计与文档收尾

**文件：**

- 修改：`README.md`
- 检查：`docs/superpowers/specs/2026-07-28-generalized-site-discovery-design.md`
- 检查：全部新增和修改文件

- [ ] **步骤 1：扫描业务关键词特例和占位内容**

运行：

```powershell
rg -n "王丹莉|具体业务关键词特例|真实姓名示例" `
  discovery tests scripts server.py matcher.py sitesearch.py jobs.py
```

预期：无命中。测试随机标记只能使用 `BODY-ONLY-8472` 等虚构值。

- [ ] **步骤 2：运行 discovery 专项测试**

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/test_discovery_models.py `
  tests/test_discovery_urltools.py `
  tests/test_discovery_parsers.py `
  tests/test_discovery_adapters.py `
  tests/test_discovery_fetcher.py `
  tests/test_discovery_providers.py `
  tests/test_discovery_engine.py `
  tests/test_discovery_integration.py -q
```

预期：全部通过，无失败和错误。

- [ ] **步骤 3：运行完整测试**

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

预期：原有 113 项和全部新增测试均通过。允许保留现有 FastAPI 生命周期及
Windows asyncio transport 警告，但不得出现新失败。

- [ ] **步骤 4：审计页面与时间预算**

在预算测试中使用 150 个候选，断言：

```python
assert stats.candidates_fetched <= 120
assert budget.used_html_pages <= 120
assert stats.budget_expanded is True
```

使用过期的 `started_at`，断言不再发起页面抓取且 `partial` 为 `True`。

- [ ] **步骤 5：检查 API 向后兼容**

确认响应仍包含：

```text
startUrl
keywords
expandedKeywords
depth
render
renderMode
autoNote
siteSearch
pagesCrawled
crawledPages
pagesFailed
totalHits
results
searchId
```

并新增 `discovery`。

- [ ] **步骤 6：检查 Git 范围**

```powershell
git status --short
git diff --stat
```

确认没有 `.env`、缓存、Playwright 浏览器文件、抓取结果或目标站原始敏感
响应进入版本控制。

- [ ] **步骤 7：最终提交（仅在用户明确授权后）**

```powershell
git add discovery tests scripts README.md crawler.py matcher.py `
  sitesearch.py server.py jobs.py static/index.html docs/superpowers
git commit -m "feat: generalize site structure discovery"
```

未获得授权时跳过该步骤，保持工作区改动供用户审阅。
