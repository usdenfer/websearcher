# FreeCMS Recent Procurement Recall Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在中央政府采购网的 FreeCMS 搜索中，以关键词搜索和近 30 天采购公告列表双通道召回候选，并区分正文强命中与标题弱命中。

**Architecture:** 保留现有 Provider → Candidate → Fetcher → Matcher 流程，在 FreeCMS 适配器中声明近期公告接口，由独立 Provider 遍历日期窗口。Candidate 保存日期与召回证据，DiscoveryRun 将证据交给服务层；服务层继续用正文匹配统计 `totalHits`，再补充标题弱命中与 `weakHits`。FreeCMS 目标站使用独立的 300 秒、150/300 HTML 页预算，其他站点行为不变。

**Tech Stack:** Python 3.12、FastAPI、httpx、BeautifulSoup、pytest、原生 HTML/JavaScript。

---

## 文件结构

- `discovery/models.py`：扩充候选证据、近期诊断和停止原因模型。
- `discovery/adapters.py`：声明中央政府采购网 `selectInfoMore.do` 请求参数并解析列表响应。
- `discovery/providers.py`：实现最近 30 天公告列表 Provider 及日期停止规则。
- `discovery/engine.py`：合并候选证据、接入新 Provider，并把已排序候选交给服务层。
- `matcher.py`：把正文强命中与标题弱命中合并为稳定 API 结果。
- `server.py`：选择 FreeCMS 专用预算，返回 `weakHits` 和扩展诊断。
- `static/index.html`：展示正文命中/标题召回标签和弱命中统计。
- `tests/test_discovery_models.py`：模型与诊断序列化测试。
- `tests/test_discovery_adapters.py`：近期公告接口契约测试。
- `tests/test_discovery_providers.py`：日期窗口、翻页、乱序和失败降级测试。
- `tests/test_discovery_engine.py`：候选证据合并和 Provider 接线测试。
- `tests/test_matcher.py`：强弱判定、去重和排序测试。
- `tests/test_server.py`：预算及响应兼容测试。
- `tests/test_frontend_smoke.py`：前端标记与空结果条件测试。

### Task 1: 扩充候选证据与诊断模型

**Files:**
- Modify: `discovery/models.py:8-101`
- Test: `tests/test_discovery_models.py`
- Test: `tests/test_discovery_engine.py:20-69`

- [ ] **Step 1: 写候选证据和诊断字段的失败测试**

在 `tests/test_discovery_models.py` 增加：

```python
def test_candidate_carries_recent_recall_evidence_without_changing_identity():
    first = Candidate(
        "https://example.test/notice/1",
        "site-search-api",
        title_hint="alpha 公告",
        score=100,
        published_date="2026-07-20",
        source_evidence=("site-search-api",),
    )
    second = Candidate(
        "https://example.test/notice/1",
        "freecms-recent",
        title_hint="alpha 公告",
        score=75,
        published_date="2026-07-20",
        source_evidence=("freecms-recent",),
    )

    assert first == second
    assert first.published_date == "2026-07-20"
    assert first.source_evidence == ("site-search-api",)


def test_discovery_stats_exposes_recent_recall_diagnostics():
    stats = DiscoveryStats(
        profile="freecms",
        recent_window_days=30,
        weak_candidates=2,
        unknown_date_candidates=1,
        stop_reason="date-boundary",
    )

    data = stats.as_dict()
    assert data["recentWindowDays"] == 30
    assert data["weakCandidates"] == 2
    assert data["unknownDateCandidates"] == 1
    assert data["stopReason"] == "date-boundary"


def test_discovery_stats_keeps_highest_priority_stop_reason():
    stats = DiscoveryStats()

    stats.note_stop("date-boundary")
    stats.note_stop("provider-page-limit")
    stats.note_stop("html-page-budget")
    stats.note_stop("time-budget")
    stats.note_stop("date-boundary")

    assert stats.stop_reason == "time-budget"
```

在 `tests/test_discovery_engine.py` 增加合并证据测试：

```python
def test_merge_candidates_unions_sources_and_keeps_best_metadata():
    merged = merge_candidates([
        Candidate(
            "https://x.test/a",
            "freecms-recent",
            title_hint="alpha 公告",
            score=75,
            published_date="2026-07-20",
            source_evidence=("freecms-recent",),
        ),
        Candidate(
            "https://x.test/a",
            "site-search-api",
            keyword="alpha",
            title_hint="alpha 公告",
            score=100,
            source_evidence=("site-search-api",),
        ),
    ])

    assert len(merged) == 1
    assert merged[0].source == "site-search-api"
    assert merged[0].published_date == "2026-07-20"
    assert merged[0].source_evidence == (
        "freecms-recent",
        "site-search-api",
    )
```

