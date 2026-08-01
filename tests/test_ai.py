"""ai.py 的单元测试：解析、提示词、降级、错误路径（不触网）。"""
import asyncio
import json

import pytest

from ai import (AIError, MAX_EXPANDED, ask_prompt, chat, expand_prompt,
                parse_ai_entries, parse_expansion, parse_sse_line,
                summarize_prompt)


def test_parse_expansion_good():
    assert parse_expansion('["省长", "予波同志"]') == ["省长", "予波同志"]


def test_parse_expansion_embedded_in_prose():
    assert parse_expansion('好的：\n["a", "b"]\n以上') == ["a", "b"]


def test_parse_expansion_bad_json_and_non_list():
    assert parse_expansion("not json at all") == []
    assert parse_expansion('{"a": 1}') == []
    assert parse_expansion('[1, 2, " "]') == ["1", "2"]


def test_parse_expansion_limit():
    words = [f"w{i}" for i in range(10)]
    assert len(parse_expansion(json.dumps(words))) == MAX_EXPANDED


def test_parse_sse_line():
    assert parse_sse_line(
        'data: {"choices":[{"delta":{"content":"你好"}}]}') == "你好"
    assert parse_sse_line("data: [DONE]") is None
    assert parse_sse_line(": comment") is None
    assert parse_sse_line("data: not-json") is None
    assert parse_sse_line('data: {"choices":[{"delta":{}}]}') is None


def test_expand_prompt_contents():
    msgs = expand_prompt(["王予波"], "dct.yn.gov.cn")
    assert msgs[0]["role"] == "system"
    assert "王予波" in msgs[1]["content"]
    assert "dct.yn.gov.cn" in msgs[1]["content"]


def test_summarize_prompt_budget():
    pages = [{"pageUrl": f"http://h/p{i}", "pageTitle": f"P{i}",
              "hits": [{"snippet": "x" * 500, "kind": "text"}]}
             for i in range(30)]
    msgs = summarize_prompt(["kw"], pages)
    assert len(msgs[1]["content"]) < 21000  # 预算约 20000 字
    assert "kw" in msgs[1]["content"]


def test_ask_prompt_contents_and_budget():
    pages = [{"url": f"http://h/p{i}", "title": f"P{i}", "text": "y" * 3000}
             for i in range(10)]
    msgs = ask_prompt(["kw"], "问题？", pages)
    assert "问题？" in msgs[1]["content"]
    assert len(msgs[1]["content"]) < 31000  # 预算约 30000 字


def test_chat_without_key_raises(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(AIError, match="DEEPSEEK_API_KEY"):
        asyncio.run(chat([{"role": "user", "content": "hi"}]))


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
