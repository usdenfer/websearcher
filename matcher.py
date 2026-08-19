"""Keyword matcher: search keywords across visible text and element attributes."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment
from discovery.models import Candidate
from discovery.urltools import normalize_candidate_url

SNIPPET_RADIUS = 60

META_NAME_KEYS = {"keywords", "description"}
SKIP_TEXT_PARENTS = {"script", "style", "noscript", "a", "title"}
NOISE_SELECTORS = (
    "script, style, noscript, nav, header, footer, aside, "
    "[hidden], [aria-hidden='true'], .nav, .navbar, .footer, "
    ".sidebar, .advert, .ad, .login, "
    ".top, .banner, .crumbs, .breadcrumb, .location, .path, "
    ".toolbar, .share, .related, .recommend, .hot, .links"
)
MAIN_CONTENT_SELECTORS = (
    "article",
    "main",
    "[role='main']",
    ".article-content",
    ".content-main",
    ".article_body",
    ".detail-content",
    ".detail",
    ".info-detail",
    ".article-detail",
    ".news-content",
    ".bulletin-content",
    ".con_text",
    ".text_content",
    ".info_content",
    ".content_box",
    "#content",
    "#detail",
    "#mainContent",
    "#article",
)


@dataclass
class Hit:
    kind: str
    snippet: str
    keyword: str
    href: str
    linkHref: str | None = None


def make_snippet(text: str, keyword: str) -> str | None:
    """Return ~60 chars of context around the first case-insensitive match."""
    norm = re.sub(r"\s+", " ", text)
    idx = norm.lower().find(keyword.lower())
    if idx < 0:
        return None
    start = max(0, idx - SNIPPET_RADIUS)
    end = min(len(norm), idx + len(keyword) + SNIPPET_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(norm) else ""
    return prefix + norm[start:end] + suffix


def extract_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""


_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d",
]

_META_DATE_NAMES = {
    "article:published_time", "date", "pubdate", "dc.date",
    "citation_date", "dc.date.issued", "dcterms.issued",
    "publish-date", "release_date",
}

_URL_DATE_RE = re.compile(
    r'/(\d{4})[-/](\d{1,2})(?:[-/](\d{1,2}))?[/.]'
)
_URL_DATE_FILE_RE = re.compile(
    r'(?:[^0-9]|^)(\d{4})(\d{2})(\d{2})(?:\d{2,})?(?:\D|$)'
)
_CHINESE_DATE_RE = re.compile(
    r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日'
)
_ISO_DATE_BODY_RE = re.compile(
    r'(?:[^\d]|^)(\d{4})-(\d{1,2})-(\d{1,2})(?:[^\d]|$)'
)
_PUBLISH_LABEL_DATE_RE = re.compile(
    r'(?:发布|发表|上传|更新|录入)时间[：:]\s*(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})'
)
_PUBLISH_LABEL_FULL_RE = re.compile(
    r'(?:发布|发表|上传|更新|录入)时间[：:]\s*'
    r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日'
)


def _try_parse_date(value: str) -> str | None:
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value[:19], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.strip()).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _parse_url_date(url: str) -> str | None:
    m = _URL_DATE_RE.search(url)
    if m:
        year = int(m.group(1))
        month = int(m.group(2))
        day = int(m.group(3)) if m.group(3) else 1
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None
    m = _URL_DATE_FILE_RE.search(url)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)),
                            int(m.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def _parse_body_date(text: str) -> str | None:
    def _make_date(y, m, d):
        try:
            return datetime(int(y), int(m), int(d)).strftime("%Y-%m-%d")
        except ValueError:
            return None

    m = _PUBLISH_LABEL_FULL_RE.search(text)
    if m:
        return _make_date(m.group(1), m.group(2), m.group(3))
    m = _PUBLISH_LABEL_DATE_RE.search(text)
    if m:
        return _make_date(m.group(1), m.group(2), m.group(3))
    m = _CHINESE_DATE_RE.search(text)
    if m:
        return _make_date(m.group(1), m.group(2), m.group(3))
    m = _ISO_DATE_BODY_RE.search(text)
    if m:
        return _make_date(m.group(1), m.group(2), m.group(3))
    return None


def extract_published_date(html: str, url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")

    for meta in soup.find_all("meta"):
        name = (meta.get("name") or "").lower()
        prop = (meta.get("property") or "").lower()
        if name in _META_DATE_NAMES or prop in _META_DATE_NAMES:
            content = meta.get("content")
            if content:
                parsed = _try_parse_date(content.strip())
                if parsed:
                    return parsed

    for time_el in soup.find_all("time"):
        dt = time_el.get("datetime") or time_el.get("content")
        if dt:
            parsed = _try_parse_date(dt.strip())
            if parsed:
                return parsed

    parsed = _parse_url_date(url)
    if parsed:
        return parsed

    for el in soup.find_all(
        attrs={"class": re.compile(r'(?:time|date|pub|publish)',
                                   re.IGNORECASE)}
    ):
        text = _node_text(el)
        if text:
            parsed = _parse_body_date(text)
            if parsed:
                return parsed

    body_text = _node_text(soup.body) if soup.body else ""
    parsed = _parse_body_date(body_text)
    if parsed:
        return parsed

    return None


def match_page(html: str, page_url: str, keywords: list[str]) -> list[Hit]:
    """Match keywords against all visible text and element attributes."""
    soup = BeautifulSoup(html, "html.parser")
    keywords = [k for k in keywords if k.strip()]
    hits: list[Hit] = []
    seen: set[tuple[str, str, str]] = set()

    def add(kind: str, raw_text: str, keyword: str,
            link_href: str | None = None) -> None:
        snippet = make_snippet(raw_text, keyword)
        if snippet is None:
            return
        key = (kind, snippet, keyword)
        if key in seen:
            return
        seen.add(key)
        if kind == "text":
            href = f"{page_url}#:~:text={quote(keyword)}"
        elif kind in ("link-text", "link-href") and link_href:
            # for link hits the destination page is what the user wants:
            # jump straight to the link target (already absolute)
            href = link_href
        else:
            href = page_url
        hits.append(Hit(kind=kind, snippet=snippet, keyword=keyword,
                        href=href, linkHref=link_href))

    # text: visible body text (skip script/style/noscript/a/title nodes)
    for node in soup.find_all(string=True):
        if isinstance(node, Comment):
            continue
        if node.parent and node.parent.name in SKIP_TEXT_PARENTS:
            continue
        if not str(node).strip():
            continue
        for kw in keywords:
            add("text", str(node), kw)

    # img-alt / img-src
    for img in soup.find_all("img"):
        alt = img.get("alt")
        if alt:
            for kw in keywords:
                add("img-alt", alt, kw)
        src = img.get("src")
        if src:
            filename = urlsplit(src).path.rsplit("/", 1)[-1]
            if filename:
                for kw in keywords:
                    add("img-src", filename, kw)

    # title-attr / aria-label
    for el in soup.find_all(attrs={"title": True}):
        for kw in keywords:
            add("title-attr", el["title"], kw)
    for el in soup.find_all(attrs={"aria-label": True}):
        for kw in keywords:
            add("aria-label", el["aria-label"], kw)

    # link-text / link-href
    for a in soup.find_all("a", href=True):
        absolute = urljoin(page_url, a["href"])
        for kw in keywords:
            add("link-text", a.get_text(), kw, link_href=absolute)
            add("link-href", a["href"], kw, link_href=absolute)

    # meta
    for meta in soup.find_all("meta"):
        content = meta.get("content")
        if not content:
            continue
        name = (meta.get("name") or "").lower()
        prop = (meta.get("property") or "").lower()
        if name in META_NAME_KEYS or prop.startswith("og:") \
                or name.startswith("twitter:"):
            for kw in keywords:
                add("meta", content, kw)

    # form
    for el in soup.find_all(["input", "textarea"]):
        for attr in ("placeholder", "value"):
            val = el.get(attr)
            if val:
                for kw in keywords:
                    add("form", val, kw)

    return hits


def extract_text(html: str, limit: int = 3000) -> str:
    """Return visible page text (no script/style/comments), capped at limit."""
    soup = BeautifulSoup(html, "html.parser")
    chunks: list[str] = []
    total = 0
    for node in soup.find_all(string=True):
        if isinstance(node, Comment):
            continue
        if node.parent and node.parent.name in {"script", "style", "noscript"}:
            continue
        t = re.sub(r"\s+", " ", str(node)).strip()
        if not t:
            continue
        chunks.append(t)
        total += len(t)
        if total >= limit:
            break
    return " ".join(chunks)[:limit]


def _node_text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def extract_main_text(html: str) -> str:
    """Return normalized text from the page's most likely article container."""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select(NOISE_SELECTORS):
        node.decompose()
    for node in soup.select("[aria-hidden]"):
        value = node.get("aria-hidden")
        if isinstance(value, str) and value.strip().lower() == "true":
            node.decompose()

    candidates: list[tuple[object, str]] = []
    seen: set[int] = set()
    for selector in MAIN_CONTENT_SELECTORS:
        for node in soup.select(selector):
            identity = id(node)
            text = _node_text(node)
            if identity not in seen and text:
                seen.add(identity)
                candidates.append((node, text))

    preferred = None
    if candidates:
        preferred = max(candidates, key=lambda item: len(item[1]))[0]
    root = preferred or soup.body
    return _node_text(root) if root is not None else ""