- [ ] **Step 2: 运行测试并确认因缺少字段或证据合并而失败**

Run:

```powershell
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest tests/test_discovery_models.py tests/test_discovery_engine.py::test_merge_candidates_unions_sources_and_keeps_best_metadata -q
```

Expected: FAIL，错误指出 `Candidate` 或 `DiscoveryStats` 不接受新增字段，或合并结果未保留证据。

- [ ] **Step 3: 实现模型字段和稳定序列化**

在 `Candidate` 末尾增加不参与 URL 身份比较的字段：

```python
published_date: str | None = field(default=None, compare=False)
source_evidence: tuple[str, ...] = field(default=(), compare=False)
```

在 `DiscoveryStats` 增加：

```python
recent_window_days: int | None = None
weak_candidates: int = 0
unknown_date_candidates: int = 0
stop_reason: str | None = None
```

并增加确定性的停止原因优先级：

```python
_STOP_REASON_PRIORITY = {
    None: 0,
    "date-boundary": 10,
    "provider-page-limit": 20,
    "channel-failure": 30,
    "html-page-budget": 40,
    "time-budget": 50,
}

def note_stop(self, reason: str) -> None:
    current = _STOP_REASON_PRIORITY.get(self.stop_reason, 0)
    incoming = _STOP_REASON_PRIORITY.get(reason, 0)
    if incoming > current:
        self.stop_reason = reason
```

所有 Provider 和引擎必须调用 `note_stop()`，不得直接覆盖 `stop_reason`，避免
并行通道最后写入者造成不确定结果。

在 `as_dict()` 的现有返回值中稳定加入：

```python
"recentWindowDays": self.recent_window_days,
"weakCandidates": self.weak_candidates,
"unknownDateCandidates": self.unknown_date_candidates,
"stopReason": self.stop_reason,
```

更新 `merge_candidates()`：仍由最高分 Candidate 决定 `source`、`keyword`、
`title_hint`、`score`、`requires_render` 和 `section`；对所有同 URL Candidate 的
非空 `published_date` 取最高分候选的值，最高分无日期时取已有可靠日期；
`source_evidence` 与每个 Candidate 的 `source` 合并、去重并按字典序保存。

- [ ] **Step 4: 运行模型和引擎合并测试**

Run:

```powershell
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest tests/test_discovery_models.py tests/test_discovery_engine.py::test_merge_candidates_unions_sources_and_keeps_best_metadata -q
```

Expected: PASS。

- [ ] **Step 5: 提交模型变更**

```powershell
git add discovery/models.py discovery/engine.py tests/test_discovery_models.py tests/test_discovery_engine.py
git commit -m "feat: preserve FreeCMS recall evidence"
```

### Task 2: 声明并解析近期公告接口

**Files:**
- Modify: `discovery/adapters.py:79-143`
- Test: `tests/test_discovery_adapters.py`

- [ ] **Step 1: 写接口声明和解析失败测试**

在 `tests/test_discovery_adapters.py` 增加：

```python
def test_zycg_adapter_declares_recent_notice_api():
    adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
    spec = adapter.recent_notice_spec()

    assert spec is not None
    assert spec.url.endswith(
        "/freecms/rest/v1/notice/selectInfoMore.do"
    )
    assert spec.params_for("", page=2) == {
        "siteId": "6f5243ee-d4d9-4b69-abbd-1e40576ccd7d",
        "channel": "d0e7c5f4-b93e-4478-b7fe-61110bb47fd5",
        "pageSize": "15",
        "implementWay": "1",
        "noticeType": "1,2,3,31,32,52,57,61",
        "title": "",
        "currPage": 2,
    }


def test_non_zycg_freecms_has_no_site_specific_recent_notice_api():
    adapter = FreeCmsAdapter("https://freecms.example.test/")
    assert adapter.recent_notice_spec() is None


def test_freecms_recent_response_accepts_pageurl_case_variants():
    adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
    ok, rows, warning = adapter.parse_recent_response(json.dumps({
        "code": 200,
        "data": [
            {
                "pageurl": "/notice/1",
                "title": "第一条",
                "addtimeStr": "2026-07-20 10:00:00",
            },
            {
                "pageUrl": "/notice/2",
                "title": "第二条",
                "addtimeStr": "2026-07-19",
            },
        ],
    }))

    assert ok is True
    assert [row["pageUrl"] for row in rows] == [
        "/notice/1",
        "/notice/2",
    ]
    assert warning == ""
```

- [ ] **Step 2: 运行适配器测试并确认失败**

Run:

```powershell
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest tests/test_discovery_adapters.py -q
```

Expected: FAIL，`recent_notice_spec` 或 `parse_recent_response` 尚不存在。

