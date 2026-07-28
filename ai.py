"""DeepSeek AI client: keyword expansion, summarization, site Q&A."""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

import httpx
from dotenv import load_dotenv

load_dotenv()

EXPAND_TIMEOUT = 15.0
CHAT_TIMEOUT = 90.0
MAX_EXPANDED = 5


class AIError(Exception):
    pass


def api_key() -> str:
    return os.getenv("DEEPSEEK_API_KEY", "")


def base_url() -> str:
    return os.getenv("AI_BASE_URL", "https://api.deepseek.com")


def model() -> str:
    return os.getenv("AI_MODEL", "deepseek-chat")


def _payload(messages: list[dict], max_tokens: int, stream: bool) -> dict:
    return {"model": model(), "messages": messages,
            "max_tokens": max_tokens, "stream": stream}


def _map_http_error(exc: Exception) -> AIError:
    if isinstance(exc, httpx.HTTPStatusError):
        return AIError(f"AI 服务返回 HTTP {exc.response.status_code}")
    if isinstance(exc, httpx.TimeoutException):
        return AIError("AI 服务超时")
    return AIError("无法连接 AI 服务")


async def chat(messages: list[dict], max_tokens: int = 1000,
               timeout: float = CHAT_TIMEOUT) -> str:
    if not api_key():
        raise AIError("未配置 DEEPSEEK_API_KEY")
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(
                f"{base_url()}/chat/completions",
                json=_payload(messages, max_tokens, False),
                headers={"Authorization": f"Bearer {api_key()}"})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise _map_http_error(exc)
    try:
        return resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError):
        raise AIError("AI 返回格式异常")


def parse_sse_line(line: str) -> str | None:
    """Extract a text delta from one SSE data line; None if none."""
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if data == "[DONE]":
        return None
    try:
        chunk = json.loads(data)
        return chunk["choices"][0]["delta"].get("content") or None
    except (ValueError, KeyError, IndexError, TypeError):
        return None


async def chat_stream(messages: list[dict],
                      max_tokens: int = 2000) -> AsyncIterator[str]:
    """Yield text deltas from a streaming chat completion."""
    if not api_key():
        raise AIError("未配置 DEEPSEEK_API_KEY")
    async with httpx.AsyncClient(timeout=CHAT_TIMEOUT) as client:
        try:
            async with client.stream(
                "POST", f"{base_url()}/chat/completions",
                json=_payload(messages, max_tokens, True),
                headers={"Authorization": f"Bearer {api_key()}"},
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    delta = parse_sse_line(line)
                    if delta:
                        yield delta
        except httpx.HTTPError as exc:
            raise _map_http_error(exc)


def expand_prompt(keywords: list[str], host: str) -> list[dict]:
    return [
        {"role": "system", "content":
         "你是搜索关键词扩展助手。只返回一个 JSON 数组（不要任何其他文字），"
         "包含最多 5 个与用户关键词相关的同义词、别称或相关搜索词，"
         "用于提高站内搜索召回。不要重复用户已给的词。无法扩展时返回 []。"},
        {"role": "user", "content":
         f"网站：{host}\n关键词：{'、'.join(keywords)}"},
    ]


def parse_expansion(text: str, limit: int = MAX_EXPANDED) -> list[str]:
    """Parse the model's JSON-array answer; degrade to [] on any failure."""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [str(w).strip() for w in data if str(w).strip()][:limit]


async def expand_keywords(keywords: list[str], host: str) -> list[str]:
    """Return extra related keywords; never raises, [] on failure."""
    try:
        text = await chat(expand_prompt(keywords, host),
                          max_tokens=200, timeout=EXPAND_TIMEOUT)
    except AIError:
        return []
    return parse_expansion(text)


def summarize_prompt(keywords: list[str], pages: list[dict]) -> list[dict]:
    blocks: list[str] = []
    used = 0
    for p in pages:
        block = f"页面《{p['pageTitle'] or p['pageUrl']}》（{p['pageUrl']}）：\n"
        for h in p["hits"][:3]:
            block += f"- {h['snippet'][:200]}\n"
        if used + len(block) > 4000:
            break
        blocks.append(block)
        used += len(block)
    return [
        {"role": "system", "content":
         "你是搜索结果分析助手。基于用户给出的站内搜索命中片段，"
         "先用 2~3 句话给出总体结论，然后按页面逐条给出一句话摘要"
         "（格式：- 《页面标题》：摘要）。只基于所给内容，不要编造。"},
        {"role": "user", "content":
         f"搜索关键词：{'、'.join(keywords)}\n\n" + "\n".join(blocks)},
    ]


def ask_prompt(keywords: list[str], question: str,
               pages: list[dict]) -> list[dict]:
    blocks: list[str] = []
    used = 0
    for p in pages:
        block = f"页面《{p['title'] or p['url']}》（{p['url']}）：\n{p['text']}\n"
        if used + len(block) > 6000:
            break
        blocks.append(block)
        used += len(block)
    return [
        {"role": "system", "content":
         "你是网站内容问答助手。只基于用户给出的页面内容回答问题，"
         "回答中引用来源时附上页面完整 URL（便于点击）。"
         "所给内容不足以回答时，明确说明，不要编造。"},
        {"role": "user", "content":
         f"搜索关键词：{'、'.join(keywords)}\n\n"
         + "\n".join(blocks) + f"\n问题：{question}"},
    ]
