"""DeepSeek AI client: keyword expansion, summarization, site Q&A."""
from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator

import httpx
from dotenv import load_dotenv

load_dotenv()

EXPAND_TIMEOUT = 15.0
CHAT_TIMEOUT = 180.0
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
        return AIError(f"AI \u670d\u52a1\u8fd4\u56de HTTP {exc.response.status_code}")
    if isinstance(exc, httpx.TimeoutException):
        return AIError("AI \u670d\u52a1\u8d85\u65f6")
    return AIError("\u65e0\u6cd5\u8fde\u63a5 AI \u670d\u52a1")


async def chat(messages: list[dict], max_tokens: int = 4000,
               timeout: float = CHAT_TIMEOUT) -> str:
    if not api_key():
        raise AIError("\u672a\u914d\u7f6e DEEPSEEK_API_KEY")
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
        raise AIError("AI \u8fd4\u56de\u683c\u5f0f\u5f02\u5e38")


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
                      max_tokens: int = 4000) -> AsyncIterator[str]:
    """Yield text deltas from a streaming chat completion."""
    if not api_key():
        raise AIError("\u672a\u914d\u7f6e DEEPSEEK_API_KEY")
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
         "\u4f60\u662f\u641c\u7d22\u5173\u952e\u8bcd\u6269\u5c55\u52a9\u624b\u3002\u53ea\u8fd4\u56de\u4e00\u4e2a JSON \u6570\u7ec4\uff08\u4e0d\u8981\u4efb\u4f55\u5176\u4ed6\u6587\u5b57\uff09\uff0c"
         "\u5305\u542b\u6700\u591a 5 \u4e2a\u4e0e\u7528\u6237\u5173\u952e\u8bcd\u76f8\u5173\u7684\u540c\u4e49\u8bcd\u3001\u522b\u79f0\u6216\u76f8\u5173\u641c\u7d22\u8bcd\uff0c"
         "\u7528\u4e8e\u63d0\u9ad8\u7ad9\u5185\u641c\u7d22\u53ec\u56de\u3002\u4e0d\u8981\u91cd\u590d\u7528\u6237\u5df2\u7ed9\u7684\u8bcd\u3002\u65e0\u6cd5\u6269\u5c55\u65f6\u8fd4\u56de []\u3002\n\n"
         "\u786c\u6027\u8fb9\u754c\uff08\u8fdd\u53cd\u5219\u4e0d\u8fd4\u56de\u8be5\u8bcd\uff09\uff1a\n"
         "1. \u6269\u5c55\u8bcd\u5fc5\u987b\u4e0e\u81f3\u5c11\u4e00\u4e2a\u539f\u59cb\u8bcd\u5171\u4eab\u4e2d\u6587\u5b57\u7b26\u2014\u2014\u6559\u80b2\u53ea\u80fd\u6269\u5c55\u5230\u5b66\u6821/\u57f9\u8bad/\u6559\u5b66\u7b49\uff0c\u7edd\u4e0d\u53ef\u8df3\u5230\u6587\u65c5/\u533b\u7597/\u4ea4\u901a/\u519c\u4e1a\n"
         "2. \u7981\u6b62\u6dfb\u52a0\u4e0e\u539f\u59cb\u8bcd\u96f6\u5b57\u91cd\u53e0\u7684\u8de8\u9886\u57df\u8bcd\n"
         "3. \u4e0d\u6dfb\u52a0\u5355\u5b57\u6216\u8fc7\u4e8e\u5bbd\u6cdb\u7684\u53cc\u5b57\u901a\u7528\u8bcd\uff08\u9879\u76ee\u3001\u91c7\u8d2d\u3001\u670d\u52a1\u3001\u7ba1\u7406\uff09\n"
         "4. \u4f18\u5148\u6269\u5c55\u5177\u4f53\u673a\u6784\u540d\u3001\u5730\u540d\u53d8\u4f53\u3001\u884c\u4e1a\u672f\u8bed"},
        {"role": "user", "content":
         f"\u7f51\u7ad9\uff1a{host}\n\u5173\u952e\u8bcd\uff1a{'、'.join(keywords)}"},
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