- [ ] **Step 3: 实现站点限定的接口契约**

在 `FreeCmsAdapter` 增加：

```python
def recent_notice_spec(self) -> SearchSpec | None:
    host = (urlsplit(self.origin).hostname or "").lower().rstrip(".")
    if host != "zycg.gov.cn" and not host.endswith(".zycg.gov.cn"):
        return None
    return SearchSpec(
        "freecms-recent",
        self.origin + "/freecms/rest/v1/notice/selectInfoMore.do",
        "title",
        "currPage",
        (
            ("siteId", "6f5243ee-d4d9-4b69-abbd-1e40576ccd7d"),
            ("channel", "d0e7c5f4-b93e-4478-b7fe-61110bb47fd5"),
            ("pageSize", "15"),
            ("implementWay", "1"),
            ("noticeType", "1,2,3,31,32,52,57,61"),
        ),
    )
```

实现 `parse_recent_response()`：复用与 `parse_api_response()` 相同的业务码和
列表结构校验；逐行复制字典，将 `pageurl`、`pageURL` 或 `pageUrl` 统一为
`pageUrl`，缺少 URL 的记录保留给 Provider 跳过，错误信息使用
“FreeCMS 近期公告接口……”前缀。

- [ ] **Step 4: 运行适配器测试**

Run:

```powershell
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest tests/test_discovery_adapters.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交适配器变更**

```powershell
git add discovery/adapters.py tests/test_discovery_adapters.py
git commit -m "feat: declare FreeCMS recent notice endpoint"
```

### Task 3: 实现 30 天公告列表 Provider

**Files:**
- Modify: `discovery/providers.py:478-570`
- Test: `tests/test_discovery_providers.py`

- [ ] **Step 1: 写日期边界、乱序和失败降级测试**

在 `tests/test_discovery_providers.py` 导入 `date`、`FreeCmsRecentProvider`，
并增加以下完整测试：

```python
async def _recent_candidates(responses):
    requested_pages = []
    response_iter = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(
                200,
                text="<html></html>",
                headers={"set-cookie": "JSESSIONID=warm; Path=/"},
            )
        requested_pages.append(int(request.url.params["currPage"]))
        return httpx.Response(200, json=next(response_iter))

    stats = DiscoveryStats()
    adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
    policy = adapter.domain_policy(adapter.origin)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        candidates = await FreeCmsRecentProvider(
            client,
            BudgetManager(timeout_seconds=30),
            stats,
            policy,
            adapter,
            today=date(2026, 7, 29),
        ).discover(["alpha"])
    return candidates, requested_pages, stats


def _notice(url, title, published):
    return {
        "pageUrl": url,
        "title": title,
        "addtimeStr": published,
    }


def test_freecms_recent_provider_stops_at_30_day_boundary():
    responses = [
        {"code": 200, "data": [
            _notice("/n/1", "第一条", "2026-07-29"),
            _notice("/n/2", "第二条", "2026-07-01"),
        ]},
        {"code": 200, "data": [
            _notice("/n/3", "截止日", "2026-06-29"),
            _notice("/n/4", "过期", "2026-06-28"),
        ]},
    ]
    candidates, pages, stats = asyncio.run(
        _recent_candidates(responses)
    )

    assert pages == [1, 2]
    assert [item.url for item in candidates] == [
        "https://www.zycg.gov.cn/n/1",
        "https://www.zycg.gov.cn/n/2",
        "https://www.zycg.gov.cn/n/3",
    ]
    assert stats.stop_reason == "date-boundary"
    assert "freecms-recent" in stats.sources_succeeded


def test_freecms_recent_provider_keeps_scanning_out_of_order_dates():
    responses = [
        {"code": 200, "data": [
            _notice("/n/old", "过期", "2026-06-28"),
            _notice("/n/new", "回升", "2026-07-10"),
        ]},
        {"code": 200, "data": [
            _notice("/n/next", "下一页", "2026-07-09"),
        ]},
        {"code": 200, "data": []},
    ]
    candidates, pages, stats = asyncio.run(
        _recent_candidates(responses)
    )

    assert pages == [1, 2, 3]
    assert [item.url for item in candidates] == [
        "https://www.zycg.gov.cn/n/new",
        "https://www.zycg.gov.cn/n/next",
    ]
    assert stats.stop_reason is None


def test_freecms_recent_failure_does_not_mark_channel_success():
    candidates, pages, stats = asyncio.run(_recent_candidates([
        {"code": -1, "msg": "公告列表查询失败"},
    ]))

    assert candidates == []
    assert pages == [1]
    assert "freecms-recent" not in stats.sources_succeeded
    assert stats.warnings == [
        "freecms-recent: 公告列表查询失败"
    ]
