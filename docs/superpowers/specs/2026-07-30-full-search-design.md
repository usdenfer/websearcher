# 全量搜索设计

日期：2026-07-30

## 1. 目标

将现有受时间和页数双重预算约束的搜索系统，改为不受时间限制、仅设 5000
页软上限的全量搜索模式。BFS 爬虫不限深度和单页链接提取量，Discovery
引擎不限来源和翻页深度，所有 candidate 一次性全部抓取。

## 2. 范围和约束

### 2.1 范围内

- BFS 爬虫深度不限，提取页面中所有同站链接（不再截断为 30 个）。
- Discovery 引擎所有 provider 跑满，不再受单源请求次数限制。
- 所有 candidate 一次性抓取，不再分两批。
- 并发数从 8 提升至 16。
- 单页超时从 10s 提升至 15s，重试次数从 4 次提升至 6 次。
- 全局软上限：5000 页，触发时记录 warning 但不中断。
- 时间预算：86400s（24h），等于永不触发。

### 2.2 范围外

- 绕过验证码、登录墙、付费墙。
- 对超大站点（10 万页以上）做全量覆盖。
- UI 层面的进度展示改造。

## 3. 总体架构

搜索流程不变，但所有截断点和超时点均被放宽或移除：

1. BFS 爬虫 → 不限深度，全链接提取，并发 16。
2. Discovery 引擎 → 所有 provider 不限请求次数，翻页跑满。
3. Candidate 抓取 → 一次性全部调度，不分批。
4. 匹配 → 不变。

## 4. 逐文件变更

### 4.1 `search_budget.py`

| 配置项 | 当前值 | 新值 |
|--------|--------|------|
| `SEARCH_BUDGET_SECONDS` | 180 | 86400 |
| `BASE_BFS_PAGE_BUDGET` | 50 | 5000 |
| zycg 特殊预算 | 300s / 150 / 300 | 移除，统一全量策略 |
| 默认 initial_pages | 80 | 3000 |
| 默认 max_pages | 160 | 5000 |

`make_search_budget()` 移除 zycg 分支，统一返回全量预算。

### 4.2 `discovery/models.py`

`BudgetManager` 默认值：

| 字段 | 当前值 | 新值 |
|------|--------|------|
| `initial_pages` | 60 | 3000 |
| `max_pages` | 120 | 5000 |
| `timeout_seconds` | 120.0 | 86400.0 |

`reserve_html()` 在达到 `page_limit` 时记录 `stop_reason="html-page-budget"` 和 `partial=True`，但不抛异常——允许继续发现候选但停止抓取正文。

### 4.3 `crawler.py`

| 配置项 | 当前值 | 新值 |
|--------|--------|------|
| `MAX_SUBPAGES` | 30 | 移除（`extract_same_site_links` 提取全部） |
| `MAX_TOTAL_PAGES` | 60 | 5000 |
| `RENDER_MAX_PAGES` | 60 | 5000 |
| `RENDER_SUBPAGE_LINKS` | 60 | 5000 |
| `CONCURRENCY` | 8 | 16 |
| `PAGE_TIMEOUT` | 10.0 | 15.0 |
| 重试次数 | 4 | 6 |
| 重试 base_delay | 1.5 | 1.5（不变） |

`extract_same_site_links()` 移除 `limit` 参数默认值和内部 `len(links) >= limit` 截断。`gather_before_deadline` / `await_before_deadline` 逻辑保留不动（deadline 为 24h 后基本不触发）。

### 4.4 `discovery/engine.py`

- `discover_pages()` 默认 `timeout_seconds` 从 120 改为 86400。
- `rank_candidates()` 保持 `per_source=None, per_section=None`（已为 None）。
- Candidate 抓取：移除两批逻辑，改为一次性 `_gather_candidates` 所有 pending。
- `_gather_candidates()` timeout 从 `budget.remaining_seconds()` 改为 `None`。

### 4.5 `discovery/providers.py`

`Provider.get_text()` 默认 `limit` 从 10 改为 500。

各 Provider 内部限制：

| Provider | 参数 | 当前值 | 新值 |
|----------|------|--------|------|
| `SearchProvider` | get_text limit | 10 | 500 |
| `SearchProvider` | additional_requests | 18 | 500 |
| `SitemapProvider` | get_text limit | 5 | 100 |
| `SitemapProvider` | child_urls[:8] | 8 | 移除截断 |
| `FeedProvider` | feed_urls[:8] | 8 | 移除截断 |
| `FeedProvider` | get_text limit | 5 | 100 |
| `CategoryProvider` | category_urls[:16] | 16 | 移除截断 |
| `CategoryProvider` | get_text limit | 20 | 500 |
| `CategoryProvider` | additional_requests | 20 | 500 |
| `FreeCmsApiProvider` | max_pages_per_keyword | 20 | 100 |
| `FreeCmsRecentProvider` | max_pages | 150 | 500 |

### 4.6 `discovery/fetcher.py`

`DiscoveryFetcher` 默认并发从 8 改为 16，`per_host_concurrency` 从 4 改为 8。

`fetch_html_page()` 中 `asyncio.timeout(remaining)` 的超时移除（或设为极大值），`_reserve()` 失败时记录 warning 而不阻塞。

### 4.7 `server.py`

- 移除 `asyncio.timeout(budget.remaining_seconds())` 包裹。
- `_search_budget()` 直接调用 `make_search_budget()`（已统一全量策略）。
- 关键字扩展的 `asyncio.timeout` 保留但放宽至 30s。

## 5. 非目标 / 不变

- AI 摘要和聊天功能不变。
- 定时任务机制不变。
- 前端 UI 不变（可在后续迭代中添加进度展示）。
- 渲染相关逻辑不变（BFS 深度已不限，render discovery 自然覆盖更多）。
- `locator.py`、`matcher.py`、`cache.py` 不变。
- 测试文件仅调整与硬编码配置值相关的断言。
