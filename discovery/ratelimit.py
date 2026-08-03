from __future__ import annotations

import asyncio
import os
import time
from urllib.parse import urlsplit


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, ""))
    except (ValueError, TypeError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, ""))
    except (ValueError, TypeError):
        return default


DEFAULT_MAX_RPS = _env_float("MAX_RPS_PER_HOST", 2.0)
DEFAULT_COOLDOWN_SECONDS = _env_float("RATE_LIMIT_COOLDOWN_S", 30.0)
COOLDOWN_DECAY = 0.8

GOV_SITE_OVERRIDES: dict[str, float] = {
    "www.yngp.com": 1.0,
    "www.ccgp.gov.cn": 1.0,
    "zycg.gov.cn": 1.0,
    "www.zycg.gov.cn": 1.0,
}


class PerHostRateLimiter:
    def __init__(
        self,
        default_rate: float = DEFAULT_MAX_RPS,
        overrides: dict[str, float] | None = None,
    ):
        self._default_rate = default_rate
        self._overrides = overrides or {}
        self._tokens: dict[str, float] = {}
        self._last_fill: dict[str, float] = {}
        self._cooldowns: dict[str, float] = {}
        self._backoff_lock = asyncio.Lock()
        self._rate_limit_count: dict[str, int] = {}

    def _rate_for(self, host: str) -> float:
        for pattern, rate in self._overrides.items():
            if host == pattern or host.endswith("." + pattern):
                return rate
        return self._default_rate

    @staticmethod
    def _extract_host(url: str) -> str:
        try:
            host = urlsplit(url).hostname
            return host or ""
        except (ValueError, UnicodeError):
            return ""

    def _fill_tokens(self, host: str, rate: float) -> None:
        now = time.monotonic()
        last = self._last_fill.get(host, now)
        elapsed = now - last
        max_tokens = max(rate, 1.0)
        self._tokens[host] = min(max_tokens, self._tokens.get(host, max_tokens) + elapsed * rate)
        self._last_fill[host] = now

    async def wait(self, url: str) -> None:
        host = self._extract_host(url)
        if not host:
            return
        rate = self._rate_for(host)
        if rate <= 0:
            return
        while True:
            self._fill_tokens(host, rate)
            tokens = self._tokens.get(host, rate)
            if tokens >= 1.0:
                self._tokens[host] = tokens - 1.0
                return
            sleep_time = (1.0 - tokens) / rate
            await asyncio.sleep(sleep_time)

    async def report_rate_limited(self, url: str) -> None:
        host = self._extract_host(url)
        if not host:
            return
        async with self._backoff_lock:
            current = self._cooldowns.get(host, 0.0)
            new_cooldown = max(current * 2.0, DEFAULT_COOLDOWN_SECONDS, 5.0)
            self._cooldowns[host] = min(new_cooldown, 300.0)
            self._rate_limit_count[host] = self._rate_limit_count.get(host, 0) + 1

    async def cooldown_if_needed(self, url: str) -> None:
        host = self._extract_host(url)
        if not host:
            return
        cooldown = 0.0
        async with self._backoff_lock:
            cooldown = self._cooldowns.get(host, 0.0)
            if cooldown <= 0:
                return
            self._cooldowns[host] = cooldown * COOLDOWN_DECAY
            if self._cooldowns[host] < 0.5:
                self._cooldowns[host] = 0.0
        await asyncio.sleep(cooldown)

    def rate_limit_events(self, host: str) -> int:
        return self._rate_limit_count.get(host, 0)


_rate_limiter: PerHostRateLimiter | None = None
_lock = asyncio.Lock()


async def get_rate_limiter() -> PerHostRateLimiter:
    global _rate_limiter
    if _rate_limiter is not None:
        return _rate_limiter
    async with _lock:
        if _rate_limiter is not None:
            return _rate_limiter
        _rate_limiter = PerHostRateLimiter(overrides=GOV_SITE_OVERRIDES)
        return _rate_limiter