```

每个 Mock handler 都先响应首页预热请求，再按 `currPage` 返回 JSON；断言请求
参数包含 Task 2 的固定参数。

- [ ] **Step 2: 运行新 Provider 测试并确认失败**

Run:

```powershell
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest tests/test_discovery_providers.py -k "freecms_recent" -q
```

Expected: FAIL，`FreeCmsRecentProvider` 尚不存在。

- [ ] **Step 3: 实现 Provider**

在 `discovery/providers.py` 增加：

```python
class FreeCmsRecentProvider(Provider):
    source = "freecms-recent"

    def __init__(
        self,
        client,
        budget,
        stats,
        policy,
        adapter,
        *,
        today: date | None = None,
        max_pages: int = 80,
    ):
        super().__init__(client, budget, stats, policy)
        self.adapter = adapter
        self.today = today or date.today()
        self.max_pages = max_pages
```

`discover()` 的完整规则：

1. `recent_notice_spec()` 为 `None` 时直接返回空列表，不标记 tried/succeeded。
2. 设置 `stats.recent_window_days = 30`，调用与搜索 Provider 相同的会话预热。
3. 截止日为 `today - timedelta(days=30)`，截止日当天包含。
4. 每页先用 `get_text(spec.url, limit=self.max_pages,
   params=spec.params_for("", page), counts_as_html=False)` 预留
   `freecms-recent` Provider 配额，再调用 `parse_recent_response()`。
5. `addtimeStr[:10]` 用 `date.fromisoformat()` 解析；解析失败视为未知日期，
   `unknown_date_candidates` 加一，但不触发日期停止。
6. URL 使用 `pageUrl`，经 `urljoin`、`normalize_candidate_url` 和
   `url_allowed` 校验。
7. Candidate 的 `title_hint` 来自标题，`published_date` 为 ISO 日期，
   `source_evidence=("freecms-recent",)`，基础分 75。
8. 已知过期记录不加入结果。只有此前页面均保持日期非递增，才在首次过期记录
   处调用 `stats.note_stop("date-boundary")` 并停止；一旦检测到日期回升，则标记
   列表乱序，后续只逐条过滤。
9. 空页正常结束；业务失败记录脱敏 warning；到达 80 页设置
   `stats.note_stop("provider-page-limit")`；时间耗尽设置 partial 并调用
   `stats.note_stop("time-budget")`。
10. 至少有一页业务成功才把 `freecms-recent` 加入 `sources_succeeded`。

把会话预热抽成模块级 `_warm_freecms_session(client, budget, stats, source,
origin)`，让
`FreeCmsApiProvider` 和 `FreeCmsRecentProvider` 共用相同的超时、取消和脱敏
warning 逻辑；每个 Provider 可独立预热，保证并发启动时没有顺序依赖。

- [ ] **Step 4: 运行 FreeCMS Provider 测试**

Run:

```powershell
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest tests/test_discovery_providers.py -k "freecms" -q
```

Expected: PASS。

- [ ] **Step 5: 提交 Provider 变更**

```powershell
git add discovery/providers.py tests/test_discovery_providers.py
git commit -m "feat: recall recent FreeCMS notices"
```

### Task 4: 扩展关键词通道的翻页和日期过滤

**Files:**
- Modify: `discovery/providers.py:478-570`
- Test: `tests/test_discovery_providers.py`

- [ ] **Step 1: 写关键词接口翻页失败测试**

在 `tests/test_discovery_providers.py` 增加：

```python
def test_freecms_search_pages_each_keyword_and_filters_old_rows():
    async def run():
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/":
                return httpx.Response(200, text="<html></html>")
            requests.append((
                request.url.params["title"],
                int(request.url.params["currPage"]),
            ))
            page = int(request.url.params["currPage"])
            rows = {
                1: [{
                    "pageUrl": "/n/1",
                    "title": "alpha 第一条",
                    "addtimeStr": "2026-07-20",
                }],
                2: [{
                    "pageUrl": "/n/old",
                    "title": "alpha 过期",
                    "addtimeStr": "2026-06-28",
                }],
            }.get(page, [])
            return httpx.Response(
                200,
                json={"code": 200, "data": rows},
            )

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsApiProvider(
                client,
                BudgetManager(timeout_seconds=30),
                stats,
                adapter.domain_policy(adapter.origin),
                adapter,
                today=date(2026, 7, 29),
            ).discover(["alpha"])

        assert requests == [("alpha", 1), ("alpha", 2)]
        assert [item.url for item in result] == [
            "https://www.zycg.gov.cn/n/1"
        ]
        assert result[0].published_date == "2026-07-20"
        assert result[0].source_evidence == ("site-search-api",)

    asyncio.run(run())