def match_body_page(
    html: str, page_url: str, keywords: list[str],
) -> list[Hit]:
    """Match keywords against the extracted article body (OR logic)."""
    text = extract_main_text(html)
    result: list[Hit] = []
    for keyword in dict.fromkeys(k.strip() for k in keywords if k.strip()):
        snippet = make_snippet(text, keyword)
        if snippet is None:
            continue
        result.append(Hit(
            kind="text",
            snippet=snippet,
            keyword=keyword,
            href=f"{page_url}#:~:text={quote(keyword)}",
        ))
    return result


_TITLE_PATTERNS = [
    re.compile(r'(?:采购)?项目名称[：:]\s*(.+?)(?:[。；;]|\n|$)'),
    re.compile(r'项目编号[：:][^。；;\n]*?项目名称[：:]\s*(.+?)(?:[。；;]|\n|$)'),
    re.compile(r'采购(?:意向|需求|内容)[：:]\s*(.+?)(?:[。；;]|\n|$)'),
    re.compile(r'(?:公告|公示)\s*标题[：:]\s*(.+?)(?:[。；;]|\n|$)'),
    re.compile(r'(?:^|[。；;，,、\n])\s*'
               r'([^。；;，,、\n]{10,60}?(?:项目|采购项目|招标项目|中标|成交|合同))'),
    re.compile(r'项目概况[：:]?\s*\n?\s*(.+?)(?:[。；;]|\n|$)'),
    re.compile(r'(.{8,50}(?:项目|采购|招标|中标|施工|监理|设备).{0,30})'),
    re.compile(r'((?:招标|采购|中标|成交|合同)\s*公告)'),
    re.compile(r'(?:^|[。；;，,、\n])\s*'
               r'([^。；;，,、\n]{8,60}?(?:公告|公示|通知))'),
    re.compile(r'^(.{8,60}?(?:公告|公示|通知))'),
]

