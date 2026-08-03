import asyncio

import discovery.ratelimit as ratelimit


def test_rate_limiter_host_rules_and_invalid_urls():
    limiter = ratelimit.PerHostRateLimiter(
        default_rate=2.0,
        overrides={"gov.test": 0.5},
    )
    assert limiter._rate_for("a.gov.test") == 0.5
    assert limiter._rate_for("other.test") == 2.0
    assert limiter._extract_host("http://[bad") == ""
    asyncio.run(limiter.wait("not-a-url"))
    asyncio.run(limiter.wait("https://other.test/"))


def test_rate_limiter_records_caps_and_decays_cooldown(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(ratelimit.asyncio, "sleep", fake_sleep)
    limiter = ratelimit.PerHostRateLimiter()

    async def run():
        for _ in range(10):
            await limiter.report_rate_limited("https://x.test/a")
        assert limiter.rate_limit_events("x.test") == 10
        assert limiter._cooldowns["x.test"] == 300.0
        await limiter.cooldown_if_needed("https://x.test/a")
        await limiter.cooldown_if_needed("not-a-url")

    asyncio.run(run())
    assert sleeps == [300.0]
    assert limiter._cooldowns["x.test"] == 240.0


def test_get_rate_limiter_is_singleton(monkeypatch):
    monkeypatch.setattr(ratelimit, "_rate_limiter", None)

    async def run():
        first = await ratelimit.get_rate_limiter()
        second = await ratelimit.get_rate_limiter()
        assert first is second

    asyncio.run(run())
