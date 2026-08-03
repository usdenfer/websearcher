import asyncio
import time

import httpx
import pytest

import crawler
from crawler import (
    MAX_REDIRECT_HOPS,
    await_before_deadline,
    describe_error,
    fetch_html_response,
    gather_before_deadline,
)


def test_describe_error_maps_user_safe_categories():
    request = httpx.Request("GET", "https://x.test/")
    response = httpx.Response(503, request=request)
    assert describe_error(
        httpx.HTTPStatusError(
            "private",
            request=request,
            response=response,
        )
    ) == "HTTP 503"
    assert describe_error(
        httpx.ReadTimeout("private", request=request)
    ) == "访问超时"
    assert describe_error(
        httpx.ConnectError("private", request=request)
    ) == "连接失败"
    assert describe_error(ValueError("公开原因")) == "公开原因"
    assert describe_error(RuntimeError("private")) == "RuntimeError"


def test_fetch_html_response_stops_redirect_loop():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "/again"})

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            with pytest.raises(httpx.TooManyRedirects):
                await fetch_html_response(
                    client,
                    "https://x.test/start",
                    reserve_request=lambda: True,
                    redirect_allowed=lambda url: True,
                )

    asyncio.run(run())
    assert calls == MAX_REDIRECT_HOPS + 1


def test_await_before_deadline_closes_unstarted_coroutine():
    closed = False

    async def operation():
        nonlocal closed
        try:
            await asyncio.sleep(1)
        finally:
            closed = True

    result, expired = asyncio.run(
        await_before_deadline(operation(), time.monotonic() - 1)
    )
    assert result is None
    assert expired is True
    assert closed is False


def test_gather_before_deadline_cancels_all_tasks_when_wait_fails(
    monkeypatch,
):
    created = []
    real_create_task = asyncio.create_task

    async def operation():
        await asyncio.sleep(10)

    def track_task(coroutine):
        task = real_create_task(coroutine)
        created.append(task)
        return task

    async def broken_wait(*args, **kwargs):
        raise RuntimeError("wait failed")

    monkeypatch.setattr(crawler.asyncio, "create_task", track_task)
    monkeypatch.setattr(crawler.asyncio, "wait", broken_wait)
    with pytest.raises(RuntimeError, match="wait failed"):
        asyncio.run(
            gather_before_deadline([operation(), operation()], None)
        )
    assert len(created) == 2
    assert all(task.done() for task in created)


def test_await_before_deadline_cancels_task_when_wait_fails(monkeypatch):
    created = []
    real_create_task = asyncio.create_task

    async def operation():
        await asyncio.sleep(10)

    def track_task(coroutine):
        task = real_create_task(coroutine)
        created.append(task)
        return task

    async def broken_wait(*args, **kwargs):
        raise RuntimeError("wait failed")

    monkeypatch.setattr(crawler.asyncio, "create_task", track_task)
    monkeypatch.setattr(crawler.asyncio, "wait", broken_wait)
    with pytest.raises(RuntimeError, match="wait failed"):
        asyncio.run(await_before_deadline(operation(), None))
    assert len(created) == 1
    assert created[0].done()