def test_freecms_search_keeps_unknown_date_candidate():
    async def run():
        pages = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/":
                return httpx.Response(200, text="<html></html>")
            page = int(request.url.params["currPage"])
            pages.append(page)
            rows = [{
                "pageUrl": "/n/unknown",
                "title": "alpha 无日期公告",
            }] if page == 1 else []
            return httpx.Response(
                200,
                json={"code": 200, "data": rows},
            )

        stats = DiscoveryStats()
        adapter = FreeCmsAdapter("https://www.zycg.gov.cn/")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            result = await FreeCmsApiProvider(
                client,
                BudgetManager(timeout_seconds=30),
                stats,
                adapter.domain_policy(adapter.origin),
                adapter,
                today=date(2026, 7, 29),
            ).discover(["alpha"])

        assert pages == [1, 2]
        assert len(result) == 1
        assert result[0].published_date is None
        assert stats.unknown_date_candidates == 1

    asyncio.run(run())
```

- [ ] **Step 2: 运行关键词 Provider 测试并确认失败**

Run:

```powershell
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest tests/test_discovery_providers.py -k "freecms_search_pages or freecms_search_keeps_unknown" -q
```

Expected: FAIL，当前 Provider 只请求第一页，也不保存发布日期。

- [ ] **Step 3: 实现每关键词最多 10 页**

给 `FreeCmsApiProvider.__init__()` 增加仅用于确定性测试的
`today: date | None = None` 和 `max_pages_per_keyword: int = 10`。对最多 6 个
关键词分别执行页码 `1..max_pages_per_keyword`：

- 调用 `spec.params_for(keyword, page)`。
- Provider 总请求配额为
  `len(keywords[:6]) * max_pages_per_keyword`。
- 业务成功的空列表停止当前关键词翻页。
- 已知日期早于 `today - timedelta(days=30)` 的行不进入 Candidate。
- 当前页日期可靠且已越过截止日时停止当前关键词；日期缺失时继续到空页或页数
  上限。
- 日期未知的候选保留并增加 `unknown_date_candidates`。
- Candidate 保存 ISO `published_date` 和
  `source_evidence=("site-search-api",)`。
- 达到页数上限且最后一页仍非空时调用
  `stats.note_stop("provider-page-limit")`。
- 一个关键词业务失败不阻止后续关键词；任意页业务成功即可维持现有
  `sources_succeeded` 语义。

- [ ] **Step 4: 运行所有 FreeCMS Provider 测试**

Run:

```powershell
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest tests/test_discovery_providers.py -k "freecms" -q
```

Expected: PASS。

- [ ] **Step 5: 提交关键词翻页**

```powershell
git add discovery/providers.py tests/test_discovery_providers.py
git commit -m "feat: paginate FreeCMS keyword search"
```

### Task 5: 接入双通道并保留已排序候选

**Files:**
- Modify: `discovery/engine.py:19-390`
- Modify: `discovery/__init__.py`
- Test: `tests/test_discovery_engine.py`

- [ ] **Step 1: 写引擎接线和 DiscoveryRun 证据测试**

增加测试，断言：

```python
def test_freecms_engine_runs_search_and_recent_providers(monkeypatch):
    url = "https://www.zycg.gov.cn/notice/1"
    context, providers, fetchers = _install_engine_fakes(
        monkeypatch,
        batches={
            "site-search-api": [
                Candidate(
                    url,
                    "site-search-api",
                    keyword="alpha",
                    title_hint="alpha 公告",
                    score=100,
                )
            ],
            "freecms-recent": [
                Candidate(
                    url,
                    "freecms-recent",
                    title_hint="alpha 公告",
                    score=75,
                    published_date="2026-07-20",
                )
            ],
        },
    )
    base = CrawlResult(pages=[
        CrawledPage(
            "https://www.zycg.gov.cn/",
            "<html>searchAll.do</html>",
        )
    ])

    run = _run(discover_pages(
        "https://www.zycg.gov.cn/",
        ["alpha"],
        base,
        1,
        "off",
    ))

    assert context.exited is True
    assert {item[0] for item in providers} >= {
        "site-search-api", "freecms-recent",
    }
    assert fetchers[0].urls == [url]
    assert len(run.candidates) == 1
    assert run.candidates[0].source_evidence == (
        "freecms-recent", "site-search-api",
    )


def test_generic_engine_does_not_run_freecms_recent_provider(monkeypatch):
    _context, providers, _fetchers = _install_engine_fakes(monkeypatch)
    base = CrawlResult(pages=[
        CrawledPage("https://x.test/", "<html>普通首页</html>")
    ])

    _run(discover_pages(
        "https://x.test/",
        ["alpha"],
        base,
        1,
        "off",
    ))

    assert "freecms-recent" not in {
        item[0] for item in providers
    }
