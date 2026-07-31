# AI 概要卡片标题与正文质量 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 AI 直接生成结构化卡片（标题+摘要），替代仅依赖正则提取的方式，提升卡片标题与正文的质量。

**Architecture:** 修改 `summarize_prompt()` 要求 AI 输出 `【概述】`+`【条目】` 格式；在 `ai.py` 新增 `parse_ai_entries()` 解析此格式；`server.py` 和 `jobs.py` 优先使用 AI 生成的 entries，解析失败时回退到正则提取。

**Tech Stack:** Python 3.12、DeepSeek API（OpenAI 兼容）、pytest

---

### Task 1: ai.py — 新 prompt + 结构化解析器

**Files:**
- Modify: `ai.py`
- Test: `tests/test_ai.py`

**Interfaces:**
- Consumes: 无新依赖
- Produces:
  - `summarize_prompt()` 系统提示词改为要求 `【概述】/【条目】` 格式，输入预算 20000 → 30000
  - `parse_ai_entries(text: str) -> tuple[str, list[dict]]` 公开函数

- [ ] **Step 1: Write the failing test**

在 `tests/test_ai.py` 末尾追加：

```python
from ai import parse_ai_entries

def test_parse_ai_entries_full_format():
    text = """【概述】
本次搜索共命中3条结果，涉及两个采购项目和一个招标公告，时间跨度为2025年1月。

【条目】
[2025-01-15]《XX市智慧交通系统建设项目》
预算800万元，预计2025年3月启动采购，主要包括智慧交通指挥中心建设和信号灯联网改造。
https://example.com/page1

[2025-01-10]《YY服务中心装修改造》
预算200万，装修改造YY服务中心。
https://example.com/page2
"""
    overview, entries = parse_ai_entries(text)
    assert "3条结果" in overview
    assert len(entries) == 2
    assert entries[0]["title"] == "XX市智慧交通系统建设项目"
    assert entries[0]["date"] == "2025-01-15"
    assert "预算800万" in entries[0]["summary"]
    assert entries[0]["link"] == "https://example.com/page1"
    assert entries[1]["title"] == "YY服务中心装修改造"
    assert entries[1]["link"] == "https://example.com/page2"


def test_parse_ai_entries_no_date():
    text = """【概述】
简要总结。

【条目】
《某项目公告》
摘要内容。
https://example.com/p
"""
    _, entries = parse_ai_entries(text)
    assert len(entries) == 1
    assert entries[0]["date"] == ""
    assert entries[0]["title"] == "某项目公告"


def test_parse_ai_entries_skip_malformed():
    text = """【概述】
总结。

【条目】
[2025-01-15]《有效标题1》
有效摘要。
https://example.com/1

无书名号的不完整条目
这段应该被跳过。
https://example.com/skip

《有效标题2》
有效摘要2。
https://example.com/2
"""
    _, entries = parse_ai_entries(text)
    assert len(entries) == 2
    assert entries[0]["title"] == "有效标题1"
    assert entries[1]["title"] == "有效标题2"


def test_parse_ai_entries_no_markers():
    text = "这是纯文本概述，没有格式标记。"
    overview, entries = parse_ai_entries(text)
    assert "纯文本概述" in overview
    assert entries == []


def test_parse_ai_entries_overview_only():
    text = """【概述】
只有概述没有条目部分。
"""
    overview, entries = parse_ai_entries(text)
    assert "只有概述" in overview
    assert entries == []


def test_summarize_prompt_contains_structured_format():
    msgs = summarize_prompt(["测试"], [{"pageUrl": "http://h/p",
            "pageTitle": "测试页", "hits": [{"snippet": "x" * 100}]}])
    system = msgs[0]["content"]
    assert "【概述】" in system
    assert "【条目】" in system
    assert "《" in system
    # budget 应提高到 30000
    assert "30000" not in system  # 不在 prompt 中暴露实现细节
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ai.py -v -k "parse_ai_entries or summarize_prompt_contains"`

Expected: FAIL — `parse_ai_entries` 不存在，`summarize_prompt` 的 system prompt 不包含 `【概述】`

- [ ] **Step 3: Write minimal implementation**

修改 `ai.py`：

