"""ai.py 的单元测试：解析、提示词、降级、错误路径（不触网）。"""
import asyncio
import json

import pytest

from ai import (AIError, MAX_EXPANDED, ask_prompt, chat, expand_prompt,
                parse_expansion, parse_sse_line, summarize_prompt)


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
    assert len(msgs[1]["content"]) < 4500  # 预算约 4000 字
    assert "kw" in msgs[1]["content"]


def test_ask_prompt_contents_and_budget():
    pages = [{"url": f"http://h/p{i}", "title": f"P{i}", "text": "y" * 3000}
             for i in range(10)]
    msgs = ask_prompt(["kw"], "问题？", pages)
    assert "问题？" in msgs[1]["content"]
    assert len(msgs[1]["content"]) < 7000  # 预算约 6000 字


def test_chat_without_key_raises(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(AIError, match="DEEPSEEK_API_KEY"):
        asyncio.run(chat([{"role": "user", "content": "hi"}]))