```

同步扩充 `_install_engine_fakes()`，用
`provider_type("freecms-recent")` monkeypatch
`engine.FreeCmsRecentProvider`，确保两个测试沿用现有 fake Provider 记录结构。

- [ ] **Step 2: 运行引擎新测试并确认失败**

Run:

```powershell
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest tests/test_discovery_engine.py -k "freecms_engine or generic_engine" -q
```

Expected: FAIL，新 Provider 未接入且 `DiscoveryRun` 没有 `candidates`。

- [ ] **Step 3: 实现接线和证据传递**

给 `DiscoveryRun` 增加向后兼容默认字段：

```python
@dataclass
class DiscoveryRun:
    pages: list[CrawledPage]
    failed: list[dict]
    stats: DiscoveryStats
    candidates: list[Candidate] = field(default_factory=list)
```

导入并在 FreeCMS 分支同时追加：

```python
providers.extend([
    FreeCmsApiProvider(client, budget, stats, policy, adapter),
    FreeCmsRecentProvider(client, budget, stats, policy, adapter),
])
```

返回 `DiscoveryRun` 时把 `ranked` 传入 `candidates`。保持所有现有三参数
`DiscoveryRun` 构造兼容。`discovery/__init__.py` 继续延迟导入公开类型，
不引入循环依赖。

- [ ] **Step 4: 运行引擎测试**

Run:

```powershell
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest tests/test_discovery_engine.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交引擎接线**

```powershell
git add discovery/engine.py discovery/__init__.py tests/test_discovery_engine.py
git commit -m "feat: combine FreeCMS recall channels"
```

### Task 6: 生成强弱命中结果

**Files:**
- Modify: `matcher.py:210-290`
- Modify: `server.py:128-265`
- Test: `tests/test_matcher.py`
- Test: `tests/test_server.py`

- [ ] **Step 1: 写强弱判定和 API 兼容失败测试**

在 `tests/test_matcher.py` 增加：

```python
def test_match_body_with_recall_returns_strong_before_weak():
    pages = [
        CrawledPage(
            "https://x.test/strong",
            "<title>普通标题</title><main>alpha 正文</main>",
        )
    ]
    candidates = [
        Candidate(
            "https://x.test/weak",
            "site-search-api",
            title_hint="alpha 标题公告",
            published_date="2026-07-20",
        ),
        Candidate(
            "https://x.test/strong",
            "freecms-recent",
            title_hint="普通标题",
            published_date="2026-07-19",
        ),
        Candidate(
            "https://x.test/no-match",
            "freecms-recent",
            title_hint="普通公告",
            published_date="2026-07-21",
        ),
    ]

    results, strong_hits, weak_hits = match_body_with_recall(
        pages, ["alpha"], candidates
    )

    assert strong_hits == 1
    assert weak_hits == 1
    assert [item["matchStrength"] for item in results] == [
        "strong", "weak",
    ]
    assert results[1]["hits"][0]["kind"] == "title-recall"
```

在 `tests/test_server.py` 增加一个 `fake_discover()`，返回一篇正文强命中页面和
一条未抓取但标题命中的 Candidate，断言：

```python
assert data["totalHits"] == 1
assert data["weakHits"] == 1
assert [item["matchStrength"] for item in data["results"]] == [
    "strong", "weak",
]
assert data["discovery"]["weakCandidates"] == 1
```

- [ ] **Step 2: 运行 Matcher 和 Server 新测试并确认失败**

Run:

```powershell
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest tests/test_matcher.py tests/test_server.py -k "recall or match_strength or weak" -q
```

Expected: FAIL，缺少 `match_body_with_recall`、`weakHits` 或
`matchStrength`。

- [ ] **Step 3: 实现强弱匹配**

在 `matcher.py` 增加
`match_body_with_recall(pages, keywords, candidates)`：

- 先调用正文匹配逻辑；每个正文结果增加 `matchStrength="strong"`。
- 用规范化 URL 去重强结果和候选；正文强结果永远覆盖同 URL 弱结果。
- 对没有强结果的 Candidate，以 `title_hint` 对所有去空、稳定去重关键词执行
  `make_snippet()`。
- 标题命中先调用 `make_snippet(candidate.title_hint, keyword)`，再用返回的
  `snippet` 构造 `Hit(kind="title-recall", snippet=snippet,
  keyword=keyword, href=candidate.url)`；页面标题为 `title_hint`，允许正文
  抓取失败时返回。
- 无标题命中的近期列表 Candidate 不返回。
- FreeCMS 结果按强度、已知日期新到旧、分数高到低、URL 排序；日期未知排在
  同强度已知日期之后。
