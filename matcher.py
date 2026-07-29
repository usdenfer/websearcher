"""Keyword matcher: search keywords across visible text and element attributes."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
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
    ".sidebar, .advert, .ad, .login"
)
MAIN_CONTENT_SELECTORS = (
    "article",
    "main",
    "[role='main']",
    ".article-content",
    ".content-main",
    ".article_body",
    ".detail-content",
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


def match_page(html: str, page_url: str, keywords: list[str]) -> list[Hit]:
    soup = BeautifulSoup(html, "html.parser")
    keywords = [k for k in keywords if k.strip()]
    hits: list[Hit] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, raw_text: str, keyword: str,
            link_href: str | None = None) -> None:
        snippet = make_snippet(raw_text, keyword)
        if snippet is None:
            return
        key = (kind, snippet)
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
    """Match keywords only against the extracted article body."""
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
                "hits": [asdict(hit) for hit in hits],
                "matchStrength": "strong",
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
                "hits": [asdict(hit) for hit in hits],
                "matchStrength": "weak",
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
