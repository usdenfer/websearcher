"""通过本地 API 手动验证通用站点发现流程。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Sequence

import httpx


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="调用本地搜索 API，输出精简的站点发现诊断信息。"
    )
    parser.add_argument("start_url", help="搜索起始 URL")
    parser.add_argument("keyword", help="用户选择的正文关键词")
    parser.add_argument(
        "--api",
        default="http://127.0.0.1:7100",
        help="本地 API 地址（默认：http://127.0.0.1:7100）",
    )
    parser.add_argument(
        "--depth",
        type=int,
        choices=(1, 2, 3),
        default=1,
        help="基础抓取深度（默认：1）",
    )
    return parser


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "startUrl": args.start_url,
        "keywords": [args.keyword],
        "depth": args.depth,
        "render": "auto",
    }


def summarize_response(data: dict[str, Any]) -> str:
    summary = {
        "pagesCrawled": data["pagesCrawled"],
        "totalHits": data["totalHits"],
        "discovery": data["discovery"],
        "resultUrls": [item["pageUrl"] for item in data["results"]],
    }
    return json.dumps(summary, ensure_ascii=False, indent=2)


async def async_main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    endpoint = f"{args.api.rstrip('/')}/api/search"

    try:
        async with httpx.AsyncClient(timeout=130) as client:
            response = await client.post(endpoint, json=build_payload(args))
            response.raise_for_status()
            data = response.json()
        print(summarize_response(data))
        return 0
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"冒烟验证失败：{type(exc).__name__}", file=sys.stderr)
        return 1


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