- 返回 `(results, strong_hit_count, weak_result_count)`；强命中数仍是正文
  Hit 数，弱命中数是弱结果条数。

在 `server.py` 中，仅当 `discovery_run.stats.profile == "freecms"` 时调用新函数；
其他 profile 继续调用现有正文匹配并给结果补
`matchStrength="strong"`、`weakHits=0`。将实际弱结果数写入
`discovery_run.stats.weak_candidates`，响应增加 `"weakHits"`。

- [ ] **Step 4: 运行匹配和服务测试**

Run:

```powershell
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest tests/test_matcher.py tests/test_server.py tests/test_jobs.py tests/test_sitesearch.py -q
```

Expected: PASS，旧客户端的 `totalHits` 断言不变。

- [ ] **Step 5: 提交强弱匹配**

```powershell
git add matcher.py server.py tests/test_matcher.py tests/test_server.py
git commit -m "feat: expose strong and weak FreeCMS matches"
```

### Task 7: 应用 FreeCMS 专用预算和停止诊断

**Files:**
- Modify: `server.py:133-160`
- Modify: `discovery/engine.py:360-390`
- Test: `tests/test_server.py`
- Test: `tests/test_discovery_engine.py`

- [ ] **Step 1: 写预算选择和停止原因失败测试**

在 `tests/test_server.py` 增加：

```python
def test_zycg_search_uses_freecms_budget():
    budget = server._search_budget(
        "https://www.zycg.gov.cn/",
        archive_mode=False,
        started_at=100.0,
    )

    assert budget.initial_pages == 150
    assert budget.max_pages == 300
    assert budget.timeout_seconds == 300


def test_non_freecms_search_keeps_default_budget():
    budget = server._search_budget(
        "https://example.test/",
        archive_mode=False,
        started_at=100.0,
    )

    assert budget.initial_pages == 60
    assert budget.max_pages == 120
    assert budget.timeout_seconds == server.SEARCH_BUDGET_SECONDS
```

在 `tests/test_discovery_engine.py` 增加：当 pending 超过实际调度容量时
`partial=True` 且 `stop_reason="html-page-budget"`；当时间耗尽时为
`"time-budget"`，不得覆盖 Provider 已记录的正常 `"date-boundary"`，
除非搜索确实因更高优先级预算停止。

- [ ] **Step 2: 运行预算测试并确认失败**

Run:

```powershell
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest tests/test_server.py tests/test_discovery_engine.py -k "freecms_budget or stop_reason or default_budget" -q
```

Expected: FAIL，当前 zycg 仍使用 60/120/120 或缺少停止原因。

- [ ] **Step 3: 实现预算工厂和停止原因优先级**

在 `server.py` 增加纯函数：

```python
def _search_budget(
    start_url: str,
    archive_mode: bool,
    started_at: float,
) -> BudgetManager:
    if archive_mode:
        return BudgetManager(
            initial_pages=ARCHIVE_MAX_PAGES,
            max_pages=ARCHIVE_MAX_PAGES,
            timeout_seconds=ARCHIVE_BUDGET_SECONDS,
            started_at=started_at,
        )
    host = (urlsplit(start_url).hostname or "").lower().rstrip(".")
    if host == "zycg.gov.cn" or host.endswith(".zycg.gov.cn"):
        return BudgetManager(
            initial_pages=150,
            max_pages=300,
            timeout_seconds=300,
            started_at=started_at,
        )
    return BudgetManager(
        initial_pages=60,
        max_pages=120,
        timeout_seconds=SEARCH_BUDGET_SECONDS,
        started_at=started_at,
    )
```

用该函数替换 `search()` 内联预算构造。在引擎结束处按优先级设置停止原因：
通过 `stats.note_stop()` 保持
`time-budget` > `html-page-budget` > `channel-failure` >
`provider-page-limit` > `date-boundary`；只有时间或页面预算导致未完成时设置
`partial=True`，正常到达日期边界不视为部分结果。

- [ ] **Step 4: 运行预算及现有自动预算测试**

Run:

```powershell
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest tests/test_server.py tests/test_server_auto.py tests/test_discovery_engine.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交预算变更**

```powershell
git add server.py discovery/engine.py tests/test_server.py tests/test_discovery_engine.py
git commit -m "feat: expand FreeCMS search budget"
```

### Task 8: 展示强弱命中和诊断

**Files:**
- Modify: `static/index.html:180-190,311-380`
- Test: `tests/test_frontend_smoke.py`

- [ ] **Step 1: 写前端失败测试**

在 `tests/test_frontend_smoke.py` 增加：

```python
def test_frontend_labels_strong_and_weak_matches():
    html = STATIC_INDEX.read_text(encoding="utf-8")
    assert "data.weakHits" in html
    assert 'page.matchStrength === "weak"' in html
    assert "正文命中" in html
    assert "标题召回" in html
    assert '"title-recall"' in html
    assert '"freecms-recent"' in html