```python
# 在 summarize_prompt() 之前追加公开解析函数
import re  # 加到文件顶部现有 import 区域


def parse_ai_entries(text: str) -> tuple[str, list[dict]]:
    """Parse AI structured output: 【概述】+【条目】 into overview and entries.
    Returns (overview, entries). Entries is empty list when parsing fails."""
    entries: list[dict] = []

    ov_match = re.search(r'【概述】\s*([\s\S]*?)(?=【条目】|$)', text)
    en_match = re.search(r'【条目】\s*([\s\S]*)$', text)

    overview = ov_match.group(1).strip() if ov_match else ""
    if not en_match:
        return overview, []

    blocks = re.split(r'\n\s*\n', en_match.group(1).strip())
    for block in blocks:
        lines = [ln.strip() for ln in block.split('\n') if ln.strip()]
        if len(lines) < 2:
            continue

        first_line = lines[0]
        date_m = re.search(r'\[([^\]]*)\]', first_line)
        date = date_m.group(1) if date_m else ""
        title_m = re.search(r'《([^》]*)》', first_line)
        if not title_m:
            continue
        title = title_m.group(1).strip()
        if not title:
            continue

        last_line = lines[-1]
        link = last_line if re.match(r'^https?://', last_line) else ""
        summary_lines = lines[1:-1] if link else lines[1:]
        if not link and last_line != first_line:
            summary_lines.append(last_line)
        summary = ' '.join(summary_lines).strip()

        if title and (summary or link):
            entries.append(dict(
                date=date, title=title, summary=summary, link=link,
            ))

    return overview, entries
```

修改 `summarize_prompt()` — 替换整个函数体：

```python
def summarize_prompt(keywords: list[str], pages: list[dict]) -> list[dict]:
    blocks: list[str] = []
    used = 0
    sorted_pages = sorted(
        pages,
        key=lambda p: (p.get("publishedDate") or ""),
        reverse=True,
    )
    for p in sorted_pages:
        date_str = f" [{p['publishedDate']}]" if p.get("publishedDate") else ""
        block = (
            f"页面《{p['pageTitle'] or p['pageUrl']}》"
            f"{date_str}\n"
            f"链接：{p['pageUrl']}\n"
        )
        for h in p["hits"][:5]:
            block += f"- {h['snippet'][:300]}\n"
        if used + len(block) > 30000:
            break
        blocks.append(block)
        used += len(block)
    return [
        {"role": "system", "content":
         "你是搜索结果分析助手。基于用户给出的站内搜索命中片段，生成两部分：\n"
         "\n"
         "一、【概述】（必须）\n"
         "用2~5句话总结本次搜索的整体情况，包括涉及的时间范围、主要主题、关键发现等。\n"
         "\n"
         "二、【条目】（必须）\n"
         "为每个有实质内容的页面生成一个条目卡片，格式如下：\n"
         "\n"
         "[日期]《标题》\n"
         "摘要正文\n"
         "链接\n"
         "\n"
         "标题要求（非常重要）：\n"
         "- 提取页面最核心的事件、项目、公告名称作为标题\n"
         "- 标题要具体、有区分度，禁止使用“搜索结果”、“内容页”、“通知公告”等泛词\n"
         "- 如果页面标题本身已足够具体，可直接用作标题\n"
         "- 标题控制在8~40字\n"
         "\n"
         "摘要要求：\n"
         "- 用1~3句话提炼页面的关键信息（金额、时间、内容、涉及方等）\n"
         "- 不要简单重复标题，要补充具体事实\n"
         "- 不要写“该页面提到”、“相关内容如下”等废话\n"
         "\n"
         "格式约定：\n"
         "- 每个条目之间空一行\n"
         "- 日期用[YYYY-MM-DD]格式，无日期时可省略\n"
         "- 标题用《》括起来\n"
         "- 每个条目的最后一行必须是完整URL\n"
         "- 跳过明显为导航页、联系页、登录页等无实质内容的页面\n"
         "\n"
         "只基于所给内容，不要编造。"},
        {"role": "user", "content":
         f"搜索关键词：{'、'.join(keywords)}\n\n" + "\n".join(blocks)},
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ai.py -v`

Expected: 全部通过（含既有测试）

- [ ] **Step 5: Commit**

```bash
git add ai.py tests/test_ai.py
git commit -m "feat: structured AI summary prompt with parse_ai_entries parser"
```

---

### Task 2: server.py — SSE 优先使用 AI 条目

**Files:**
- Modify: `server.py`

**Interfaces:**
- Consumes: `ai.parse_ai_entries`（Task 1）
- Produces: `sse_summarize()` 的 `parsed` 事件优先使用 AI 条目，失败回退 `_build_entries_from_pages()`

- [ ] **Step 1: 检查 sse_summarize 调用方确保兼容**

现有 SSE 调用点在 `server.py:712-713`:
```python
messages = summarize_prompt(all_kw, result["results"])
return StreamingResponse(sse_summarize(messages, result["results"], entry["texts"]),
                         media_type="text/event-stream")
```
`result["results"]` 即 pages 列表，`entry["texts"]` 即 texts dict。签名不变。

- [ ] **Step 2: Write the failing test**

在 `tests/test_server_ai.py` 追加：

