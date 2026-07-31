# AI 概要卡片标题与正文质量 — 设计文档

日期：2026-08-01
状态：待用户确认
前置：`2026-07-28-ai-agent-design.md`

## 1. 问题描述

AI 概要中的卡片标题和正文无法抓住重点，原因是：

- **AI prompt** (`summarize_prompt`) 只要求 AI 生成 2~5 句整体概述，不要求生成每页卡片
- **卡片标题/正文完全由正则提取**（`_build_structured_summary`、`_make_entry_title`），使用政府采购专用模式，非采购页面回退到取前 150 字全文或 URL 路径
- **前端已支持** `【概述】/【条目】` 格式解析（`parseSummaryEntries`），但 AI 从未被要求输出此格式

## 2. 方案：AI 直接生成结构化卡片

让 AI 在生成整体概述的同时，为每个有效页面生成标题和摘要卡片。正则提取保留作为 AI 输出解析失败时的兜底。

## 3. Prompt 重新设计 (`ai.py`)

修改 `summarize_prompt()`，要求 AI 输出明确的 `【概述】` + `【条目】` 结构：

```
【system】
你是搜索结果分析助手。基于用户给出的站内搜索命中片段，生成两部分：

一、【概述】（必须）：2~5 句话总结整体情况。

二、【条目】（必须）：为每个有效页面生成条目卡片：
  [日期]《标题》
  摘要
  链接

标题要求：提取页面核心事件/项目/公告名称，8~40 字，要具体有区分度，禁止泛词。
摘要要求：1~3 句话提炼关键信息（金额、时间、内容等），不重复标题，不写废话。
格式：条目间空一行，日期 [YYYY-MM-DD]，标题用《》，最后一行是完整 URL。
跳过导航页、联系页等无实质内容页面。
只基于所给内容，不编造。
```

同时将输入预算从 20000 字符调至 30000，给 AI 更多上下文。

## 4. 服务端解析 (`server.py`)

- 新增 `_parse_ai_entries(text: str) -> tuple[str, list[dict]]` 解析 `【概述】/【条目】`
- 在 `sse_summarize()` 中，AI 完成后调用解析，成功则用 AI 条目，失败回退 `_build_entries_from_pages()`
- `parsed` SSE 事件格式不变（`overview` + `entries`）
- 保留现有正则提取函数不动，仅作为 fallback

## 5. Job AI 摘要 (`jobs.py`)

- `_generate_job_ai_summary()`：`max_tokens` 从 2000 调至 3000
- 解析 AI 输出获取 entries；解析失败回退 `_build_job_summary_entries()`

## 6. 前端 (`static/index.html`)

- **无需改动**。`parseSummaryEntries()` 已支持格式，渲染逻辑不变。

## 7. 边界与错误处理

- AI 输出不包含 `【概述】/【条目】` 标记 → 回退正则提取
- AI 输出的个别条目格式错误 → 跳过该条目，保留其他
- AI 调用超时/失败 → 同现有错误处理，不影响基础搜索
- 缓存过期/不存在 → 404，同现有行为

## 8. 测试

- `tests/test_ai.py`：验证新 prompt 内容包含 `【概述】【条目】` 格式说明
- `tests/test_server_ai.py`：验证 `_parse_ai_entries` 解析正确/错误格式
- 既有测试全部保持通过