def parse_ai_entries(text: str) -> tuple[str, list[dict]]:
    """Parse AI structured output: \u3010\u6982\u8ff0\u3011+\u3010\u6761\u76ee\u3011 into overview and entries.
    Returns (overview, entries). Entries is empty list when parsing fails."""
    entries: list[dict] = []

    ov_match = re.search(r'\u3010\u6982\u8ff0\u3011\s*([\s\S]*?)(?=\u3010\u6761\u76ee\u3011|$)', text)
    en_match = re.search(r'\u3010\u6761\u76ee\u3011\s*([\s\S]*)$', text)

    if not ov_match:
        return text.strip(), []

    overview = ov_match.group(1).strip()
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
        title_m = re.search(r'\u300a([^\u300b]*)\u300b', first_line)
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
            f"\u9875\u9762\u300a{p['pageTitle'] or p['pageUrl']}\u300b"
            f"{date_str}\n"
            f"\u94fe\u63a5\uff1a{p['pageUrl']}\n"
        )
        for h in p["hits"][:5]:
            block += f"- {h['snippet'][:300]}\n"
        if used + len(block) > 30000:
            break
        blocks.append(block)
        used += len(block)
    return [
        {"role": "system", "content":
         "\u4f60\u662f\u641c\u7d22\u7ed3\u679c\u5206\u6790\u52a9\u624b\u3002\u57fa\u4e8e\u7528\u6237\u7ed9\u51fa\u7684\u7ad9\u5185\u641c\u7d22\u547d\u4e2d\u7247\u6bb5\uff0c\u751f\u6210\u4e24\u90e8\u5206\uff1a\n"
         "\n"
         "\u4e00\u3001\u3010\u6982\u8ff0\u3011\uff08\u5fc5\u987b\uff09\n"
         "\u75282~5\u53e5\u8bdd\u603b\u7ed3\u672c\u6b21\u641c\u7d22\u7684\u6574\u4f53\u60c5\u51b5\uff0c\u5305\u62ec\u6d89\u53ca\u7684\u65f6\u95f4\u8303\u56f4\u3001\u4e3b\u8981\u4e3b\u9898\u3001\u5173\u952e\u53d1\u73b0\u7b49\u3002\n"
         "\n"
         "\u4e8c\u3001\u3010\u6761\u76ee\u3011\uff08\u5fc5\u987b\uff09\n"
         "\u4e3a\u6bcf\u4e2a\u6709\u5b9e\u8d28\u5185\u5bb9\u7684\u9875\u9762\u751f\u6210\u4e00\u4e2a\u6761\u76ee\u5361\u7247\uff0c\u683c\u5f0f\u5982\u4e0b\uff1a\n"
         "\n"
         "[\u65e5\u671f]\u300a\u6807\u9898\u300b\n"
         "\u6458\u8981\u6b63\u6587\n"
         "\u94fe\u63a5\n"
         "\n"
         "\u6807\u9898\u8981\u6c42\uff08\u975e\u5e38\u91cd\u8981\uff09\uff1a\n"
         "- \u63d0\u53d6\u9875\u9762\u6700\u6838\u5fc3\u7684\u4e8b\u4ef6\u3001\u9879\u76ee\u3001\u516c\u544a\u540d\u79f0\u4f5c\u4e3a\u6807\u9898\n"
         "- \u6807\u9898\u8981\u5177\u4f53\u3001\u6709\u533a\u5206\u5ea6\uff0c\u7981\u6b62\u4f7f\u7528\u201c\u641c\u7d22\u7ed3\u679c\u201d\u3001\u201c\u5185\u5bb9\u9875\u201d\u3001\u201c\u901a\u77e5\u516c\u544a\u201d\u7b49\u6cdb\u8bcd\n"
         "- \u5982\u679c\u9875\u9762\u6807\u9898\u672c\u8eab\u5df2\u8db3\u591f\u5177\u4f53\uff0c\u53ef\u76f4\u63a5\u7528\u4f5c\u6807\u9898\n"
         "- \u6807\u9898\u63a7\u5236\u57288~40\u5b57\n"
         "\n"
         "\u6458\u8981\u8981\u6c42\uff1a\n"
         "- \u75281~3\u53e5\u8bdd\u63d0\u70bc\u9875\u9762\u7684\u5173\u952e\u4fe1\u606f\uff08\u91d1\u989d\u3001\u65f6\u95f4\u3001\u5185\u5bb9\u3001\u6d89\u53ca\u65b9\u7b49\uff09\n"
         "- \u4e0d\u8981\u7b80\u5355\u91cd\u590d\u6807\u9898\uff0c\u8981\u8865\u5145\u5177\u4f53\u4e8b\u5b9e\n"
         "- \u4e0d\u8981\u5199\u201c\u8be5\u9875\u9762\u63d0\u5230\u201d\u3001\u201c\u76f8\u5173\u5185\u5bb9\u5982\u4e0b\u201d\u7b49\u5e9f\u8bdd\n"
         "\n"
         "\u683c\u5f0f\u7ea6\u5b9a\uff1a\n"
         "- \u6bcf\u4e2a\u6761\u76ee\u4e4b\u95f4\u7a7a\u4e00\u884c\n"
         "- \u65e5\u671f\u7528[YYYY-MM-DD]\u683c\u5f0f\uff0c\u65e0\u65e5\u671f\u65f6\u53ef\u7701\u7565\n"
         "- \u6807\u9898\u7528\u300a\u300b\u62ec\u8d77\u6765\n"
         "- \u6bcf\u4e2a\u6761\u76ee\u7684\u6700\u540e\u4e00\u884c\u5fc5\u987b\u662f\u5b8c\u6574URL\n"
         "- \u8df3\u8fc7\u660e\u663e\u4e3a\u5bfc\u822a\u9875\u3001\u8054\u7cfb\u9875\u3001\u767b\u5f55\u9875\u7b49\u65e0\u5b9e\u8d28\u5185\u5bb9\u7684\u9875\u9762\n"
         "\n"
         "\u53ea\u57fa\u4e8e\u6240\u7ed9\u5185\u5bb9\uff0c\u4e0d\u8981\u7f16\u9020\u3002"},
        {"role": "user", "content":
         f"\u641c\u7d22\u5173\u952e\u8bcd\uff1a{'、'.join(keywords)}\n\n" + "\n".join(blocks)},
    ]