def test_frontend_does_not_show_empty_state_when_only_weak_results_exist():
    html = STATIC_INDEX.read_text(encoding="utf-8")
    assert "data.results.length === 0" in html
```

- [ ] **Step 2: 运行前端静态测试并确认失败**

Run:

```powershell
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest tests/test_frontend_smoke.py -k "strong_and_weak or only_weak" -q
```

Expected: FAIL，页面尚未渲染弱命中。

- [ ] **Step 3: 实现最小前端扩展**

在 `KIND_LABELS` 增加：

```javascript
"title-recall": "标题召回"
```

在 `DISCOVERY_WARNING_CHANNELS` 增加 `"freecms-recent"`，让该通道的脱敏错误
能够以真实通道名展示。

摘要行追加 `，标题召回 ${data.weakHits || 0} 条`。空结果判断从
`data.totalHits === 0` 改为 `data.results.length === 0`，确保只有弱结果时仍
显示卡片。每个结果卡标题后增加：

```javascript
const strengthLabel = page.matchStrength === "weak"
  ? "标题召回" : "正文命中";
```

并将经过 `escapeHtml(strengthLabel)` 的 badge 放在标题旁。弱命中不显示
“所在页定位”，因为片段并非正文；现有 `title-recall` kind 会自然走该分支。

- [ ] **Step 4: 运行前端测试**

Run:

```powershell
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest tests/test_frontend_smoke.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交前端变更**

```powershell
git add static/index.html tests/test_frontend_smoke.py
git commit -m "feat: label FreeCMS recall strength"
```

### Task 9: 完整回归与手动冒烟入口

**Files:**
- Modify: `scripts/smoke_discovery.py`
- Test: `tests/test_smoke_discovery.py`
- Verify: all tests

- [ ] **Step 1: 写冒烟摘要失败测试**

在 `tests/test_smoke_discovery.py` 增加响应包含 `weakHits`、
`recentWindowDays` 和 `stopReason` 的 fixture，断言 `summarize_response()`
输出：

```python
summary = json.loads(summarize_response(response))
assert summary["strongHits"] == 2
assert summary["weakHits"] == 1
assert summary["recentWindowDays"] == 30
assert summary["stopReason"] == "date-boundary"
```

更新 `test_async_main_posts_to_normalized_api_and_prints_safe_summary` 对 timeout
的断言为 `330`，覆盖 FreeCMS 300 秒服务预算和网络收尾时间。

- [ ] **Step 2: 运行冒烟脚本测试并确认失败**

Run:

```powershell
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest tests/test_smoke_discovery.py -q
```

Expected: FAIL，摘要尚未暴露近期召回信息。

- [ ] **Step 3: 扩充冒烟摘要**

在 `summarize_response()` 返回字典中加入：

```python
"strongHits": payload.get("totalHits", 0),
"weakHits": payload.get("weakHits", 0),
"recentWindowDays": discovery.get("recentWindowDays"),
"stopReason": discovery.get("stopReason"),
```

将 `async_main()` 的 httpx timeout 从 130 秒提高到 330 秒。保持命令行参数和
现有输出兼容，不把真实站点请求加入 CI。

- [ ] **Step 4: 运行相关测试和完整测试**

Run:

```powershell
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest tests/test_smoke_discovery.py -q
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest -q
```

Expected: 两条命令均 PASS；完整套件零失败。

- [ ] **Step 5: 检查范围和敏感信息**

Run:

```powershell
rg -n "T[B]D|T[O]DO|王丹莉|JSESSIONID=|Cookie:|Authorization:" discovery matcher.py server.py static tests scripts
git diff --check master...HEAD
git status --short
```

Expected:

- 无占位符。
- 不增加真实姓名或具体业务关键词特例。
- 生产代码和测试输出不包含真实 Cookie、认证头或密钥。
- `git diff --check` 无错误。
- `git status --short` 只显示本任务最后待提交文件。

- [ ] **Step 6: 提交冒烟摘要**

```powershell
git add scripts/smoke_discovery.py tests/test_smoke_discovery.py
git commit -m "test: report FreeCMS recall diagnostics"
```

- [ ] **Step 7: 最终验证提交状态**

Run:

```powershell
D:\WebProjects\web_keyword_catcher\.venv\Scripts\python.exe -m pytest -q
git status --short --branch
git log --oneline --decorate master..HEAD
```

Expected: 完整测试零失败；工作区干净；分支仅包含本实施计划及其功能提交。
