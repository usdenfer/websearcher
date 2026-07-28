"""Headless Chromium rendering for JS-heavy sites (Playwright).

render_page loads a URL in a real browser so JS-injected content and links
(ajax list pages, tab panels, pagination) become visible to the crawler.
If pagination controls are detected, it clicks "next page" repeatedly and
collects newly appeared links, so old list items buried many pages deep
can still be discovered.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

RENDER_TIMEOUT_MS = 25_000
NETWORK_IDLE_MS = 6_000
MAX_PAGINATION_CLICKS = 100
PAGINATION_SETTLE_MS = 1_000
RENDER_CONCURRENCY = 2


class RenderError(Exception):
    """Rendering failed (navigation error, HTTP error, browser missing)."""


@dataclass(frozen=True)
class RenderedPage:
    html: str
    links: list[str]
    final_url: str


# Browser 与事件循环绑定：换 loop（如测试里多次 asyncio.run）必须重建
_state: dict = {"loop": None, "pw": None, "browser": None, "sem": None}

_NEXT_TEXTS = {
    "下一页", "下页", "next", ">", "»", "›", "下一页>", "下一页»", "下页>",
}

_FIND_NEXT_JS = """() => {
  const wanted = %s;
  const usable = (a) => {
    const cls = String(a.className || "") + " " +
      String(a.parentElement ? a.parentElement.className : "");
    if (/disabled/.test(cls)) return false;
    const href = a.getAttribute("href");
    const oc = a.getAttribute("onclick");
    return !!(href || oc || a.tagName === "BUTTON");
  };
  // 策略1：文本/title 明确是“下一页”
  const els = [...document.querySelectorAll("a,button")];
  for (const el of els) {
    const t = (el.textContent || "").replace(/\\s+/g, "").toLowerCase();
    const title = (el.getAttribute("title") || "");
    if ((wanted.includes(t) || title.includes("下一页")) && usable(el)) {
      return el;
    }
  }
  // 策略2：分页容器里当前页的下一个兄弟页码
  const pageish = (a) => {
    const href = (a.getAttribute("href") || "").trim();
    const oc = a.getAttribute("onclick") || "";
    const t = (a.textContent || "").replace(/\s+/g, "");
    return /^javascript:/i.test(href) || href === "#" ||
      /^\d+$/.test(t) || /page|GetClassList|goPage/i.test(href + " " + oc);
  };
  const conts = [...document.querySelectorAll(
    ".pagination, .pager, [class*='pager'], [class*='Pager']")];
  for (const c of conts) {
    const items = [...c.querySelectorAll("li")];
    for (let i = 0; i < items.length - 1; i++) {
      const li = items[i];
      const a = li.querySelector("a");
      if (!a) continue;
      const cur = /active|current|\\bon\\b/.test(
        String(li.className) + " " + String(a.className));
      if (!cur) continue;
      const nxt = items[i + 1];
      if (/disabled/.test(String(nxt.className))) return null;
      const na = nxt.querySelector("a");
      // 必须是“翻页式”链接，避免把栏目菜单的下一栏目当成下一页
      if (na && usable(na) && pageish(na)) return na;
    }
  }
  return null;
}""" % repr(sorted(_NEXT_TEXTS))


async def _shutdown_locked() -> None:
    pw = _state.get("pw")
    _state.update(loop=None, pw=None, browser=None, sem=None)
    if pw is not None:
        try:
            await pw.stop()
        except Exception:  # 旧 loop 已关闭时停止会失败，浏览器随进程退出
            pass


async def shutdown() -> None:
    """关闭浏览器实例（应用退出时调用）。"""
    await _shutdown_locked()


async def _get_browser():
    loop = asyncio.get_running_loop()
    browser = _state.get("browser")
    if browser is not None and _state["loop"] is loop:
        try:
            if browser.is_connected():
                return browser
        except Exception:
            pass
    await _shutdown_locked()
    from playwright.async_api import async_playwright
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        raise RenderError(
            f"浏览器启动失败：{exc!r}（首次使用需运行 playwright install chromium）"
        ) from exc
    _state.update(loop=loop, pw=pw, browser=browser,
                  sem=asyncio.Semaphore(RENDER_CONCURRENCY))
    return browser


async def _wait_idle(page) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_MS)
    except Exception:
        pass
    await page.wait_for_timeout(400)


async def _collect_links(page) -> list[str]:
    try:
        hrefs = await page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.href)")
    except Exception:
        return []
    return [h for h in hrefs
            if isinstance(h, str) and h.startswith(("http://", "https://"))]


async def _find_next(page):
    try:
        return await page.evaluate_handle(_FIND_NEXT_JS)
    except Exception:
        return None


async def _click_next(page, btn) -> bool:
    try:
        await btn.click(timeout=3000)
        return True
    except Exception:
        pass
    try:
        href = await btn.get_attribute("href")
    except Exception:
        return False
    if href and href.strip().lower().startswith("javascript:"):
        try:
            await page.evaluate(href.strip()[len("javascript:"):])
            return True
        except Exception:
            return False
    return False


async def _harvest_pagination(page, seen: set[str]) -> tuple[set[str], list[str]]:
    extra: set[str] = set()
    html_parts: list[str] = []
    base_url = page.url.split("#")[0]
    for _ in range(MAX_PAGINATION_CLICKS):
        handle = await _find_next(page)
        if handle is None:
            break
        element = handle.as_element()
        if element is None:
            break
        if not await _click_next(page, element):
            break
        await page.wait_for_timeout(PAGINATION_SETTLE_MS)
        if page.url.split("#")[0] != base_url:
            # 点击导致了整页导航（误点了栏目菜单等），放弃翻页结果
            return set(), []
        html_parts.append(await page.content())
        new = set(await _collect_links(page)) - seen - extra
        if not new:
            # 可能还在加载，再等一拍确认
            await page.wait_for_timeout(PAGINATION_SETTLE_MS)
            html_parts[-1] = await page.content()
            new = set(await _collect_links(page)) - seen - extra
            if not new:
                break
        extra |= new
    return extra, html_parts


async def render_page_result(url: str) -> RenderedPage:
    """Render a page and retain the browser's effective URL after navigation."""
    browser = await _get_browser()
    async with _state["sem"]:
        page = await browser.new_page()
        try:
            try:
                resp = await page.goto(
                    url, timeout=RENDER_TIMEOUT_MS,
                    wait_until="domcontentloaded")
            except Exception as exc:
                raise RenderError(f"页面加载失败：{exc}") from exc
            if resp is not None and resp.status >= 400:
                raise RenderError(f"HTTP {resp.status}")
            await _wait_idle(page)
            links = set(await _collect_links(page))
            initial_html = await page.content()
            extra, html_parts = await _harvest_pagination(page, set(links))
            links |= extra
            # 拼接初始与每个翻页状态的 HTML，避免内容被覆盖丢失
            html = "\n".join([initial_html, *html_parts])
            return RenderedPage(html, sorted(links), page.url)
        finally:
            try:
                await page.close()
            except Exception:
                pass


async def render_page(url: str) -> tuple[str, list[str]]:
    """渲染 URL，返回 (最终 HTML, 页面及翻页中发现的全部 http(s) 链接)。"""
    result = await render_page_result(url)
    return result.html, result.links