_SITE_NAME_RE = re.compile(
    r'(?:政府采购网|采购网|招标网|公共资源|政府门户|人民政府|'
    r'政务服务平台|政务服务网|交易平台|交易中心|'
    r'信息网|服务网|门户网站)$')


def _extract_project_title(text: str) -> str:
    for pat in _TITLE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        try:
            title = m.group(1).strip()
        except IndexError:
            continue
        title = re.sub(r'^[：:，,、。；;\s]+', '', title)
        title = re.sub(r'^[A-Za-z0-9.\s\-–—_]+(?=[\u4e00-\u9fff])',
                       '', title)
        title = re.sub(r'[：:，,、。；;\s]+$', '', title)
        if len(title) >= 8:
            return title
    return ""


def _clean_page_title(page_title: str) -> str:
    title = re.sub(r'\s*[-–—_|｜]\s*.+$', '', page_title.strip())
    if len(title) < 4:
        return page_title.strip()
    return title


def extract_display_title(html: str, page_title: str, url: str) -> str:
    cleaned_pt = _clean_page_title(page_title)

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(['h1', 'h2', 'h3']):
        text = _node_text(tag)
        if text and len(text) >= 8:
            title = _extract_project_title(text)
            if title and len(title) >= 8:
                return title[:60]

    main_text = extract_main_text(html)
    if main_text and len(main_text) >= 40:
        title = _extract_project_title(main_text)
        if title and len(title) >= 8:
            if (cleaned_pt and len(cleaned_pt) >= 8
                    and title in cleaned_pt):
                return cleaned_pt[:80]
            return title[:60]

    pt_title = _extract_project_title(cleaned_pt)
    if pt_title:
        return pt_title[:60]

    if (cleaned_pt and len(cleaned_pt) >= 8
            and not _SITE_NAME_RE.search(cleaned_pt)):
        return cleaned_pt[:80]

    path = urlsplit(url).path.strip("/")
    if path:
        segments = path.split("/")
        last = segments[-1]
        if "." in last:
            last = last.rsplit(".", 1)[0]
        if len(last) >= 4:
            return last[:80]
    return cleaned_pt or url[:80]


_JS_DRIVEN_RE = re.compile(
    r"\$\.ajax|\$\.get\(|\$\.post\(|\$\.getJSON|getScript|"
    r"XMLHttpRequest|\.load\(\s*['\"]",
    re.IGNORECASE)


def looks_js_driven(html: str) -> bool:
    """Heuristic: page loads its lists/content via AJAX, so the static
    HTML is an incomplete view of the site (worth a render pass)."""
    return bool(_JS_DRIVEN_RE.search(html))


