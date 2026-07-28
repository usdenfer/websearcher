"""renderer 测试：真实 Chromium 渲染 JS 页面与翻页抓取。

需要项目 .venv 安装 playwright 且 Chromium 可用；不满足则跳过。
"""
import asyncio

import pytest
import renderer
from discovery.urltools import same_site_boundary

pytest.importorskip("playwright")

from renderer import RenderError, render_page  # noqa: E402


def _chromium_ok() -> bool:
    async def try_launch():
        from playwright.async_api import async_playwright
        try:
            async with async_playwright() as p:
                b = await p.chromium.launch(headless=True)
                await b.close()
            return True
        except Exception:
            return False
    try:
        return asyncio.run(try_launch())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _chromium_ok(),
                                reason="Chromium 不可用")


def test_render_executes_js(site_server):
    html, links = asyncio.run(render_page(f"{site_server}/dynamic.html"))
    assert "JS注入的正文内容标记" in html
    assert any(u.endswith("/sub1.html") for u in links)


def test_render_result_exposes_redirect_final_url(redirect_site):
    assert hasattr(renderer, "render_page_result")
    result = asyncio.run(
        renderer.render_page_result(redirect_site["start"])
    )
    assert result.final_url == redirect_site["home"]
    assert "Redirected home" in result.html


def test_render_navigation_policy_blocks_cross_host_before_request(
        redirect_site):
    redirect_site["handler"].render_outside_requests = 0
    start_url = redirect_site["render_start"]

    with pytest.raises(RenderError, match="站外"):
        asyncio.run(renderer.render_page_result(
            start_url,
            navigation_allowed=lambda target: same_site_boundary(
                start_url,
                target,
            ),
        ))

    assert redirect_site["handler"].render_outside_requests == 0


def test_render_navigation_policy_allows_same_site_redirect(redirect_site):
    start_url = redirect_site["start"]

    result = asyncio.run(renderer.render_page_result(
        start_url,
        navigation_allowed=lambda target: same_site_boundary(
            start_url,
            target,
        ),
    ))

    assert result.final_url == redirect_site["home"]
    assert redirect_site["article"] in result.links


def test_render_pagination_collects_links(site_server):
    html, links = asyncio.run(render_page(f"{site_server}/pager.html"))
    # 第一页和翻页后第二页的文章链接都应收集到
    assert any(u.endswith("/sub1.html") for u in links)
    assert any(u.endswith("/sub2.html") for u in links)
    # 每个翻页状态的 HTML 都拼入最终结果（中途内容不被覆盖丢失）
    assert "第一批文章" in html
    assert "第二批文章" in html


def test_render_numbered_pagination(site_server):
    html, links = asyncio.run(render_page(f"{site_server}/pager2.html"))
    # 数字页码分页：1→2→3 逐页点击，三页文章链接全部收集
    assert any(u.endswith("/sub1.html") for u in links)
    assert any(u.endswith("/sub2.html") for u in links)
    assert any(u.endswith("/deep.html") for u in links)
    assert "第三页文章" in html
    # 栏目菜单（.pager 里 active 的兄弟链接）不能被当作翻页点击：
    # 否则页面会导航到 dynamic.html，其 JS 注入内容会混入结果
    assert "JS注入的正文内容标记" not in html


def test_render_bad_url_raises():
    with pytest.raises(RenderError):
        asyncio.run(render_page("http://127.0.0.1:1/nope.html"))