def ask_prompt(keywords: list[str], question: str,
               pages: list[dict]) -> list[dict]:
    blocks: list[str] = []
    used = 0
    for p in pages:
        block = f"\u9875\u9762\u300a{p['title'] or p['url']}\u300b\uff08{p['url']}\uff09\uff1a\n{p['text']}\n"
        if used + len(block) > 30000:
            break
        blocks.append(block)
        used += len(block)
    return [
        {"role": "system", "content":
         "\u4f60\u662f\u7f51\u7ad9\u5185\u5bb9\u95ee\u7b54\u52a9\u624b\u3002\u53ea\u57fa\u4e8e\u7528\u6237\u7ed9\u51fa\u7684\u9875\u9762\u5185\u5bb9\u56de\u7b54\u95ee\u9898\uff0c"
         "\u56de\u7b54\u4e2d\u5f15\u7528\u6765\u6e90\u65f6\u9644\u4e0a\u9875\u9762\u5b8c\u6574 URL\uff08\u4fbf\u4e8e\u70b9\u51fb\uff09\u3002"
         "\u6240\u7ed9\u5185\u5bb9\u4e0d\u8db3\u4ee5\u56de\u7b54\u65f6\uff0c\u660e\u786e\u8bf4\u660e\uff0c\u4e0d\u8981\u7f16\u9020\u3002"},
        {"role": "user", "content":
         f"\u641c\u7d22\u5173\u952e\u8bcd\uff1a{'、'.join(keywords)}\n\n"
         + "\n".join(blocks) + f"\n\u95ee\u9898\uff1a{question}"},
    ]