def match_crawl_result(pages, keywords: list[str]) -> tuple[list[dict], int]:
    """Match keywords against crawled pages (objects with .url/.html).
    Returns (results, total_hits) in the /api/search response shape."""
    results: list[dict] = []
    total_hits = 0
    for page in pages:
        hits = match_page(page.html, page.url, keywords)
        if not hits:
            continue
        total_hits += len(hits)
        results.append({
            "pageUrl": page.url,
            "pageTitle": extract_title(page.html),
            "displayTitle": extract_display_title(
                page.html, extract_title(page.html), page.url),
            "publishedDate": extract_published_date(page.html, page.url),
            "hits": [asdict(h) for h in hits],
        })
    return results, total_hits


def match_body_crawl_result(
    pages, keywords: list[str],
) -> tuple[list[dict], int]:
    """Aggregate article-body-only matches in the API response shape."""
    results: list[dict] = []
    total_hits = 0
    for page in pages:
        hits = match_body_page(page.html, page.url, keywords)
        if not hits:
            continue
        total_hits += len(hits)
        results.append({
            "pageUrl": page.url,
            "pageTitle": extract_title(page.html),
            "displayTitle": extract_display_title(
                page.html, extract_title(page.html), page.url),
            "publishedDate": extract_published_date(page.html, page.url),
            "hits": [asdict(hit) for hit in hits],
        })
    return results, total_hits


def _published_ordinal(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10]).toordinal()
    except (TypeError, ValueError):
        return None


def match_body_with_recall(
    pages,
    keywords: list[str],
    candidates: list[Candidate],
) -> tuple[list[dict], int, int]:
    """Return strong body matches plus title-only candidate recall.

    The hit count retains the existing body-hit meaning.  Weak count is the
    number of candidate result rows, not the number of matching title terms.
    """
    stable_keywords = list(
        dict.fromkeys(keyword.strip() for keyword in keywords if keyword.strip())
    )
    candidate_by_url: dict[str, tuple[Candidate, int]] = {}
    for index, candidate in enumerate(candidates):
        normalized = normalize_candidate_url(candidate.url)
        if not normalized:
            continue
        current = candidate_by_url.get(normalized)
        if current is None or candidate.score > current[0].score:
            candidate_by_url[normalized] = (candidate, index)

    ranked_results: list[tuple[dict, Candidate | None, int, str]] = []
    strong_urls: set[str] = set()
    strong_hit_count = 0
    for index, page in enumerate(pages):
        normalized = normalize_candidate_url(page.url)
        if normalized in strong_urls:
            continue
        hits = match_body_page(page.html, page.url, stable_keywords)
        if not hits:
            continue
        strong_urls.add(normalized)
        strong_hit_count += len(hits)
        candidate_entry = candidate_by_url.get(normalized)
        candidate = candidate_entry[0] if candidate_entry else None
        ranked_results.append((
            {
                "pageUrl": page.url,
                "pageTitle": extract_title(page.html),
                "displayTitle": extract_display_title(
                    page.html, extract_title(page.html), page.url),
                "hits": [asdict(hit) for hit in hits],
                "matchStrength": "strong",
                "publishedDate": (
                    candidate.published_date
                    if candidate and candidate.published_date
                    else None
                ),
            },
            candidate,
            index,
            normalized,
        ))

    weak_result_count = 0
    page_offset = len(pages)
    for normalized, (candidate, candidate_index) in candidate_by_url.items():
        if normalized in strong_urls:
            continue
        hits: list[Hit] = []
        for keyword in stable_keywords:
            snippet = make_snippet(candidate.title_hint, keyword)
            if snippet is None:
                continue
            hits.append(Hit(
                kind="title-recall",
                snippet=snippet,
                keyword=keyword,
                href=candidate.url,
            ))
        if not hits:
            continue
        weak_result_count += 1
        ranked_results.append((
            {
                "pageUrl": candidate.url,
                "pageTitle": candidate.title_hint,
                "displayTitle": candidate.title_hint,
                "hits": [asdict(hit) for hit in hits],
                "matchStrength": "weak",
                "publishedDate": candidate.published_date or None,
            },
            candidate,
            page_offset + candidate_index,
            normalized,
        ))

    def sort_key(
        item: tuple[dict, Candidate | None, int, str],
    ) -> tuple[int, bool, int, int, str, int]:
        result, candidate, original_index, normalized = item
        published = _published_ordinal(
            candidate.published_date if candidate else None
        )
        score = candidate.score if candidate else 0
        return (
            0 if result["matchStrength"] == "strong" else 1,
            published is None,
            -(published or 0),
            -score,
            normalized,
            original_index,
        )

    ranked_results.sort(key=sort_key)
    return (
        [result for result, _candidate, _index, _url in ranked_results],
        strong_hit_count,
        weak_result_count,
    )