```python
from ai import parse_ai_entries


def test_parse_ai_entries_used_in_sse(monkeypatch, site_server):
    """当 AI 输出合法结构化格式时，parsed 事件应包含 AI 生成的条目。"""
    monkeypatch.setattr(server, "expand_keywords", _no_expand)

    ai_text = """【概述】
测试概述。

【条目】
[2025-01-15]《AI生成的标题》
AI生成的摘要。
https://example.com/page
"""

    class _FakeStream:
        def __init__(self, text):
            self._text = iter(text)
        def __aiter__(self):
            return self
        async def __anext__(self):
            try:
                return next(self._text)
            except StopIteration:
                raise StopAsyncIteration

    async def fake_stream(messages):
        return _FakeStream(ai_text)
    monkeypatch.setattr(server, "chat_stream", fake_stream)

    sid = search(site_server, ["alpha"]).json()["searchId"]
    resp = client.post("/api/summarize", json={"searchId": sid})
    assert resp.status_code == 200

    # 在 SSE 输出中查找 parsed 事件
    text = resp.text
    assert '"type": "parsed"' in text
    # parsed 事件中的 entries 应该是 AI 生成的
    assert "AI生成的标题" in text
    assert "AI生成的摘要" in text


def test_parse_ai_entries_fallback(monkeypatch, site_server):
    """当 AI 输出不含结构化标记时，parsed 事件回退到正则提取的条目。"""
    monkeypatch.setattr(server, "expand_keywords", _no_expand)

    ai_text = "这是一段纯文本，没有结构化标记，应该是overview。"

    class _FakeStream:
        def __init__(self, text):
            self._text = iter(text)
        def __aiter__(self):
            return self
        async def __anext__(self):
            try:
                return next(self._text)
            except StopIteration:
                raise StopAsyncIteration

    async def fake_stream(messages):
        return _FakeStream(ai_text)
    monkeypatch.setattr(server, "chat_stream", fake_stream)

    sid = search(site_server, ["alpha"]).json()["searchId"]
    resp = client.post("/api/summarize", json={"searchId": sid})
    assert resp.status_code == 200
    text = resp.text
    assert '"type": "parsed"' in text
    assert "AI生成" not in text  # 没有用 AI 条目（因为无法解析）
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_server_ai.py -v -k "parse_ai_entries"`

Expected: FAIL — parsed 事件中不包含 AI 生成的条目

- [ ] **Step 4: Write minimal implementation**

修改 `server.py`，更新 imports：

```python
from ai import (AIError, ask_prompt, chat_stream, expand_keywords,
                parse_ai_entries, summarize_prompt)
```

修改 `sse_summarize()` 函数：

```python
async def sse_summarize(messages: list[dict], pages: list[dict], texts: dict[str, str]):
    """Stream AI deltas, then parse AI-structured entries (fall back to regex)."""
    full_text = ""
    try:
        async for delta in chat_stream(messages):
            full_text += delta
            yield sse_event({"type": "delta", "text": delta})

        ai_overview, ai_entries = parse_ai_entries(full_text)
        if ai_entries:
            overview = ai_overview or full_text.strip()
            entries = ai_entries
        else:
            overview = full_text.strip()
            entries = _build_entries_from_pages(pages, texts)

        yield sse_event({
            "type": "parsed",
            "overview": overview,
            "entries": entries,
        })
        yield sse_event({"type": "done"})
    except AIError as exc:
        yield sse_event({"type": "error", "message": str(exc)})
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_server_ai.py -v`

Expected: 全部通过

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_server_ai.py
git commit -m "feat: prefer AI-parsed entries in SSE summary, regex as fallback"
```

---

### Task 3: jobs.py — Job AI 摘要使用结构化输出

**Files:**
- Modify: `jobs.py`

- [ ] **Step 1: 实现修改**

修改 `_generate_job_ai_summary()`：

```python
async def _generate_job_ai_summary(keywords: list[str],
                                   new_results: list[dict]) -> dict | None:
    """Call AI to generate structured overview + entries for new job hits."""
    if not new_results:
        return None
    try:
        from ai import summarize_prompt, chat, parse_ai_entries
        messages = summarize_prompt(keywords, new_results)
        overview = await chat(messages, max_tokens=3000)
        overview = overview.strip()

        ai_overview, entries = parse_ai_entries(overview)
        return {"overview": ai_overview or overview, "entries": entries}
    except Exception:
        return None
```

中间区域 `_TITLE_PATTERNS`、`_PROJECT_TERMS` 等正则仍保留（供 `_build_job_summary_entries` 兜底使用，不删除）。

- [ ] **Step 2: Run existing job tests**

Run: `python -m pytest tests/test_jobs.py -v`

Expected: 全部通过

- [ ] **Step 3: Commit**

```bash
git add jobs.py
git commit -m "feat: structured AI output for job summaries, max_tokens 2000 -> 3000"
```

---

### Task 4: 整体验证 + 清理

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -v`

Expected: 全部通过

- [ ] **Step 2: Commit（如有调整）**

```bash
git add -A
git commit -m "chore: verify full test suite passes after AI summary card changes"
```
