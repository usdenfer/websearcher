import asyncio
from types import SimpleNamespace

import jobs


def test_job_summary_filters_invalid_rows_and_sorts_entries():
    results = [
        {"pageUrl": "", "hits": [{"snippet": "ignored"}]},
        {"pageUrl": "https://x.test/empty", "hits": []},
        {
            "pageUrl": "https://x.test/project",
            "pageTitle": "某政府采购网",
            "publishedDate": "2026-08-03",
            "hits": [
                {
                    "snippet": "学校设备采购项目",
                    "keyword": "学校",
                }
            ],
        },
    ]
    pages = [
        SimpleNamespace(
            url="https://x.test/project",
            html=(
                "<main>项目名称：某学校智慧教室设备采购项目。"
                "预算金额：80万元。计划采购时间：2026年10月</main>"
            ),
        )
    ]
    entries = jobs._build_job_summary_entries(results, pages)
    assert len(entries) == 1
    assert entries[0]["date"] == "2026-08-03"
    assert "80万元" in entries[0]["summary"]


def test_job_date_and_time_filters_keep_unknown_dates():
    since = jobs._parse_published_date("2026-08-01")
    assert jobs._parse_published_date("2026/08/03") is not None
    assert jobs._parse_published_date("bad") is None
    rows = [
        {"pageUrl": "old", "publishedDate": "2026-07-01"},
        {"pageUrl": "new", "publishedDate": "2026-08-03"},
        {"pageUrl": "unknown"},
    ]
    assert [
        row["pageUrl"]
        for row in jobs._filter_results_by_time(rows, since)
    ] == ["new", "unknown"]


def test_generate_job_ai_summary_handles_empty_success_and_failure(
    monkeypatch,
):
    assert asyncio.run(jobs._generate_job_ai_summary(["x"], [])) is None

    async def fake_chat(*args, **kwargs):
        return (
            "【概述】\n概述\n\n【条目】\n"
            "[2026-08-03]《标题》\n摘要\nhttps://x.test/a"
        )

    monkeypatch.setattr("ai.chat", fake_chat)
    result = asyncio.run(
        jobs._generate_job_ai_summary(
            ["x"],
            [
                {
                    "pageUrl": "https://x.test/a",
                    "pageTitle": "A",
                    "hits": [],
                }
            ],
        )
    )
    assert result["overview"] == "概述"
    assert result["entries"][0]["title"] == "标题"

    async def broken_chat(*args, **kwargs):
        raise RuntimeError("private")

    monkeypatch.setattr("ai.chat", broken_chat)
    assert (
        asyncio.run(
            jobs._generate_job_ai_summary(
                ["x"],
                [
                    {
                        "pageUrl": "https://x.test/a",
                        "pageTitle": "A",
                        "hits": [],
                    }
                ],
            )
        )
        is None
    )
